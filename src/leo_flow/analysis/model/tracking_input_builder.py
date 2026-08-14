"""Pure, deterministic freezing of closed tracking-input evidence."""

from __future__ import annotations

from dataclasses import dataclass

from leo_flow.analysis.dataset.snapshot import (
    DatasetSnapshotBundle,
    DatasetSnapshotRef,
    verify_snapshot_ref,
)
from leo_flow.contracts._validation import require_token
from leo_flow.contracts.capture import RecordingManifest
from leo_flow.contracts.core import (
    V0_1,
    ArtifactRef,
    Digest,
    FeatureId,
    Provenance,
    SchemaRef,
    canonical_digest,
)
from leo_flow.contracts.ephemeris import (
    EphemerisSelectionPolicy,
    RecordingEphemerisLink,
    RecordingInterval,
)
from leo_flow.contracts.features import (
    Covariance,
    FeatureObservation,
    FeatureSetBundle,
    FeatureSetRef,
)
from leo_flow.contracts.hardware import RecordingHardwareLink
from leo_flow.contracts.storage import PublishedRecordingRef
from leo_flow.contracts.tracking_input import (
    RF_MEASUREMENT_BASIS,
    RF_UNITS,
    AbsoluteRfMeasurementEvidence,
    DurableDatasetIdentity,
    FeatureSetIdentity,
    PredictionCovarianceEvidence,
    ReceiverCalibrationEvidence,
    RfReferenceFrame,
    TrackingInputEntry,
    TrackingInputSnapshot,
    tracking_entry_key,
    tracking_input_membership_digest,
    tracking_input_snapshot_digest,
)


class TrackingInputBuildError(ValueError):
    """Supplied evidence does not close over one exact deterministic snapshot."""


@dataclass(frozen=True)
class TrackingSelectionSpec:
    """Content-identified semantics for observations admitted to tracking."""

    artifact_ref: ArtifactRef
    method_id: str
    method_version: str
    feature_kind: str
    reference_frame: RfReferenceFrame
    required_measurements: tuple[str, str]
    covariance_basis: tuple[str, str]
    covariance_units: tuple[str, str]

    SCHEMA_ID = "org.leo-flow.tracking-selection-spec"

    def __post_init__(self) -> None:
        require_token(self.method_id, "method_id")
        require_token(self.method_version, "method_version")
        require_token(self.feature_kind, "feature_kind")
        if self.reference_frame is not RfReferenceFrame.ABSOLUTE_RF:
            raise ValueError("tracking selection must require ABSOLUTE_RF")
        if self.required_measurements != RF_MEASUREMENT_BASIS:
            raise ValueError("tracking selection must require frequency and drift")
        if (
            self.covariance_basis != RF_MEASUREMENT_BASIS
            or self.covariance_units != RF_UNITS
        ):
            raise ValueError("tracking selection covariance semantics differ")
        expected = tracking_selection_spec_digest(
            self.method_id,
            self.method_version,
            self.feature_kind,
            self.reference_frame,
            self.required_measurements,
            self.covariance_basis,
            self.covariance_units,
        )
        if (
            self.artifact_ref.schema != SchemaRef(self.SCHEMA_ID, V0_1)
            or self.artifact_ref.digest != expected
            or self.artifact_ref.artifact_id != f"tracksel_{expected.value[:32]}"
        ):
            raise ValueError("tracking selection artifact differs from its content")


def tracking_selection_spec_digest(
    method_id: str,
    method_version: str,
    feature_kind: str,
    reference_frame: RfReferenceFrame,
    required_measurements: tuple[str, str],
    covariance_basis: tuple[str, str],
    covariance_units: tuple[str, str],
) -> Digest:
    return canonical_digest(
        {
            "schema": SchemaRef(TrackingSelectionSpec.SCHEMA_ID, V0_1),
            "method_id": method_id,
            "method_version": method_version,
            "feature_kind": feature_kind,
            "reference_frame": reference_frame,
            "required_measurements": required_measurements,
            "covariance_basis": covariance_basis,
            "covariance_units": covariance_units,
        }
    )


