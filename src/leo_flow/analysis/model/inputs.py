"""Fail-closed assembly of exact cross-recording model inputs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeVar

from leo_flow.analysis.dataset import (
    DatasetSnapshotBundle,
    DatasetSnapshotRef,
    verify_snapshot_ref,
)
from leo_flow.contracts._validation import require_utc_ns
from leo_flow.contracts.core import ArtifactRef, Digest, RecordingId, SchemaRef, UtcNs
from leo_flow.contracts.ephemeris import (
    EphemerisSelectionPolicy,
    EphemerisSnapshotRef,
    EphemerisSource,
    RecordingEphemerisLink,
)
from leo_flow.contracts.features import FeatureSetBundle, FeatureSetRef
from leo_flow.contracts.hardware import (
    HardwareMetadataSnapshotRef,
    RecordingHardwareLink,
)
from leo_flow.contracts.model import (
    FeatureDatasetSnapshotRef,
    ModelAnalysisRequest,
)
from leo_flow.contracts.ports import FeatureSetReader
from leo_flow.contracts.storage import PublishedRecordingRef

_RefT = TypeVar("_RefT")


class ModelInputAssemblyError(ValueError):
    """A frozen dataset cannot be closed over exact authoritative inputs."""


class RecordingCatalogReader(Protocol):
    def get(self, recording_id: RecordingId) -> PublishedRecordingRef | None: ...


class RecordingHardwareLinkReader(Protocol):
    def get(self, recording_id: RecordingId) -> RecordingHardwareLink | None: ...


class RecordingEphemerisLinkReader(Protocol):
    def get_exact(
        self,
        recording_id: RecordingId,
        source: EphemerisSource,
        scope: str,
        policy: EphemerisSelectionPolicy,
        policy_ref: ArtifactRef,
        as_of_utc_ns: UtcNs,
    ) -> RecordingEphemerisLink | None: ...


@dataclass(frozen=True)
class EphemerisLinkRequirement:
    """The one frozen selection regime accepted by a model run."""

    source: EphemerisSource
    scope: str
    policy: EphemerisSelectionPolicy
    policy_ref: ArtifactRef
    as_of_utc_ns: UtcNs

    def __post_init__(self) -> None:
        if not self.scope or any(character.isspace() for character in self.scope):
            raise ValueError("scope must be a token")
        if self.policy is EphemerisSelectionPolicy.BEST_EPHEMERIS:
            raise ValueError("best_ephemeris has no frozen selection semantics")
        require_utc_ns(self.as_of_utc_ns, "as_of_utc_ns")


@dataclass(frozen=True)
class AssembledModelInputs:
    """A request and the ordered recording identities used to construct it."""

    request: ModelAnalysisRequest
    ordered_recording_ids: tuple[RecordingId, ...]
    recording_identity_digests: tuple[Digest, ...]


def assemble_model_inputs(
    *,
    dataset: DatasetSnapshotBundle,
    expected_dataset_ref: DatasetSnapshotRef,
    features: FeatureSetReader,
    recordings: RecordingCatalogReader,
    hardware_links: RecordingHardwareLinkReader,
    ephemeris_links: RecordingEphemerisLinkReader,
    ephemeris_requirement: EphemerisLinkRequirement,
    model_config_ref: ArtifactRef,
    algorithm_ref: ArtifactRef,
) -> AssembledModelInputs:
    """Build a model request without mutable lookups or inferred dependencies."""

    try:
        verify_snapshot_ref(dataset, expected_dataset_ref)
    except ValueError as error:
        raise ModelInputAssemblyError("dataset snapshot was substituted") from error

    recording_identities: dict[RecordingId, Digest] = {}
    ordered_recording_ids: list[RecordingId] = []
    for member in dataset.members:
        feature_ref = member.feature_set_ref
        bundle = _open_exact_feature(feature_ref, features)
        recording_id = bundle.recording_id
        identity_digest = bundle.input_recording_identity_digest
        prior = recording_identities.get(recording_id)
        if prior is not None:
            if prior != identity_digest:
                raise ModelInputAssemblyError(
                    f"recording {recording_id} has conflicting feature identities"
                )
            continue
        recording_identities[recording_id] = identity_digest
        ordered_recording_ids.append(recording_id)

    hardware_refs: list[HardwareMetadataSnapshotRef] = []
    ephemeris_refs: list[EphemerisSnapshotRef] = []
    hardware_by_id: dict[object, HardwareMetadataSnapshotRef] = {}
    ephemeris_by_id: dict[object, EphemerisSnapshotRef] = {}
    for recording_id in ordered_recording_ids:
        expected_identity = recording_identities[recording_id]
        published = recordings.get(recording_id)
        if published is None:
            raise ModelInputAssemblyError(
                f"recording {recording_id} is not authoritatively published"
            )
        recording_ref = published.recording_object
        if recording_ref.recording_id != recording_id:
            raise ModelInputAssemblyError("recording catalog substituted an identity")
        if recording_ref.identity_digest() != expected_identity:
            raise ModelInputAssemblyError(
                f"recording {recording_id} identity differs from frozen features"
            )

        hardware_link = hardware_links.get(recording_id)
        if hardware_link is None:
            raise ModelInputAssemblyError(
                f"recording {recording_id} has no authoritative hardware link"
            )
        _verify_hardware_link(hardware_link, recording_id, expected_identity)
        _append_exact_ref(
            hardware_refs,
            hardware_by_id,
            hardware_link.hardware_snapshot_ref.snapshot_id,
            hardware_link.hardware_snapshot_ref,
            kind="hardware",
        )

        ephemeris_link = ephemeris_links.get_exact(
            recording_id,
            ephemeris_requirement.source,
            ephemeris_requirement.scope,
            ephemeris_requirement.policy,
            ephemeris_requirement.policy_ref,
            ephemeris_requirement.as_of_utc_ns,
        )
        if ephemeris_link is None:
            raise ModelInputAssemblyError(
                f"recording {recording_id} has no exact authoritative ephemeris "
                "link for the required selection regime"
            )
        _verify_ephemeris_link(
            ephemeris_link,
            recording_id,
            expected_identity,
            ephemeris_requirement,
            maximum_as_of_utc_ns=int(dataset.feature_dataset.selection_cutoff_utc_ns),
        )
        _append_exact_ref(
            ephemeris_refs,
            ephemeris_by_id,
            ephemeris_link.selection.snapshot_ref.snapshot_id,
            ephemeris_link.selection.snapshot_ref,
            kind="ephemeris",
        )

    request = ModelAnalysisRequest(
        schema=SchemaRef(ModelAnalysisRequest.SCHEMA_ID),
        dataset_snapshot_ref=FeatureDatasetSnapshotRef(
            dataset.feature_dataset.snapshot_id,
            dataset.feature_dataset.membership_digest,
        ),
        hardware_metadata_snapshot_refs=tuple(hardware_refs),
        ephemeris_snapshot_refs=tuple(ephemeris_refs),
        model_config_ref=model_config_ref,
        algorithm_ref=algorithm_ref,
    )
    return AssembledModelInputs(
        request,
        tuple(ordered_recording_ids),
        tuple(recording_identities[item] for item in ordered_recording_ids),
    )


def _open_exact_feature(
    expected: FeatureSetRef, reader: FeatureSetReader
) -> FeatureSetBundle:
    try:
        with reader.open(expected) as view:
            if view.ref != expected:
                raise ModelInputAssemblyError("feature reader substituted a reference")
            bundle = view.bundle()
    except ModelInputAssemblyError:
        raise
    except Exception as error:
        raise ModelInputAssemblyError(
            f"cannot read frozen feature set {expected.feature_set_id}"
        ) from error
    if (
        bundle.feature_set_id != expected.feature_set_id
        or bundle.analysis_run_id != expected.analysis_run_id
    ):
        raise ModelInputAssemblyError("feature bundle identity differs from its ref")
    return bundle


def _verify_hardware_link(
    link: RecordingHardwareLink,
    recording_id: RecordingId,
    recording_identity: Digest,
) -> None:
    # Reconstructing invokes the public contract's digest and ID checks even for
    # custom catalogs that did not create the value through the normal adapter.
    try:
        validated = RecordingHardwareLink(
            link.link_id,
            link.recording_id,
            link.recording_identity_digest,
            link.hardware_snapshot_ref,
            link.link_digest,
        )
    except ValueError as error:
        raise ModelInputAssemblyError("hardware link contract is invalid") from error
    if validated.recording_id != recording_id:
        raise ModelInputAssemblyError("hardware catalog substituted a recording")
    if validated.recording_identity_digest != recording_identity:
        raise ModelInputAssemblyError("hardware link recording identity differs")


def _verify_ephemeris_link(
    link: RecordingEphemerisLink,
    recording_id: RecordingId,
    recording_identity: Digest,
    requirement: EphemerisLinkRequirement,
    *,
    maximum_as_of_utc_ns: int,
) -> None:
    try:
        validated = RecordingEphemerisLink(
            link.link_id,
            link.recording_id,
            link.recording_identity_digest,
            link.recording_interval,
            link.scope,
            link.selection,
            link.link_digest,
        )
    except ValueError as error:
        raise ModelInputAssemblyError("ephemeris link contract is invalid") from error
    if validated.recording_id != recording_id:
        raise ModelInputAssemblyError("ephemeris catalog substituted a recording")
    if validated.recording_identity_digest != recording_identity:
        raise ModelInputAssemblyError("ephemeris link recording identity differs")
    selection = validated.selection
    if (
        selection.source is not requirement.source
        or validated.scope != requirement.scope
        or selection.policy is not requirement.policy
        or selection.policy_ref != requirement.policy_ref
        or selection.as_of_utc_ns != requirement.as_of_utc_ns
    ):
        raise ModelInputAssemblyError("ephemeris link selection regime differs")
    if int(selection.as_of_utc_ns) > maximum_as_of_utc_ns:
        raise ModelInputAssemblyError("ephemeris link crosses dataset selection cutoff")


def _append_exact_ref(
    ordered: list[_RefT],
    by_id: dict[object, _RefT],
    snapshot_id: object,
    ref: _RefT,
    *,
    kind: str,
) -> None:
    prior = by_id.get(snapshot_id)
    if prior is None:
        by_id[snapshot_id] = ref
        ordered.append(ref)
    elif prior != ref:
        raise ModelInputAssemblyError(
            f"{kind} snapshot ID identifies conflicting exact references"
        )
