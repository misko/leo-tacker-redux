from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import replace
from importlib.util import find_spec
from pathlib import Path

import pytest

from leo_flow.analysis.ephemeris.normalization import parse_tle_catalog
from leo_flow.analysis.orbit import (
    AssociationPolicy,
    AssociationValidationCase,
    DigitalInjectionSpecification,
    EphemerisLinkEvidence,
    ReceiverRfCalibration,
    RfAssociationRequest,
    RfMeasurement,
    SatelliteCarrierHypothesis,
    Sgp4OrbitPropagator,
    StationGeometrySnapshot,
    ValidationOutcome,
    inject_synthetic_rf_measurement,
    run_association_validation,
    sgp4_vallado_wgs72_specification,
)
from leo_flow.contracts.core import (
    AnalysisRunId,
    ArtifactRef,
    Digest,
    EphemerisSnapshotId,
    FeatureId,
    FeatureSetId,
    HardwareSnapshotId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    StationId,
    UtcNs,
    canonical_digest,
    canonical_json_bytes,
)
from leo_flow.contracts.ephemeris import (
    EphemerisSelectionPolicy,
    EphemerisSnapshotRef,
    EphemerisSource,
    RecordingInterval,
)
from leo_flow.contracts.features import FeatureSetRef
from leo_flow.contracts.hardware import HardwareMetadataSnapshotRef
from leo_flow.contracts.storage import ObjectRef

FIXTURE = Path(__file__).with_name("fixtures") / "rf_association_synthetic_v1.json"
REQUIRES_SGP4 = pytest.mark.skipif(
    find_spec("sgp4") is None,
    reason="RF association validation requires the optional orbit extra",
)


class _View:
    def __init__(self, ref: EphemerisSnapshotRef, data: bytes) -> None:
        self.ref = ref
        self._data = data

    def normalized_bytes(self) -> bytes:
        return self._data


class _Reader:
    def __init__(self, ref: EphemerisSnapshotRef, data: bytes) -> None:
        self._view = _View(ref, data)

    def open(self, ref: EphemerisSnapshotRef):
        assert ref == self._view.ref
        return nullcontext(self._view)


def _artifact(name: str, value: object) -> ArtifactRef:
    return ArtifactRef(name, canonical_digest(value))


def _catalog(document: dict[str, object]) -> tuple[bytes, dict[int, UtcNs]]:
    raw_entries = document["tle"]
    assert isinstance(raw_entries, list)
    entries = []
    for raw in raw_entries:
        assert isinstance(raw, dict)
        parsed = parse_tle_catalog(f"{raw['line1']}\n{raw['line2']}\n".encode("ascii"))[
            0
        ]
        assert parsed.norad_id == raw["norad_id"]
        entries.append(parsed)
    data = canonical_json_bytes(
        {
            "schema": "org.leo-flow.normalized-tle-catalog",
            "version": "1.0",
            "source": EphemerisSource.SPACE_TRACK.value,
            "scope": "rf-validation",
            "entries": [
                {
                    "norad_id": entry.norad_id,
                    "name": entry.name,
                    "line1": entry.line1,
                    "line2": entry.line2,
                    "epoch_utc_ns": int(entry.epoch_utc_ns),
                }
                for entry in sorted(entries, key=lambda item: item.norad_id)
            ],
        }
    )
    return data, {entry.norad_id: entry.epoch_utc_ns for entry in entries}


def _station(document: dict[str, object]) -> StationGeometrySnapshot:
    raw_position = document["station_position_m"]
    assert isinstance(raw_position, list)
    position = tuple(float(value) for value in raw_position)
    assert len(position) == 3
    station_id = StationId("station_rf_validation")
    identity = {
        "station_id": str(station_id),
        "frame": "ITRF",
        "position_m": position,
    }
    return StationGeometrySnapshot(
        station_id,
        "ITRF",
        (position[0], position[1], position[2]),
        canonical_digest(identity),
    )