@dataclass(frozen=True)
class ObservationTrackingEvidence:
    """Required external evidence for exactly one selected feature observation."""

    feature_id: FeatureId
    calibration: ReceiverCalibrationEvidence
    prediction: PredictionCovarianceEvidence


@dataclass(frozen=True)
class TrackingMemberSource:
    """All already-loaded authoritative evidence for one frozen dataset member."""

    feature_set_ref: FeatureSetRef
    feature_set: FeatureSetBundle
    published_recording: PublishedRecordingRef
    recording_manifest: RecordingManifest
    hardware_link: RecordingHardwareLink
    ephemeris_link: RecordingEphemerisLink
    observation_evidence: tuple[ObservationTrackingEvidence, ...]


def freeze_tracking_inputs(
    *,
    dataset: DatasetSnapshotBundle,
    expected_dataset_ref: DatasetSnapshotRef,
    selection: TrackingSelectionSpec,
    member_sources: tuple[TrackingMemberSource, ...],
    builder_ref: ArtifactRef,
    provenance: Provenance,
) -> TrackingInputSnapshot:
    """Freeze supplied values only; this function owns no reader capability."""

    try:
        verify_snapshot_ref(dataset, expected_dataset_ref)
    except ValueError as error:
        raise TrackingInputBuildError("dataset snapshot was substituted") from error
    if builder_ref.schema is None:
        raise TrackingInputBuildError("tracking builder requires an exact schema")
    expected_refs = tuple(member.feature_set_ref for member in dataset.members)
    supplied_refs = tuple(source.feature_set_ref for source in member_sources)
    if supplied_refs != expected_refs:
        raise TrackingInputBuildError(
            "member sources must exactly match ordered dataset membership"
        )
    if len(set(supplied_refs)) != len(supplied_refs):
        raise TrackingInputBuildError("member source references are duplicated")

    entries: list[TrackingInputEntry] = []
    for member, source in zip(dataset.members, member_sources, strict=True):
        entries.extend(
            _freeze_member(member.feature_set_ref, source, selection, dataset)
        )
    if not entries:
        raise TrackingInputBuildError("tracking selection produced no observations")
    ordered_entries = tuple(sorted(entries, key=tracking_entry_key))
    membership_digest = tracking_input_membership_digest(ordered_entries)
    durable_identity = DurableDatasetIdentity(
        expected_dataset_ref.snapshot_id,
        expected_dataset_ref.feature_membership_digest,
        expected_dataset_ref.snapshot_digest,
    )
    _verify_provenance(
        provenance,
        durable_identity,
        membership_digest,
        builder_ref,
        selection.artifact_ref,
    )
    schema = SchemaRef(TrackingInputSnapshot.SCHEMA_ID, V0_1)
    snapshot_digest = tracking_input_snapshot_digest(
        schema,
        durable_identity,
        builder_ref,
        selection.artifact_ref,
        provenance,
        ordered_entries,
        membership_digest,
    )
    return TrackingInputSnapshot(
        schema,
        f"trackinput_{snapshot_digest.value[:32]}",
        durable_identity,
        builder_ref,
        selection.artifact_ref,
        provenance,
        ordered_entries,
        membership_digest,
        snapshot_digest,
    )


