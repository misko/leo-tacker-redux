from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from leo_flow.analysis.dataset import (
    DatasetMember,
    DatasetRole,
    DatasetSnapshotBundle,
    DatasetSplit,
    LabelEvidence,
    LabelSource,
    TruthLabel,
    dataset_snapshot_digest,
)
from leo_flow.analysis.model import (
    EphemerisLinkRequirement,
    ModelInputAssemblyError,
    assemble_model_inputs,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    DatasetSnapshotId,
    Digest,
    EphemerisSnapshotId,
    HardwareSnapshotId,
    RecordingId,
    SchemaRef,
    UtcNs,
    canonical_digest,
)
from leo_flow.contracts.ephemeris import (
    EphemerisSelection,
    EphemerisSelectionPolicy,
    EphemerisSnapshotRef,
    EphemerisSource,
    RecordingEphemerisLink,
    RecordingInterval,
)
from leo_flow.contracts.hardware import (
    HardwareMetadataSnapshotRef,
    RecordingHardwareLink,
)
from leo_flow.contracts.model import (
    FeatureDatasetSnapshot,
    feature_dataset_membership_digest,
)
from leo_flow.contracts.storage import (
    ObjectRef,
    PublishedRecordingRef,
    RecordingObjectRef,
)
from tests.model_analysis.fakes import FakeFeatureSetReader, feature_set


def _digest(value: str) -> Digest:
    return Digest.sha256(value.encode())


def _recording(index: int) -> PublishedRecordingRef:
    def object_ref(kind: str) -> ObjectRef:
        return ObjectRef(
            _digest(f"recording-{index}-{kind}"),
            100 + index,
            "application/octet-stream",
            f"recording-{kind}-v0.1",
            f"memory://recording/{index}/{kind}",
        )

    return PublishedRecordingRef(
        RecordingObjectRef(
            RecordingId(f"rec_{index}"),
            object_ref("data"),
            object_ref("metadata"),
            _digest(f"manifest-{index}"),
        )
    )


def _hardware_link(
    recording: PublishedRecordingRef, ref: HardwareMetadataSnapshotRef
) -> RecordingHardwareLink:
    identity = recording.recording_object.identity_digest()
    link_digest = canonical_digest(
        {
            "recording_id": str(recording.recording_id),
            "recording_identity_digest": str(identity),
            "hardware_snapshot_id": str(ref.snapshot_id),
            "hardware_snapshot_digest": str(ref.digest),
        }
    )
    return RecordingHardwareLink(
        f"hwlink_{link_digest.value[:32]}",
        recording.recording_id,
        identity,
        ref,
        link_digest,
    )


POLICY_REF = ArtifactRef("available-then-v1", _digest("policy"))
REQUIREMENT = EphemerisLinkRequirement(
    EphemerisSource.SPACE_TRACK,
    "leo",
    EphemerisSelectionPolicy.AVAILABLE_THEN,
    POLICY_REF,
    UtcNs(4_000),
)


def _ephemeris_link(
    recording: PublishedRecordingRef,
    ref: EphemerisSnapshotRef,
    *,
    scope: str = "leo",
    policy_ref: ArtifactRef = POLICY_REF,
    as_of: int = 4_000,
) -> RecordingEphemerisLink:
    selection = EphemerisSelection(
        ref.source,
        EphemerisSelectionPolicy.AVAILABLE_THEN,
        policy_ref,
        ref,
        UtcNs(as_of),
    )
    interval = RecordingInterval(UtcNs(1_000), UtcNs(2_000))
    identity = recording.recording_object.identity_digest()
    link_digest = canonical_digest(
        {
            "recording_identity_digest": str(identity),
            "recording_interval": interval,
            "source": selection.source.value,
            "scope": scope,
            "policy": selection.policy.value,
            "policy_ref": selection.policy_ref,
            "as_of_utc_ns": selection.as_of_utc_ns,
            "snapshot_ref": selection.snapshot_ref,
        }
    )
    return RecordingEphemerisLink(
        f"ephlink_{link_digest.value[:32]}",
        recording.recording_id,
        identity,
        interval,
        scope,
        selection,
        link_digest,
    )


class _Catalog:
    def __init__(self, values: dict[RecordingId, object]) -> None:
        self.values = values
        self.calls: list[RecordingId] = []

    def get(self, recording_id: RecordingId) -> object | None:
        self.calls.append(recording_id)
        return self.values.get(recording_id)


