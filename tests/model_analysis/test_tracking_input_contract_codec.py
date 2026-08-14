from __future__ import annotations

import json
from dataclasses import replace

import pytest

import leo_flow.analysis.model.tracking_input_codec as codec
from leo_flow.analysis.dataset.snapshot import DatasetSnapshotRef
from leo_flow.analysis.model.tracking_input_codec import (
    MalformedTrackingInputError,
    decode_tracking_input,
    encode_tracking_input,
)
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
    Provenance,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    StationId,
    UtcNs,
    canonical_digest,
    canonical_json_bytes,
)
from leo_flow.contracts.ephemeris import (
    EphemerisSelection,
    EphemerisSelectionPolicy,
    EphemerisSnapshotRef,
    EphemerisSource,
    RecordingEphemerisLink,
    RecordingInterval,
)
from leo_flow.contracts.features import Covariance, FeatureSetRef
from leo_flow.contracts.hardware import (
    HardwareMetadataSnapshotRef,
    RecordingHardwareLink,
)
from leo_flow.contracts.storage import ObjectRef
from leo_flow.contracts.tracking_input import (
    RF_CALIBRATION_BASIS,
    RF_MEASUREMENT_BASIS,
    RF_UNITS,
    TRACKING_INPUT_FORMAT_ID,
    TRACKING_INPUT_MEDIA_TYPE,
    AbsoluteRfMeasurementEvidence,
    DurableDatasetIdentity,
    FeatureSetIdentity,
    PredictionCovarianceEvidence,
    ReceiverCalibrationEvidence,
    RfReferenceFrame,
    TrackingInputEntry,
    TrackingInputSnapshot,
    TrackingInputSnapshotRef,
    receiver_calibration_digest,
    tracking_input_membership_digest,
    tracking_input_snapshot_digest,
)


def _digest(label: str) -> Digest:
    return Digest.sha256(label.encode())


def _artifact(label: str, schema_id: str | None = None) -> ArtifactRef:
    return ArtifactRef(
        label,
        _digest(label),
        SchemaRef(schema_id or f"org.leo-flow.{label}", V0_1),
    )


def _covariance(
    basis: tuple[str, str],
    values: tuple[tuple[float, float], tuple[float, float]],
) -> Covariance:
    return Covariance(basis, RF_UNITS, values)