def _freeze_member(
    expected_ref: FeatureSetRef,
    source: TrackingMemberSource,
    selection: TrackingSelectionSpec,
    dataset: DatasetSnapshotBundle,
) -> list[TrackingInputEntry]:
    bundle = source.feature_set
    if source.feature_set_ref != expected_ref:
        raise TrackingInputBuildError("feature source reference was substituted")
    if (
        bundle.feature_set_id != expected_ref.feature_set_id
        or bundle.analysis_run_id != expected_ref.analysis_run_id
    ):
        raise TrackingInputBuildError(
            "feature bundle identity differs from its reference"
        )

    recording_ref = source.published_recording.recording_object
    manifest = source.recording_manifest
    if (
        bundle.recording_id != recording_ref.recording_id
        or manifest.recording_id != recording_ref.recording_id
        or bundle.input_recording_identity_digest != recording_ref.identity_digest()
        or recording_ref.manifest_digest != canonical_digest(manifest)
    ):
        raise TrackingInputBuildError("recording identity or manifest was substituted")
    interval = RecordingInterval(
        manifest.capture_started_utc_ns, manifest.capture_finished_utc_ns
    )
    _verify_links(source, interval, dataset)

    selected = tuple(
        observation
        for observation in bundle.observations
        if observation.method_id == selection.method_id
        and observation.method_version == selection.method_version
        and observation.feature_kind == selection.feature_kind
    )
    selected_ids = tuple(item.feature_id for item in selected)
    if len(set(selected_ids)) != len(selected_ids):
        raise TrackingInputBuildError("selected feature observations are duplicated")
    evidence_ids = tuple(item.feature_id for item in source.observation_evidence)
    if len(set(evidence_ids)) != len(evidence_ids):
        raise TrackingInputBuildError("observation evidence is duplicated")
    if set(evidence_ids) != set(selected_ids) or len(evidence_ids) != len(selected_ids):
        raise TrackingInputBuildError(
            "observation evidence must exactly cover selected observations"
        )
    evidence_by_id = {item.feature_id: item for item in source.observation_evidence}
    return [
        _freeze_observation(
            expected_ref,
            observation,
            bundle,
            recording_ref.identity_digest(),
            interval,
            source,
            evidence_by_id[observation.feature_id],
            selection,
        )
        for observation in selected
    ]


def _verify_links(
    source: TrackingMemberSource,
    interval: RecordingInterval,
    dataset: DatasetSnapshotBundle,
) -> None:
    recording = source.published_recording.recording_object
    try:
        hardware = RecordingHardwareLink(
            source.hardware_link.link_id,
            source.hardware_link.recording_id,
            source.hardware_link.recording_identity_digest,
            source.hardware_link.hardware_snapshot_ref,
            source.hardware_link.link_digest,
        )
        ephemeris = RecordingEphemerisLink(
            source.ephemeris_link.link_id,
            source.ephemeris_link.recording_id,
            source.ephemeris_link.recording_identity_digest,
            source.ephemeris_link.recording_interval,
            source.ephemeris_link.scope,
            source.ephemeris_link.selection,
            source.ephemeris_link.link_digest,
        )
    except ValueError as error:
        raise TrackingInputBuildError("recording link contract is invalid") from error
    identity = recording.identity_digest()
    if (
        hardware.recording_id != recording.recording_id
        or hardware.recording_identity_digest != identity
        or ephemeris.recording_id != recording.recording_id
        or ephemeris.recording_identity_digest != identity
        or ephemeris.recording_interval != interval
    ):
        raise TrackingInputBuildError(
            "recording links do not match authoritative identity"
        )
    if ephemeris.selection.policy is EphemerisSelectionPolicy.BEST_EPHEMERIS:
        raise TrackingInputBuildError("best_ephemeris is not a frozen selection policy")
    if int(ephemeris.selection.as_of_utc_ns) > int(
        dataset.feature_dataset.selection_cutoff_utc_ns
    ):
        raise TrackingInputBuildError("ephemeris selection crosses dataset cutoff")