class _EphemerisCatalog:
    def __init__(self, links: tuple[RecordingEphemerisLink, ...]) -> None:
        self.links = list(links)
        self.calls: list[tuple[object, ...]] = []

    def get_exact(
        self,
        recording_id: RecordingId,
        source: EphemerisSource,
        scope: str,
        policy: EphemerisSelectionPolicy,
        policy_ref: ArtifactRef,
        as_of_utc_ns: UtcNs,
    ) -> RecordingEphemerisLink | None:
        self.calls.append(
            (recording_id, source, scope, policy, policy_ref, as_of_utc_ns)
        )
        matches = tuple(
            link
            for link in self.links
            if link.recording_id == recording_id
            and link.selection.source is source
            and link.scope == scope
            and link.selection.policy is policy
            and link.selection.policy_ref == policy_ref
            and link.selection.as_of_utc_ns == as_of_utc_ns
        )
        if len(matches) > 1:
            raise AssertionError("fake catalog contains duplicate exact links")
        return matches[0] if matches else None

    def replace_for_recording(self, replacement: RecordingEphemerisLink) -> None:
        self.links = [
            link for link in self.links if link.recording_id != replacement.recording_id
        ] + [replacement]


@dataclass
class _Fixture:
    dataset: DatasetSnapshotBundle
    features: FakeFeatureSetReader
    recordings: _Catalog
    hardware: _Catalog
    ephemeris: _EphemerisCatalog
    hardware_refs: tuple[HardwareMetadataSnapshotRef, ...]
    ephemeris_refs: tuple[EphemerisSnapshotRef, ...]


def _fixture() -> _Fixture:
    recordings = (_recording(0), _recording(1))
    feature_entries = []
    members = []
    truth = TruthLabel(
        True,
        LabelSource.OBSERVED,
        (
            LabelEvidence(
                LabelSource.OBSERVED,
                _digest("truth"),
                "review-v1",
                10,
                ("method-a",),
            ),
        ),
        1.0,
    )
    for index, recording in enumerate(recordings):
        ref, bundle = feature_set(index, ())
        bundle = replace(
            bundle,
            input_recording_identity_digest=recording.recording_object.identity_digest(),
        )
        feature_entries.append((ref, bundle))
        members.append(
            DatasetMember(
                ref,
                f"pass-{index}",
                DatasetSplit.TRAIN,
                DatasetRole.SCORED_TRUTH,
                truth,
            )
        )
    refs = tuple(member.feature_set_ref for member in members)
    feature_dataset = FeatureDatasetSnapshot(
        SchemaRef(FeatureDatasetSnapshot.SCHEMA_ID),
        DatasetSnapshotId("dataset_inputs"),
        refs,
        "explicit:v1",
        UtcNs(5_000),
        feature_dataset_membership_digest(refs),
    )
    member_tuple = tuple(members)
    snapshot_digest = dataset_snapshot_digest(
        feature_dataset, "method-a", member_tuple, True, ()
    )
    dataset = DatasetSnapshotBundle(
        SchemaRef(DatasetSnapshotBundle.SCHEMA_ID),
        feature_dataset,
        "method-a",
        member_tuple,
        snapshot_digest,
        True,
        (),
    )
    shared_hardware = HardwareMetadataSnapshotRef(
        HardwareSnapshotId("hw_shared"), _digest("hardware-shared")
    )
    ephemeris_refs = tuple(
        EphemerisSnapshotRef(
            EphemerisSnapshotId(f"eph_{index}"),
            EphemerisSource.SPACE_TRACK,
            _digest(f"raw-{index}"),
            _digest(f"normalized-{index}"),
        )
        for index in range(2)
    )
    return _Fixture(
        dataset,
        FakeFeatureSetReader(tuple(feature_entries)),
        _Catalog({item.recording_id: item for item in recordings}),
        _Catalog(
            {
                item.recording_id: _hardware_link(item, shared_hardware)
                for item in recordings
            }
        ),
        _EphemerisCatalog(
            tuple(
                _ephemeris_link(item, ephemeris_refs[index])
                for index, item in enumerate(recordings)
            )
        ),
        (shared_hardware,),
        ephemeris_refs,
    )


def _assemble(fixture: _Fixture):
    return assemble_model_inputs(
        dataset=fixture.dataset,
        expected_dataset_ref=fixture.dataset.ref,
        features=fixture.features,
        recordings=fixture.recordings,  # type: ignore[arg-type]
        hardware_links=fixture.hardware,  # type: ignore[arg-type]
        ephemeris_links=fixture.ephemeris,  # type: ignore[arg-type]
        ephemeris_requirement=REQUIREMENT,
        model_config_ref=ArtifactRef("model-config", _digest("config")),
        algorithm_ref=ArtifactRef("model-algorithm", _digest("algorithm")),
    )