def _entry(index: int = 0, *, midpoint: int | None = None) -> TrackingInputEntry:
    recording_id = RecordingId(f"rec_tracking_{index}")
    identity_digest = _digest(f"recording-{index}")
    interval = RecordingInterval(
        UtcNs(1_000 + index * 1_000), UtcNs(2_000 + index * 1_000)
    )
    midpoint_ns = UtcNs(midpoint if midpoint is not None else 1_500 + index * 1_000)
    hardware_ref = HardwareMetadataSnapshotRef(
        HardwareSnapshotId(f"hw_tracking_{index}"), _digest(f"hardware-{index}")
    )
    hardware_digest = canonical_digest(
        {
            "recording_id": str(recording_id),
            "recording_identity_digest": str(identity_digest),
            "hardware_snapshot_id": str(hardware_ref.snapshot_id),
            "hardware_snapshot_digest": str(hardware_ref.digest),
        }
    )
    hardware_link = RecordingHardwareLink(
        f"hwlink_{hardware_digest.value[:32]}",
        recording_id,
        identity_digest,
        hardware_ref,
        hardware_digest,
    )
    policy_ref = _artifact(f"ephemeris-policy-{index}")
    snapshot_ref = EphemerisSnapshotRef(
        EphemerisSnapshotId(f"eph_tracking_{index}"),
        EphemerisSource.SPACE_TRACK,
        _digest(f"tle-raw-{index}"),
        _digest(f"tle-normalized-{index}"),
    )
    selection = EphemerisSelection(
        EphemerisSource.SPACE_TRACK,
        EphemerisSelectionPolicy.AVAILABLE_THEN,
        policy_ref,
        snapshot_ref,
        UtcNs(1_100 + index * 1_000),
    )
    ephemeris_digest = canonical_digest(
        {
            "recording_identity_digest": str(identity_digest),
            "recording_interval": interval,
            "source": selection.source.value,
            "scope": "active-leo",
            "policy": selection.policy.value,
            "policy_ref": selection.policy_ref,
            "as_of_utc_ns": selection.as_of_utc_ns,
            "snapshot_ref": selection.snapshot_ref,
        }
    )
    ephemeris_link = RecordingEphemerisLink(
        f"ephlink_{ephemeris_digest.value[:32]}",
        recording_id,
        identity_digest,
        interval,
        "active-leo",
        selection,
        ephemeris_digest,
    )
    measurement = AbsoluteRfMeasurementEvidence(
        FeatureId(f"feature_tracking_{index}"),
        recording_id,
        ReceiverChainId(f"rx_tracking_{index}"),
        midpoint_ns,
        RfReferenceFrame.ABSOLUTE_RF,
        (1_500_000_000.0 + index, -1_250.0),
        RF_MEASUREMENT_BASIS,
        RF_UNITS,
        _covariance(RF_MEASUREMENT_BASIS, ((4.0, 0.2), (0.2, 0.09))),
    )
    calibration_sources = (_artifact(f"calibration-source-{index}"),)
    calibration_covariance = _covariance(
        RF_CALIBRATION_BASIS, ((9.0, 0.3), (0.3, 0.04))
    )
    calibration_digest = receiver_calibration_digest(
        measurement.receiver_chain_id,
        hardware_ref,
        StationId("station_tracking"),
        interval,
        (120.0, -0.25),
        RF_CALIBRATION_BASIS,
        RF_UNITS,
        calibration_covariance,
        calibration_sources,
    )
    calibration = ReceiverCalibrationEvidence(
        ArtifactRef(
            f"rfcal_{calibration_digest.value[:32]}",
            calibration_digest,
            SchemaRef(ReceiverCalibrationEvidence.SCHEMA_ID, V0_1),
        ),
        measurement.receiver_chain_id,
        hardware_ref,
        StationId("station_tracking"),
        interval,
        (120.0, -0.25),
        RF_CALIBRATION_BASIS,
        RF_UNITS,
        calibration_covariance,
        calibration_sources,
    )
    return TrackingInputEntry(
        FeatureSetIdentity(
            FeatureSetId(f"fset_tracking_{index}"),
            AnalysisRunId(f"arun_tracking_{index}"),
            _digest(f"feature-bundle-{index}"),
            128 + index,
            "application/vnd.apache.parquet",
            "feature-set-v0.1",
        ),
        identity_digest,
        interval,
        hardware_link,
        ephemeris_link,
        measurement,
        calibration,
        PredictionCovarianceEvidence(
            _artifact(f"prediction-policy-{index}"),
            RF_MEASUREMENT_BASIS,
            RF_UNITS,
            _covariance(RF_MEASUREMENT_BASIS, ((16.0, -0.4), (-0.4, 0.16))),
        ),
    )


def _snapshot(
    entries: tuple[TrackingInputEntry, ...] | None = None,
) -> TrackingInputSnapshot:
    actual_entries = entries or (_entry(),)
    durable = DurableDatasetIdentity(
        DatasetSnapshotId("dataset_tracking"),
        _digest("dataset-membership"),
        _digest("dataset-snapshot"),
    )
    builder = _artifact("tracking-builder")
    selector = _artifact("tracking-selector")
    membership = tracking_input_membership_digest(actual_entries)
    provenance = Provenance(
        "tracking-input-builder",
        "0.1.0",
        "abc123",
        _digest("environment"),
        selector.digest,
        (durable.snapshot_digest, membership),
        (builder.digest, _digest("runtime")),
        UtcNs(10_000),
        UtcNs(10_001),
        "analysis-node",
    )
    schema = SchemaRef(TrackingInputSnapshot.SCHEMA_ID, V0_1)
    snapshot_digest = tracking_input_snapshot_digest(
        schema,
        durable,
        builder,
        selector,
        provenance,
        actual_entries,
        membership,
    )
    return TrackingInputSnapshot(
        schema,
        f"trackinput_{snapshot_digest.value[:32]}",
        durable,
        builder,
        selector,
        provenance,
        actual_entries,
        membership,
        snapshot_digest,
    )