def _link(
    recording_id: RecordingId,
    instant: UtcNs,
    snapshot_ref: EphemerisSnapshotRef,
) -> EphemerisLinkEvidence:
    interval = RecordingInterval(UtcNs(int(instant) - 1), UtcNs(int(instant) + 1))
    recording_digest = canonical_digest({"recording_id": str(recording_id)})
    policy_ref = _artifact("available-then-rf-validation-v1", {"version": 1})
    identity = {
        "recording_identity_digest": str(recording_digest),
        "recording_interval": interval,
        "source": EphemerisSource.SPACE_TRACK.value,
        "scope": "rf-validation",
        "policy": EphemerisSelectionPolicy.AVAILABLE_THEN.value,
        "policy_ref": policy_ref,
        "as_of_utc_ns": UtcNs(int(instant) + 2),
        "snapshot_ref": snapshot_ref,
    }
    link_digest = canonical_digest(identity)
    return EphemerisLinkEvidence(
        ArtifactRef(
            f"ephlink_{link_digest.value[:32]}",
            link_digest,
            SchemaRef("org.leo-flow.recording-ephemeris-link"),
        ),
        recording_id,
        recording_digest,
        interval,
        EphemerisSource.SPACE_TRACK,
        "rf-validation",
        EphemerisSelectionPolicy.AVAILABLE_THEN,
        policy_ref,
        UtcNs(int(instant) + 2),
        snapshot_ref,
    )


def _cases() -> tuple[
    ArtifactRef,
    tuple[AssociationValidationCase, ...],
    Sgp4OrbitPropagator,
]:
    fixture_bytes = FIXTURE.read_bytes()
    document = json.loads(fixture_bytes)
    assert isinstance(document, dict)
    catalog, epochs = _catalog(document)
    snapshot_ref = EphemerisSnapshotRef(
        EphemerisSnapshotId("eph_rf_validation"),
        EphemerisSource.SPACE_TRACK,
        Digest.sha256(fixture_bytes),
        Digest.sha256(catalog),
    )
    adapter = Sgp4OrbitPropagator(_Reader(snapshot_ref, catalog))
    propagation = sgp4_vallado_wgs72_specification()
    station = _station(document)
    calibration_raw = document["calibration"]
    injection_raw = document["injection"]
    assert isinstance(calibration_raw, dict)
    assert isinstance(injection_raw, dict)
    calibration = ReceiverRfCalibration(
        ReceiverChainId("rx_validation"),
        HardwareMetadataSnapshotRef(
            HardwareSnapshotId("hw_rf_validation"), Digest.sha256(fixture_bytes)
        ),
        station.station_id,
        float(calibration_raw["frequency_bias_hz"]),
        float(calibration_raw["frequency_drift_hz_s"]),
        float(calibration_raw["frequency_variance_hz2"]),
        float(calibration_raw["drift_variance_hz2_s2"]),
    )
    injection = DigitalInjectionSpecification(
        _artifact("digital-rf-injection-v1", injection_raw),
        int(injection_raw["seed"]),
        float(injection_raw["frequency_noise_span_hz"]),
        float(injection_raw["drift_noise_span_hz_s"]),
        float(injection_raw["speed_of_light_m_s"]),
    )
    carrier_hz = float(document["carrier_hz"])
    cases_raw = document["cases"]
    assert isinstance(cases_raw, list)
    cases: list[AssociationValidationCase] = []
    for index, raw in enumerate(cases_raw):
        assert isinstance(raw, dict)
        case_id = str(raw["case_id"])
        anchor_norad_id = int(raw["anchor_norad_id"])
        instant = UtcNs(
            int(epochs[anchor_norad_id])
            + int(raw["minutes_from_anchor_epoch"]) * 60_000_000_000
        )
        recording_id = RecordingId(f"rec_rf_validation_{index}")
        feature_ref = FeatureSetRef(
            FeatureSetId(f"fset_rf_validation_{index}"),
            AnalysisRunId(f"arun_rf_validation_{index}"),
            ObjectRef(
                canonical_digest({"case_id": case_id}),
                1,
                "application/json",
                "feature-v1",
                f"memory:{case_id}",
            ),
        )
        template = RfMeasurement(
            feature_ref,
            FeatureId(f"feature_rf_validation_{index}"),
            recording_id,
            calibration.receiver_chain_id,
            instant,
            carrier_hz,
            0.0,
            1.0,
            0.0004,
        )
        carriers = tuple(
            SatelliteCarrierHypothesis(int(norad_id), carrier_hz, 4.0)
            for norad_id in raw["candidate_norad_ids"]
        )
        if raw["expected"] == ValidationOutcome.PROPAGATION_ERROR.value:
            measurement = template
        else:
            truth_carrier = SatelliteCarrierHypothesis(anchor_norad_id, carrier_hz, 4.0)
            truth_state = adapter.propagate(
                snapshot_ref,
                station,
                propagation,
                anchor_norad_id,
                instant,
            )
            measurement = inject_synthetic_rf_measurement(
                template,
                truth_state,
                truth_carrier,
                calibration,
                injection,
                case_key=case_id,
                frequency_offset_hz=float(raw.get("frequency_offset_hz", 0.0)),
            )
        maximum_residual = 1_000_000_000.0 if raw["expected"] == "ambiguous" else 25.0
        request = RfAssociationRequest(
            _link(recording_id, instant, snapshot_ref),
            station,
            propagation,
            measurement,
            calibration,
            carriers,
            AssociationPolicy(
                _artifact("rf-association-validation-policy-v1", raw),
                float(raw["minimum_elevation_deg"]),
                maximum_residual,
                float(raw.get("ambiguity_delta", 0.1)),
            ),
        )
        expected = ValidationOutcome(str(raw["expected"]))
        cases.append(
            AssociationValidationCase(
                case_id,
                request,
                expected,
                anchor_norad_id if expected is ValidationOutcome.MATCH else None,
            )
        )
    experiment_ref = ArtifactRef(
        str(document["experiment_id"]), Digest.sha256(fixture_bytes)
    )
    return experiment_ref, tuple(cases), adapter