def test_assembly_closes_over_ordered_unique_exact_link_references() -> None:
    fixture = _fixture()

    assembled = _assemble(fixture)

    assert assembled.ordered_recording_ids == (
        RecordingId("rec_0"),
        RecordingId("rec_1"),
    )
    assert assembled.request.hardware_metadata_snapshot_refs == fixture.hardware_refs
    assert assembled.request.ephemeris_snapshot_refs == fixture.ephemeris_refs
    assert assembled.request.dataset_snapshot_ref.snapshot_id == DatasetSnapshotId(
        "dataset_inputs"
    )
    assert fixture.recordings.calls == list(assembled.ordered_recording_ids)


def test_multiple_features_for_one_recording_do_not_duplicate_link_inputs() -> None:
    fixture = _fixture()
    existing_entries = []
    for member in fixture.dataset.members:
        with fixture.features.open(member.feature_set_ref) as view:
            existing_entries.append((view.ref, view.bundle()))
    extra_ref, extra_bundle = feature_set(2, ())
    first_recording = fixture.recordings.values[RecordingId("rec_0")]
    assert isinstance(first_recording, PublishedRecordingRef)
    extra_bundle = replace(
        extra_bundle,
        recording_id=first_recording.recording_id,
        input_recording_identity_digest=first_recording.recording_object.identity_digest(),
    )
    extra_member = replace(
        fixture.dataset.members[0],
        feature_set_ref=extra_ref,
        split_group_id="pass-extra",
    )
    members = (*fixture.dataset.members, extra_member)
    refs = tuple(member.feature_set_ref for member in members)
    feature_dataset = replace(
        fixture.dataset.feature_dataset,
        ordered_feature_set_refs=refs,
        membership_digest=feature_dataset_membership_digest(refs),
    )
    fixture.dataset = replace(
        fixture.dataset,
        feature_dataset=feature_dataset,
        members=members,
        snapshot_digest=dataset_snapshot_digest(
            feature_dataset, "method-a", members, True, ()
        ),
    )
    fixture.features = FakeFeatureSetReader(
        (*existing_entries, (extra_ref, extra_bundle))
    )

    assembled = _assemble(fixture)

    assert assembled.ordered_recording_ids == (
        RecordingId("rec_0"),
        RecordingId("rec_1"),
    )
    assert fixture.recordings.calls == [RecordingId("rec_0"), RecordingId("rec_1")]


@pytest.mark.parametrize("missing", ["hardware", "ephemeris"])
def test_assembly_fails_closed_when_a_recording_is_unlinked(missing: str) -> None:
    fixture = _fixture()
    if missing == "hardware":
        fixture.hardware.values.pop(RecordingId("rec_1"))
    else:
        fixture.ephemeris.links = [
            link
            for link in fixture.ephemeris.links
            if link.recording_id != RecordingId("rec_1")
        ]

    match = (
        "no authoritative hardware link"
        if missing == "hardware"
        else "no exact authoritative ephemeris link"
    )
    with pytest.raises(ModelInputAssemblyError, match=match):
        _assemble(fixture)


def test_assembly_rejects_recording_identity_disagreement() -> None:
    fixture = _fixture()
    recording = fixture.recordings.values[RecordingId("rec_0")]
    assert isinstance(recording, PublishedRecordingRef)
    fixture.recordings.values[RecordingId("rec_0")] = _recording(9)

    with pytest.raises(ModelInputAssemblyError, match="substituted an identity"):
        _assemble(fixture)


@pytest.mark.parametrize("change", ["scope", "policy"])
def test_assembly_rejects_wrong_ephemeris_selection_regime(change: str) -> None:
    fixture = _fixture()
    recording = fixture.recordings.values[RecordingId("rec_0")]
    assert isinstance(recording, PublishedRecordingRef)
    reference = fixture.ephemeris_refs[0]
    if change == "scope":
        link = _ephemeris_link(recording, reference, scope="wrong-scope")
    elif change == "policy":
        link = _ephemeris_link(
            recording, reference, policy_ref=ArtifactRef("other", _digest("other"))
        )
    fixture.ephemeris.replace_for_recording(link)

    with pytest.raises(ModelInputAssemblyError, match="required selection regime"):
        _assemble(fixture)