def test_codec_round_trip_is_canonical_and_preserves_correlated_covariance() -> None:
    snapshot = _snapshot()
    encoded = encode_tracking_input(snapshot)

    assert encoded == canonical_json_bytes(snapshot)
    assert decode_tracking_input(encoded) == snapshot
    assert (
        decode_tracking_input(encoded).entries[0].measurement.covariance.values[0][1]
        == 0.2
    )


@pytest.mark.parametrize("mutation", ["unknown", "missing", "schema-version"])
def test_decoder_rejects_shape_and_schema_substitution(mutation: str) -> None:
    document = json.loads(encode_tracking_input(_snapshot()))
    if mutation == "unknown":
        document["unexpected"] = True
    elif mutation == "missing":
        del document["selector_ref"]
    else:
        document["schema"]["version"]["minor"] = 2

    with pytest.raises(MalformedTrackingInputError):
        decode_tracking_input(canonical_json_bytes(document))


def test_decoder_rejects_duplicate_keys_noncanonical_and_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = encode_tracking_input(_snapshot())
    with pytest.raises(MalformedTrackingInputError, match="duplicate"):
        decode_tracking_input(b'{"schema":null,"schema":null}')
    with pytest.raises(MalformedTrackingInputError, match="canonical"):
        decode_tracking_input(payload + b"\n")

    monkeypatch.setattr(codec, "MAX_TRACKING_INPUT_BYTES", len(payload) - 1)
    with pytest.raises(MalformedTrackingInputError, match="size"):
        decode_tracking_input(payload)


def test_decoder_applies_entry_and_source_bounds_before_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = json.loads(encode_tracking_input(_snapshot()))
    monkeypatch.setattr(codec, "MAX_TRACKING_INPUT_ENTRIES", 0)
    with pytest.raises(MalformedTrackingInputError, match="too many entries"):
        decode_tracking_input(canonical_json_bytes(document))

    monkeypatch.setattr(codec, "MAX_TRACKING_INPUT_ENTRIES", 10)
    monkeypatch.setattr(codec, "MAX_CALIBRATION_SOURCE_REFS", 0)
    with pytest.raises(MalformedTrackingInputError, match="too many entries"):
        decode_tracking_input(canonical_json_bytes(document))


@pytest.mark.parametrize(
    "values",
    [
        ((1.0, 2.0), (2.0, 1.0)),
        ((1.0, float("nan")), (float("nan"), 1.0)),
        ((1.0, float("inf")), (float("inf"), 1.0)),
    ],
)
def test_covariance_rejects_non_psd_or_non_finite_values(
    values: tuple[tuple[float, float], tuple[float, float]],
) -> None:
    with pytest.raises(ValueError):
        _covariance(RF_MEASUREMENT_BASIS, values)


def test_measurement_requires_exact_basis_units_shape_and_absolute_frame() -> None:
    measurement = _entry().measurement
    with pytest.raises(ValueError, match="basis or units"):
        replace(measurement, basis=("drift_hz_s", "frequency_hz"))
    with pytest.raises(ValueError, match="basis or units"):
        replace(measurement, units=("kHz", "Hz/s"))
    with pytest.raises(ValueError, match="shape"):
        Covariance(RF_MEASUREMENT_BASIS, RF_UNITS, ((1.0,),))
    with pytest.raises(ValueError, match="ABSOLUTE_RF"):
        replace(measurement, reference_frame="baseband")  # type: ignore[arg-type]


def test_intervals_are_half_open_for_recording_and_calibration() -> None:
    assert _entry(midpoint=1_000).measurement.midpoint_utc_ns == 1_000
    with pytest.raises(ValueError, match="half-open"):
        _entry(midpoint=2_000)

    entry = _entry()
    validity = RecordingInterval(UtcNs(1_000), entry.measurement.midpoint_utc_ns)
    calibration_digest = receiver_calibration_digest(
        entry.calibration.receiver_chain_id,
        entry.calibration.hardware_snapshot_ref,
        entry.calibration.station_id,
        validity,
        entry.calibration.value,
        entry.calibration.basis,
        entry.calibration.units,
        entry.calibration.covariance,
        entry.calibration.source_refs,
    )
    shortened = replace(
        entry.calibration,
        validity=validity,
        calibration_ref=ArtifactRef(
            f"rfcal_{calibration_digest.value[:32]}",
            calibration_digest,
            SchemaRef(ReceiverCalibrationEvidence.SCHEMA_ID, V0_1),
        ),
    )
    with pytest.raises(ValueError, match="calibration validity"):
        replace(entry, calibration=shortened)