@REQUIRES_SGP4
def test_real_sgp4_and_rf_association_have_deterministic_confusion_output() -> None:
    experiment_ref, cases, adapter = _cases()

    first = run_association_validation(experiment_ref, cases, adapter)
    second = run_association_validation(experiment_ref, cases, adapter)

    assert first == second
    assert first.passed_count == first.total_count == 6
    assert [
        (cell.expected.value, cell.observed.value, cell.count)
        for cell in first.confusion
    ] == [
        ("ambiguous", "ambiguous", 1),
        ("below_elevation", "below_elevation", 1),
        ("match", "match", 2),
        ("no_match", "no_match", 1),
        ("propagation_error", "propagation_error", 1),
    ]
    assert first.to_document()["truth_scope"] == (
        "exact_digital_injection_not_observational_tle_truth"
    )


@REQUIRES_SGP4
def test_injection_is_counter_based_and_rejects_cross_paired_truth() -> None:
    _, cases, adapter = _cases()
    request = cases[0].request
    state = adapter.propagate(
        request.ephemeris_link.snapshot_ref,
        request.station,
        request.propagation,
        6251,
        request.measurement.midpoint_utc_ns,
    )
    specification = DigitalInjectionSpecification(
        _artifact("injection-test-v1", {"seed": 7}), 7, 1.0, 1.0
    )
    carrier = next(item for item in request.carriers if item.norad_id == 6251)

    first = inject_synthetic_rf_measurement(
        request.measurement,
        state,
        carrier,
        request.calibration,
        specification,
        case_key="stable_case",
    )
    assert first == inject_synthetic_rf_measurement(
        request.measurement,
        state,
        carrier,
        request.calibration,
        specification,
        case_key="stable_case",
    )
    assert first != inject_synthetic_rf_measurement(
        request.measurement,
        state,
        carrier,
        request.calibration,
        specification,
        case_key="different_case",
    )
    with pytest.raises(ValueError, match="identities differ"):
        inject_synthetic_rf_measurement(
            request.measurement,
            replace(state, norad_id=8195),
            carrier,
            request.calibration,
            specification,
            case_key="bad_pair",
        )