def test_assembly_rejects_exact_as_of_after_dataset_cutoff() -> None:
    fixture = _fixture()
    recording = fixture.recordings.values[RecordingId("rec_0")]
    assert isinstance(recording, PublishedRecordingRef)
    fixture.ephemeris.replace_for_recording(
        _ephemeris_link(recording, fixture.ephemeris_refs[0], as_of=5_001)
    )
    requirement = replace(REQUIREMENT, as_of_utc_ns=UtcNs(5_001))

    with pytest.raises(ModelInputAssemblyError, match="selection cutoff"):
        assemble_model_inputs(
            dataset=fixture.dataset,
            expected_dataset_ref=fixture.dataset.ref,
            features=fixture.features,
            recordings=fixture.recordings,  # type: ignore[arg-type]
            hardware_links=fixture.hardware,  # type: ignore[arg-type]
            ephemeris_links=fixture.ephemeris,
            ephemeris_requirement=requirement,
            model_config_ref=ArtifactRef("config", _digest("config")),
            algorithm_ref=ArtifactRef("algorithm", _digest("algorithm")),
        )


def test_exact_lookup_ignores_other_links_for_same_recording() -> None:
    fixture = _fixture()
    recording = fixture.recordings.values[RecordingId("rec_0")]
    assert isinstance(recording, PublishedRecordingRef)
    fixture.ephemeris.links.append(
        _ephemeris_link(
            recording,
            replace(
                fixture.ephemeris_refs[0],
                snapshot_id=EphemerisSnapshotId("eph_other_regime"),
            ),
            scope="different-scope",
        )
    )

    assembled = _assemble(fixture)

    assert assembled.request.ephemeris_snapshot_refs == fixture.ephemeris_refs


def test_assembly_rejects_conflicting_refs_for_one_snapshot_id() -> None:
    fixture = _fixture()
    recording = fixture.recordings.values[RecordingId("rec_1")]
    assert isinstance(recording, PublishedRecordingRef)
    conflicting = replace(
        fixture.ephemeris_refs[0], normalized_digest=_digest("substituted-normalized")
    )
    fixture.ephemeris.replace_for_recording(_ephemeris_link(recording, conflicting))

    with pytest.raises(ModelInputAssemblyError, match="conflicting exact references"):
        _assemble(fixture)


def test_assembly_rejects_conflicting_hardware_ref_for_one_snapshot_id() -> None:
    fixture = _fixture()
    recording = fixture.recordings.values[RecordingId("rec_1")]
    assert isinstance(recording, PublishedRecordingRef)
    conflicting = replace(fixture.hardware_refs[0], digest=_digest("other-hardware"))
    fixture.hardware.values[recording.recording_id] = _hardware_link(
        recording, conflicting
    )

    with pytest.raises(ModelInputAssemblyError, match="conflicting exact references"):
        _assemble(fixture)


def test_assembly_rejects_cross_dataset_substitution_before_reader_access() -> None:
    fixture = _fixture()
    changed_ref = replace(fixture.dataset.ref, snapshot_digest=_digest("other-dataset"))

    with pytest.raises(
        ModelInputAssemblyError, match="dataset snapshot was substituted"
    ):
        assemble_model_inputs(
            dataset=fixture.dataset,
            expected_dataset_ref=changed_ref,
            features=fixture.features,
            recordings=fixture.recordings,  # type: ignore[arg-type]
            hardware_links=fixture.hardware,  # type: ignore[arg-type]
            ephemeris_links=fixture.ephemeris,  # type: ignore[arg-type]
            ephemeris_requirement=REQUIREMENT,
            model_config_ref=ArtifactRef("config", _digest("config")),
            algorithm_ref=ArtifactRef("algorithm", _digest("algorithm")),
        )

    assert fixture.features.calls == []


def test_ephemeris_link_contract_rejects_tampered_digest_and_id() -> None:
    recording = _recording(0)
    reference = EphemerisSnapshotRef(
        EphemerisSnapshotId("eph_contract"),
        EphemerisSource.SPACE_TRACK,
        _digest("raw-contract"),
        _digest("normalized-contract"),
    )
    link = _ephemeris_link(recording, reference)

    with pytest.raises(ValueError, match="digest differs"):
        replace(link, link_digest=_digest("tampered"))
    with pytest.raises(ValueError, match="derive from link digest"):
        replace(link, link_id="ephlink_" + "0" * 32)
