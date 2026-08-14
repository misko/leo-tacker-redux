from __future__ import annotations

import ast
import builtins
import inspect
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from leo_flow.analysis.dataset.api import (
    DatasetSplit,
    LabelEvidence,
    LabelSource,
    TruthLabel,
)
from leo_flow.analysis.dataset.snapshot import (
    DatasetMember,
    DatasetRole,
    DatasetSnapshotBundle,
    dataset_snapshot_digest,
)
from leo_flow.analysis.model.tracking_input_builder import (
    ObservationTrackingEvidence,
    TrackingInputBuildError,
    TrackingMemberSource,
    TrackingSelectionSpec,
    freeze_tracking_inputs,
    tracking_selection_spec_digest,
)
from leo_flow.analysis.recording.codec import (
    FEATURE_SET_FORMAT_ID,
    FEATURE_SET_MEDIA_TYPE,
    encode_feature_set,
)
from leo_flow.contracts.capture import RecordingManifest
from leo_flow.contracts.core import (
    V0_1,
    AnalysisRunId,
    ArtifactRef,
    DatasetSnapshotId,
    Digest,
    EphemerisSnapshotId,
    FeatureId,
    FeatureSetId,
    HardwareSnapshotId,
    PlanId,
    Provenance,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    StationId,
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
from leo_flow.contracts.features import (
    Covariance,
    FeatureObservation,
    FeatureSetBundle,
    FeatureSetRef,
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
from leo_flow.contracts.tracking_input import (
    RF_CALIBRATION_BASIS,
    RF_MEASUREMENT_BASIS,
    RF_UNITS,
    AbsoluteRfMeasurementEvidence,
    FeatureSetIdentity,
    PredictionCovarianceEvidence,
    ReceiverCalibrationEvidence,
    RfReferenceFrame,
    TrackingInputEntry,
    receiver_calibration_digest,
    tracking_entry_key,
    tracking_input_membership_digest,
)


def _digest(label: str) -> Digest:
    return Digest.sha256(label.encode())


def _artifact(label: str, schema_id: str | None = None) -> ArtifactRef:
    return ArtifactRef(
        label,
        _digest(label),
        SchemaRef(schema_id or f"org.leo-flow.{label}", V0_1),
    )


def _selection() -> TrackingSelectionSpec:
    digest = tracking_selection_spec_digest(
        "doppler",
        "5.0.0",
        "carrier_track",
        RfReferenceFrame.ABSOLUTE_RF,
        RF_MEASUREMENT_BASIS,
        RF_MEASUREMENT_BASIS,
        RF_UNITS,
    )
    return TrackingSelectionSpec(
        ArtifactRef(
            f"tracksel_{digest.value[:32]}",
            digest,
            SchemaRef(TrackingSelectionSpec.SCHEMA_ID, V0_1),
        ),
        "doppler",
        "5.0.0",
        "carrier_track",
        RfReferenceFrame.ABSOLUTE_RF,
        RF_MEASUREMENT_BASIS,
        RF_MEASUREMENT_BASIS,
        RF_UNITS,
    )


def _manifest(recording_id: RecordingId, chain: ReceiverChainId) -> RecordingManifest:
    return RecordingManifest(
        SchemaRef(RecordingManifest.SCHEMA_ID, V0_1),
        recording_id,
        UtcNs(900),
        UtcNs(1_000),
        UtcNs(2_000),
        StationId("station_tracking"),
        RadioId("radio_tracking"),
        "v5-test-radio",
        (chain,),
        "disciplined",
        HardwareSnapshotId("hw_tracking"),
        (),
        (),
        PlanId("plan_tracking"),
        "capture-v5",
    )


def _published(manifest: RecordingManifest) -> PublishedRecordingRef:
    data = ObjectRef(
        _digest("recording-data"),
        4_096,
        "application/octet-stream",
        "sigmf-data-v0.1",
        "opaque:data",
    )
    metadata = ObjectRef(
        _digest("recording-metadata"),
        512,
        "application/json",
        "recording-manifest-v0.1",
        "opaque:metadata",
    )
    return PublishedRecordingRef(
        RecordingObjectRef(
            manifest.recording_id,
            data,
            metadata,
            canonical_digest(manifest),
        )
    )


def _provenance(label: str) -> Provenance:
    return Provenance(
        label,
        "0.1.0",
        "abc123",
        _digest(f"{label}-environment"),
        _digest(f"{label}-config"),
        (_digest(f"{label}-input"),),
        (_digest(f"{label}-dependency"),),
        UtcNs(10),
        UtcNs(11),
        "test-node",
    )


def _observation(
    recording_id: RecordingId,
    chain: ReceiverChainId,
    *,
    feature_id: str = "feature_track_a",
    midpoint: int = 1_500,
    frequency_hz: float | None = 1_500_000_000.0,
    drift_hz_s: float | None = -1_250.0,
    covariance: Covariance | None = None,
    method_id: str = "doppler",
) -> FeatureObservation:
    return FeatureObservation(
        FeatureId(feature_id),
        recording_id,
        SegmentId("seg_tracking"),
        method_id,
        "5.0.0",
        0,
        256,
        256,
        UtcNs(midpoint),
        "carrier_track",
        12.0,
        "log_likelihood",
        receiver_chain_id=chain,
        frequency_hz=frequency_hz,
        drift_hz_s=drift_hz_s,
        covariance=covariance
        or Covariance(
            RF_MEASUREMENT_BASIS,
            RF_UNITS,
            ((4.0, 0.2), (0.2, 0.09)),
        ),
    )


def _calibration(
    chain: ReceiverChainId,
    hardware_ref: HardwareMetadataSnapshotRef,
    *,
    station: StationId | None = None,
    validity: RecordingInterval | None = None,
) -> ReceiverCalibrationEvidence:
    actual_station = station or StationId("station_tracking")
    actual_validity = validity or RecordingInterval(UtcNs(1_000), UtcNs(2_000))
    sources = (_artifact("calibration-source"),)
    covariance = Covariance(
        RF_CALIBRATION_BASIS,
        RF_UNITS,
        ((9.0, 0.3), (0.3, 0.04)),
    )
    value = (120.0, -0.25)
    digest = receiver_calibration_digest(
        chain,
        hardware_ref,
        actual_station,
        actual_validity,
        value,
        RF_CALIBRATION_BASIS,
        RF_UNITS,
        covariance,
        sources,
    )
    return ReceiverCalibrationEvidence(
        ArtifactRef(
            f"rfcal_{digest.value[:32]}",
            digest,
            SchemaRef(ReceiverCalibrationEvidence.SCHEMA_ID, V0_1),
        ),
        chain,
        hardware_ref,
        actual_station,
        actual_validity,
        value,
        RF_CALIBRATION_BASIS,
        RF_UNITS,
        covariance,
        sources,
    )


def _hardware_link(
    recording: RecordingObjectRef,
    hardware_ref: HardwareMetadataSnapshotRef,
) -> RecordingHardwareLink:
    identity = recording.identity_digest()
    digest = canonical_digest(
        {
            "recording_id": str(recording.recording_id),
            "recording_identity_digest": str(identity),
            "hardware_snapshot_id": str(hardware_ref.snapshot_id),
            "hardware_snapshot_digest": str(hardware_ref.digest),
        }
    )
    return RecordingHardwareLink(
        f"hwlink_{digest.value[:32]}",
        recording.recording_id,
        identity,
        hardware_ref,
        digest,
    )


def _ephemeris_link(
    recording: RecordingObjectRef,
    *,
    interval: RecordingInterval | None = None,
    as_of: int = 1_900,
    policy: EphemerisSelectionPolicy = EphemerisSelectionPolicy.AVAILABLE_THEN,
) -> RecordingEphemerisLink:
    actual_interval = interval or RecordingInterval(UtcNs(1_000), UtcNs(2_000))
    selection = EphemerisSelection(
        EphemerisSource.SPACE_TRACK,
        policy,
        _artifact("ephemeris-policy"),
        EphemerisSnapshotRef(
            EphemerisSnapshotId("eph_tracking"),
            EphemerisSource.SPACE_TRACK,
            _digest("tle-raw"),
            _digest("tle-normalized"),
        ),
        UtcNs(as_of),
    )
    identity = recording.identity_digest()
    digest = canonical_digest(
        {
            "recording_identity_digest": str(identity),
            "recording_interval": actual_interval,
            "source": selection.source.value,
            "scope": "active-leo",
            "policy": selection.policy.value,
            "policy_ref": selection.policy_ref,
            "as_of_utc_ns": selection.as_of_utc_ns,
            "snapshot_ref": selection.snapshot_ref,
        }
    )
    return RecordingEphemerisLink(
        f"ephlink_{digest.value[:32]}",
        recording.recording_id,
        identity,
        actual_interval,
        "active-leo",
        selection,
        digest,
    )


def _prediction() -> PredictionCovarianceEvidence:
    return PredictionCovarianceEvidence(
        _artifact("prediction-policy"),
        RF_MEASUREMENT_BASIS,
        RF_UNITS,
        Covariance(
            RF_MEASUREMENT_BASIS,
            RF_UNITS,
            ((16.0, -0.4), (-0.4, 0.16)),
        ),
    )


@dataclass(frozen=True)
class _Case:
    dataset: DatasetSnapshotBundle
    source: TrackingMemberSource
    selection: TrackingSelectionSpec
    builder_ref: ArtifactRef
    provenance: Provenance

    def freeze(self, source: TrackingMemberSource | None = None):  # type: ignore[no-untyped-def]
        return freeze_tracking_inputs(
            dataset=self.dataset,
            expected_dataset_ref=self.dataset.ref,
            selection=self.selection,
            member_sources=(source or self.source,),
            builder_ref=self.builder_ref,
            provenance=self.provenance,
        )


def _case(*, observations: tuple[FeatureObservation, ...] | None = None) -> _Case:
    recording_id = RecordingId("rec_tracking")
    chain = ReceiverChainId("rx_tracking")
    manifest = _manifest(recording_id, chain)
    published = _published(manifest)
    actual_observations = observations or (_observation(recording_id, chain),)
    feature_set = FeatureSetBundle(
        SchemaRef(FeatureSetBundle.SCHEMA_ID, V0_1),
        FeatureSetId("fset_tracking"),
        AnalysisRunId("arun_tracking"),
        recording_id,
        published.recording_object.identity_digest(),
        _provenance("feature-extractor"),
        actual_observations,
        (),
    )
    encoded = encode_feature_set(feature_set)
    feature_ref = FeatureSetRef(
        feature_set.feature_set_id,
        feature_set.analysis_run_id,
        ObjectRef(
            Digest.sha256(encoded),
            len(encoded),
            FEATURE_SET_MEDIA_TYPE,
            FEATURE_SET_FORMAT_ID,
            "opaque:feature-set",
        ),
    )
    truth_evidence = LabelEvidence(
        LabelSource.MANUAL,
        _digest("truth"),
        "human-review",
        800,
        ("doppler",),
    )
    member = DatasetMember(
        feature_ref,
        "recording-family-a",
        DatasetSplit.TRAIN,
        DatasetRole.SCORED_TRUTH,
        TruthLabel(True, LabelSource.MANUAL, (truth_evidence,), 1.0),
    )
    membership = feature_dataset_membership_digest((feature_ref,))
    feature_dataset = FeatureDatasetSnapshot(
        SchemaRef(FeatureDatasetSnapshot.SCHEMA_ID, V0_1),
        DatasetSnapshotId("dataset_tracking"),
        (feature_ref,),
        "frozen-training-split",
        UtcNs(2_000),
        membership,
    )
    dataset_digest = dataset_snapshot_digest(
        feature_dataset, "doppler", (member,), True, ()
    )
    dataset = DatasetSnapshotBundle(
        SchemaRef(DatasetSnapshotBundle.SCHEMA_ID, V0_1),
        feature_dataset,
        "doppler",
        (member,),
        dataset_digest,
        True,
        (),
    )
    hardware_ref = HardwareMetadataSnapshotRef(
        HardwareSnapshotId("hw_tracking"), _digest("hardware")
    )
    hardware_link = _hardware_link(published.recording_object, hardware_ref)
    ephemeris_link = _ephemeris_link(published.recording_object)
    selected = tuple(
        observation
        for observation in actual_observations
        if observation.method_id == "doppler"
    )
    evidence = tuple(
        ObservationTrackingEvidence(
            observation.feature_id,
            _calibration(chain, hardware_ref),
            _prediction(),
        )
        for observation in selected
    )
    source = TrackingMemberSource(
        feature_ref,
        feature_set,
        published,
        manifest,
        hardware_link,
        ephemeris_link,
        evidence,
    )
    selection = _selection()
    builder_ref = _artifact("tracking-builder")
    fallback_covariance = Covariance(
        RF_MEASUREMENT_BASIS,
        RF_UNITS,
        ((4.0, 0.2), (0.2, 0.09)),
    )
    expected_entries = tuple(
        sorted(
            (
                TrackingInputEntry(
                    FeatureSetIdentity(
                        feature_ref.feature_set_id,
                        feature_ref.analysis_run_id,
                        feature_ref.bundle_ref.digest,
                        feature_ref.bundle_ref.byte_count,
                        feature_ref.bundle_ref.media_type,
                        feature_ref.bundle_ref.format_id,
                    ),
                    published.recording_object.identity_digest(),
                    ephemeris_link.recording_interval,
                    hardware_link,
                    ephemeris_link,
                    AbsoluteRfMeasurementEvidence(
                        observation.feature_id,
                        observation.recording_id,
                        chain,
                        UtcNs(min(int(observation.midpoint_utc_ns), 1_999)),
                        RfReferenceFrame.ABSOLUTE_RF,
                        (
                            observation.frequency_hz
                            if observation.frequency_hz is not None
                            else 0.0,
                            observation.drift_hz_s
                            if observation.drift_hz_s is not None
                            else 0.0,
                        ),
                        RF_MEASUREMENT_BASIS,
                        RF_UNITS,
                        (
                            observation.covariance
                            if observation.covariance is not None
                            and observation.covariance.basis == RF_MEASUREMENT_BASIS
                            and observation.covariance.units == RF_UNITS
                            else fallback_covariance
                        ),
                    ),
                    item.calibration,
                    item.prediction,
                )
                for observation, item in zip(selected, evidence, strict=True)
            ),
            key=tracking_entry_key,
        )
    )
    tracking_membership = tracking_input_membership_digest(expected_entries)
    provenance = Provenance(
        "tracking-builder",
        "0.1.0",
        "abc123",
        _digest("builder-environment"),
        selection.artifact_ref.digest,
        (dataset.snapshot_digest, tracking_membership),
        (builder_ref.digest, _digest("runtime")),
        UtcNs(3_000),
        UtcNs(3_001),
        "analysis-node",
    )
    return _Case(dataset, source, selection, builder_ref, provenance)


def _rebuild_feature_source(
    observations: tuple[FeatureObservation, ...],
) -> _Case:
    return _case(observations=observations)


def test_freeze_builds_exact_deterministic_snapshot() -> None:
    case = _case()
    first = case.freeze()
    second = case.freeze()

    assert first == second
    assert first.durable_dataset.snapshot_digest == case.dataset.snapshot_digest
    assert first.entries[0].measurement.value == (1_500_000_000.0, -1_250.0)
    assert first.entries[0].measurement.covariance.values[0][1] == 0.2


def test_selection_spec_is_exact_content_identified_absolute_rf() -> None:
    selection = _selection()
    with pytest.raises(ValueError, match="content"):
        replace(selection, method_version="5.0.1")
    with pytest.raises(ValueError, match="ABSOLUTE_RF"):
        replace(selection, reference_frame="baseband")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="frequency and drift"):
        replace(selection, required_measurements=("frequency_hz", "snr_db"))


@pytest.mark.parametrize("kind", ["omitted", "extra", "duplicate"])
def test_member_sources_must_be_exact_ordered_one_to_one(kind: str) -> None:
    case = _case()
    sources = {
        "omitted": (),
        "extra": (case.source, case.source),
        "duplicate": (case.source, case.source),
    }[kind]
    with pytest.raises(TrackingInputBuildError, match="ordered dataset membership"):
        freeze_tracking_inputs(
            dataset=case.dataset,
            expected_dataset_ref=case.dataset.ref,
            selection=case.selection,
            member_sources=sources,
            builder_ref=case.builder_ref,
            provenance=case.provenance,
        )


@pytest.mark.parametrize("kind", ["omitted", "extra", "duplicate"])
def test_selected_observation_evidence_must_be_exact(kind: str) -> None:
    case = _case()
    current = case.source.observation_evidence[0]
    extra = replace(current, feature_id=FeatureId("feature_extra"))
    evidence = {
        "omitted": (),
        "extra": (current, extra),
        "duplicate": (current, current),
    }[kind]
    source = replace(case.source, observation_evidence=evidence)
    expected = "duplicated" if kind == "duplicate" else "exactly cover"
    with pytest.raises(TrackingInputBuildError, match=expected):
        case.freeze(source)


def test_dataset_feature_and_recording_substitutions_fail_closed() -> None:
    case = _case()
    with pytest.raises(TrackingInputBuildError, match="dataset snapshot"):
        freeze_tracking_inputs(
            dataset=case.dataset,
            expected_dataset_ref=replace(
                case.dataset.ref, snapshot_digest=_digest("substituted")
            ),
            selection=case.selection,
            member_sources=(case.source,),
            builder_ref=case.builder_ref,
            provenance=case.provenance,
        )
    substituted_bundle = replace(
        case.source.feature_set, feature_set_id=FeatureSetId("fset_substituted")
    )
    with pytest.raises(TrackingInputBuildError, match="bundle identity"):
        case.freeze(replace(case.source, feature_set=substituted_bundle))

    other_manifest = replace(case.source.recording_manifest, producer="other-capture")
    with pytest.raises(TrackingInputBuildError, match="manifest"):
        case.freeze(replace(case.source, recording_manifest=other_manifest))


def test_link_interval_policy_and_cutoff_are_revalidated() -> None:
    case = _case()
    recording = case.source.published_recording.recording_object
    wrong_interval = _ephemeris_link(
        recording,
        interval=RecordingInterval(UtcNs(1_000), UtcNs(1_999)),
    )
    with pytest.raises(TrackingInputBuildError, match="links"):
        case.freeze(replace(case.source, ephemeris_link=wrong_interval))

    after_cutoff = _ephemeris_link(recording, as_of=2_001)
    with pytest.raises(TrackingInputBuildError, match="cutoff"):
        case.freeze(replace(case.source, ephemeris_link=after_cutoff))

    mutable_policy = _ephemeris_link(
        recording, policy=EphemerisSelectionPolicy.BEST_EPHEMERIS
    )
    with pytest.raises(TrackingInputBuildError, match="not a frozen"):
        case.freeze(replace(case.source, ephemeris_link=mutable_policy))


@pytest.mark.parametrize("field", ["frequency", "drift", "covariance", "chain"])
def test_feature_measurement_semantics_fail_closed(field: str) -> None:
    base = _observation(RecordingId("rec_tracking"), ReceiverChainId("rx_tracking"))
    if field == "frequency":
        changed = replace(base, frequency_hz=None)
    elif field == "drift":
        changed = replace(base, drift_hz_s=None)
    elif field == "covariance":
        changed = replace(
            base,
            covariance=Covariance(
                ("frequency_offset_hz", "drift_hz_s"),
                RF_UNITS,
                ((4.0, 0.2), (0.2, 0.09)),
            ),
        )
    else:
        changed = replace(base, receiver_chain_id=ReceiverChainId("rx_absent"))
    case = _rebuild_feature_source((changed,))
    message = {
        "frequency": "frequency or drift",
        "drift": "frequency or drift",
        "covariance": "covariance semantics",
        "chain": "absent from manifest",
    }[field]
    with pytest.raises(TrackingInputBuildError, match=message):
        case.freeze()


def test_recording_and_calibration_intervals_are_stop_exclusive() -> None:
    stop = _observation(
        RecordingId("rec_tracking"), ReceiverChainId("rx_tracking"), midpoint=2_000
    )
    case = _case(observations=(stop,))
    with pytest.raises(TrackingInputBuildError, match="half-open"):
        case.freeze()

    case = _case()
    hardware_ref = case.source.hardware_link.hardware_snapshot_ref
    invalid = _calibration(
        ReceiverChainId("rx_tracking"),
        hardware_ref,
        validity=RecordingInterval(UtcNs(1_000), UtcNs(1_500)),
    )
    evidence = replace(case.source.observation_evidence[0], calibration=invalid)
    with pytest.raises(TrackingInputBuildError, match="invalid at feature midpoint"):
        case.freeze(replace(case.source, observation_evidence=(evidence,)))


def test_calibration_station_hardware_and_prediction_policy_are_required() -> None:
    case = _case()
    evidence = case.source.observation_evidence[0]
    wrong_station = _calibration(
        ReceiverChainId("rx_tracking"),
        case.source.hardware_link.hardware_snapshot_ref,
        station=StationId("station_other"),
    )
    with pytest.raises(TrackingInputBuildError, match="calibration authority"):
        case.freeze(
            replace(
                case.source,
                observation_evidence=(replace(evidence, calibration=wrong_station),),
            )
        )
    with pytest.raises(ValueError, match="requires a schema"):
        replace(
            evidence.prediction,
            policy_ref=ArtifactRef("prediction-policy", _digest("prediction-policy")),
        )


def test_output_order_is_independent_of_supplied_evidence_order() -> None:
    observations = (
        _observation(
            RecordingId("rec_tracking"),
            ReceiverChainId("rx_tracking"),
            feature_id="feature_track_b",
            midpoint=1_600,
        ),
        _observation(
            RecordingId("rec_tracking"),
            ReceiverChainId("rx_tracking"),
            feature_id="feature_track_a",
            midpoint=1_400,
        ),
    )
    case = _case(observations=observations)
    normal = case.freeze()
    reversed_source = replace(
        case.source,
        observation_evidence=tuple(reversed(case.source.observation_evidence)),
    )
    assert case.freeze(reversed_source) == normal
    assert tuple(item.measurement.midpoint_utc_ns for item in normal.entries) == (
        1_400,
        1_600,
    )


def test_provenance_must_close_selector_dataset_membership_and_builder() -> None:
    case = _case()
    substitutions = (
        replace(case.provenance, normalized_config_digest=_digest("other-selector")),
        replace(case.provenance, input_digests=(_digest("other-input"),)),
        replace(case.provenance, dependency_digests=(_digest("other-builder"),)),
    )
    for provenance in substitutions:
        with pytest.raises(TrackingInputBuildError, match="provenance"):
            freeze_tracking_inputs(
                dataset=case.dataset,
                expected_dataset_ref=case.dataset.ref,
                selection=case.selection,
                member_sources=(case.source,),
                builder_ref=case.builder_ref,
                provenance=provenance,
            )


def test_builder_never_requests_recording_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case()
    calls: list[object] = []

    def forbidden_open(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        raise AssertionError("recording bytes must never be opened")

    monkeypatch.setattr(builtins, "open", forbidden_open)
    assert case.freeze().entries
    assert calls == []
    assert "reader" not in inspect.signature(freeze_tracking_inputs).parameters


def test_builder_architecture_has_no_io_catalog_clock_or_network_imports() -> None:
    path = Path(inspect.getfile(freeze_tracking_inputs))
    tree = ast.parse(path.read_text())
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    forbidden = ("pathlib", "socket", "requests", "httpx", "time", "datetime")
    assert not any(name.startswith(forbidden) for name in imports)
    assert not any("catalog" in name or name.endswith(".ports") for name in imports)
    assert "leo_flow.analysis.recording" not in imports