def _freeze_observation(
    feature_ref: FeatureSetRef,
    observation: FeatureObservation,
    bundle: FeatureSetBundle,
    recording_identity: Digest,
    interval: RecordingInterval,
    source: TrackingMemberSource,
    evidence: ObservationTrackingEvidence,
    selection: TrackingSelectionSpec,
) -> TrackingInputEntry:
    if observation.recording_id != bundle.recording_id:
        raise TrackingInputBuildError("selected feature belongs to another recording")
    if (
        observation.receiver_chain_id is None
        or observation.receiver_pair_id is not None
    ):
        raise TrackingInputBuildError(
            "tracking feature must identify one receiver chain"
        )
    if observation.frequency_hz is None or observation.drift_hz_s is None:
        raise TrackingInputBuildError("tracking feature lacks frequency or drift")
    if observation.covariance is None:
        raise TrackingInputBuildError("tracking feature lacks full covariance")
    try:
        covariance = Covariance(
            observation.covariance.basis,
            observation.covariance.units,
            observation.covariance.values,
            observation.covariance.psd_tolerance,
        )
    except (TypeError, ValueError) as error:
        raise TrackingInputBuildError(
            "tracking feature covariance is invalid"
        ) from error
    if (
        covariance.basis != selection.covariance_basis
        or covariance.units != selection.covariance_units
        or len(covariance.values) != 2
        or any(len(row) != 2 for row in covariance.values)
    ):
        raise TrackingInputBuildError("tracking feature covariance semantics differ")
    manifest = source.recording_manifest
    if observation.receiver_chain_id not in manifest.receiver_chain_ids:
        raise TrackingInputBuildError("tracking receiver chain is absent from manifest")
    try:
        calibration = ReceiverCalibrationEvidence(
            evidence.calibration.calibration_ref,
            evidence.calibration.receiver_chain_id,
            evidence.calibration.hardware_snapshot_ref,
            evidence.calibration.station_id,
            evidence.calibration.validity,
            evidence.calibration.value,
            evidence.calibration.basis,
            evidence.calibration.units,
            evidence.calibration.covariance,
            evidence.calibration.source_refs,
        )
        prediction = PredictionCovarianceEvidence(
            evidence.prediction.policy_ref,
            evidence.prediction.basis,
            evidence.prediction.units,
            evidence.prediction.covariance,
        )
    except (TypeError, ValueError) as error:
        raise TrackingInputBuildError(
            "tracking uncertainty evidence is invalid"
        ) from error
    if (
        calibration.station_id != manifest.station_id
        or calibration.receiver_chain_id != observation.receiver_chain_id
        or calibration.hardware_snapshot_ref
        != source.hardware_link.hardware_snapshot_ref
    ):
        raise TrackingInputBuildError("calibration authority differs from recording")
    midpoint = int(observation.midpoint_utc_ns)
    if not (int(interval.started_utc_ns) <= midpoint < int(interval.finished_utc_ns)):
        raise TrackingInputBuildError("feature midpoint is outside half-open recording")
    if not (
        int(calibration.validity.started_utc_ns)
        <= midpoint
        < int(calibration.validity.finished_utc_ns)
    ):
        raise TrackingInputBuildError("calibration is invalid at feature midpoint")
    if prediction.policy_ref.schema is None:
        raise TrackingInputBuildError("prediction covariance lacks an exact policy")

    return TrackingInputEntry(
        FeatureSetIdentity(
            feature_ref.feature_set_id,
            feature_ref.analysis_run_id,
            feature_ref.bundle_ref.digest,
            feature_ref.bundle_ref.byte_count,
            feature_ref.bundle_ref.media_type,
            feature_ref.bundle_ref.format_id,
        ),
        recording_identity,
        interval,
        source.hardware_link,
        source.ephemeris_link,
        AbsoluteRfMeasurementEvidence(
            observation.feature_id,
            observation.recording_id,
            observation.receiver_chain_id,
            observation.midpoint_utc_ns,
            selection.reference_frame,
            (observation.frequency_hz, observation.drift_hz_s),
            selection.required_measurements,
            selection.covariance_units,
            covariance,
        ),
        calibration,
        prediction,
    )


def _verify_provenance(
    provenance: Provenance,
    dataset: DurableDatasetIdentity,
    membership_digest: Digest,
    builder_ref: ArtifactRef,
    selector_ref: ArtifactRef,
) -> None:
    if provenance.normalized_config_digest != selector_ref.digest:
        raise TrackingInputBuildError("provenance selector digest differs")
    if provenance.input_digests != (dataset.snapshot_digest, membership_digest):
        raise TrackingInputBuildError("provenance does not close exact inputs")
    if (
        not provenance.dependency_digests
        or provenance.dependency_digests[0] != builder_ref.digest
    ):
        raise TrackingInputBuildError("provenance does not identify exact builder")