def test_snapshot_rejects_noncanonical_order_duplicates_and_open_provenance() -> None:
    first = _entry(0)
    second = _entry(1)
    with pytest.raises(ValueError, match="canonical order"):
        _snapshot((second, first))
    with pytest.raises(ValueError, match="duplicated"):
        _snapshot((first, first))

    snapshot = _snapshot()
    with pytest.raises(ValueError, match="close inputs"):
        replace(
            snapshot,
            provenance=replace(snapshot.provenance, input_digests=(_digest("other"),)),
        )


def test_scientific_substitution_changes_membership_and_snapshot_identity() -> None:
    snapshot = _snapshot()
    entry = snapshot.entries[0]
    changed_entry = replace(
        entry,
        measurement=replace(
            entry.measurement,
            value=(entry.measurement.value[0] + 1.0, entry.measurement.value[1]),
        ),
    )
    changed_membership = tracking_input_membership_digest((changed_entry,))
    assert changed_membership != snapshot.membership_digest

    changed_snapshot_digest = tracking_input_snapshot_digest(
        snapshot.schema,
        snapshot.durable_dataset,
        snapshot.builder_ref,
        snapshot.selector_ref,
        replace(
            snapshot.provenance,
            input_digests=(
                snapshot.durable_dataset.snapshot_digest,
                changed_membership,
            ),
        ),
        (changed_entry,),
        changed_membership,
    )
    assert changed_snapshot_digest != snapshot.snapshot_digest


def test_approved_mirror_identities_are_exact_and_locator_independent() -> None:
    object_a = ObjectRef(
        _digest("feature-bundle"),
        123,
        "application/vnd.apache.parquet",
        "feature-set-v0.1",
        "file:///one",
    )
    object_b = replace(object_a, locator="s3://relocated/two")
    source_a = FeatureSetRef(
        FeatureSetId("fset_mirror"), AnalysisRunId("arun_mirror"), object_a
    )
    source_b = replace(source_a, bundle_ref=object_b)

    def mirror(source: FeatureSetRef) -> FeatureSetIdentity:
        return FeatureSetIdentity(
            source.feature_set_id,
            source.analysis_run_id,
            source.bundle_ref.digest,
            source.bundle_ref.byte_count,
            source.bundle_ref.media_type,
            source.bundle_ref.format_id,
        )

    assert mirror(source_a) == mirror(source_b)
    durable_source = DatasetSnapshotRef(
        DatasetSnapshotId("dataset_mirror"), _digest("members"), _digest("snapshot")
    )
    assert DurableDatasetIdentity(
        durable_source.snapshot_id,
        durable_source.feature_membership_digest,
        durable_source.snapshot_digest,
    ) == DurableDatasetIdentity(
        DatasetSnapshotId("dataset_mirror"), _digest("members"), _digest("snapshot")
    )


def test_tracking_snapshot_ref_identity_excludes_replaceable_locator() -> None:
    snapshot = _snapshot()
    bundle = ObjectRef(
        _digest("encoded-bundle"),
        len(encode_tracking_input(snapshot)),
        TRACKING_INPUT_MEDIA_TYPE,
        TRACKING_INPUT_FORMAT_ID,
        "file:///first",
    )
    first = TrackingInputSnapshotRef(
        snapshot.snapshot_id,
        snapshot.snapshot_digest,
        snapshot.membership_digest,
        bundle,
    )
    relocated = replace(
        first, bundle_ref=replace(bundle, locator="s3://archive/second")
    )
    assert first.identity_digest() == relocated.identity_digest()

    with pytest.raises(ValueError, match="metadata"):
        replace(
            first, bundle_ref=replace(bundle, media_type="application/octet-stream")
        )
