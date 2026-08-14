from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.analysis.orbit import (
    AssociationPolicy,
    AssociationStatus,
    DeterministicOrbitSimulator,
    EphemerisLinkEvidence,
    PropagatedState,
    PropagationSpecification,
    ReceiverRfCalibration,
    RfAssociationRequest,
    RfMeasurement,
    SatelliteCarrierHypothesis,
    StationGeometrySnapshot,
    associate_rf_measurement,
)
from leo_flow.contracts.core import (
    AnalysisRunId,
    ArtifactRef,
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
from testkit import digest

TIME = UtcNs(1_000)


def _artifact(name: str) -> ArtifactRef:
    return ArtifactRef(name, digest(name))


def _request(
    states: tuple[PropagatedState, ...],
    *,
    measurement_frequency: float = 990.0,
    frequency_variance: float = 1.0,
    ambiguity_delta: float = 0.1,
):
    station_identity = {
        "station_id": "station_test",
        "frame": "ITRF",
        "position_m": (1.0, 2.0, 3.0),
    }
    station = StationGeometrySnapshot(
        StationId("station_test"),
        "ITRF",
        (1.0, 2.0, 3.0),
        canonical_digest(station_identity),
    )
    object_ref = ObjectRef(
        digest("features"), 10, "application/json", "feature-v1", "memory:features"
    )
    feature_ref = FeatureSetRef(
        FeatureSetId("fset_orbit"), AnalysisRunId("arun_orbit"), object_ref
    )
    snapshot_ref = EphemerisSnapshotRef(
        EphemerisSnapshotId("eph_orbit"),
        EphemerisSource.HUGGING_FACE,
        digest("raw"),
        digest("normalized"),
    )
    propagation = PropagationSpecification(
        _artifact("sgp4-implementation-v1"),
        _artifact("wgs72-constants-v1"),
        _artifact("utc-timescale-v1"),
        _artifact("eop-fixture-v1"),
        _artifact("sgp4-error-policy-v1"),
        speed_of_light_m_s=1_000.0,
    )
    measurement = RfMeasurement(
        feature_ref,
        FeatureId("feature_orbit"),
        RecordingId("rec_orbit"),
        ReceiverChainId("rx_0"),
        TIME,
        measurement_frequency,
        -2.0,
        frequency_variance,
        1.0,
    )
    calibration = ReceiverRfCalibration(
        ReceiverChainId("rx_0"),
        HardwareMetadataSnapshotRef(HardwareSnapshotId("hw_orbit"), digest("hw")),
        StationId("station_test"),
        0.0,
        0.0,
        0.0,
        0.0,
    )
    policy_ref = _artifact("temporal-policy-v1")
    link_identity = {
        "recording_identity_digest": str(digest("recording")),
        "recording_interval": RecordingInterval(UtcNs(900), UtcNs(1_100)),
        "source": EphemerisSource.HUGGING_FACE.value,
        "scope": "starlink",
        "policy": EphemerisSelectionPolicy.AVAILABLE_THEN.value,
        "policy_ref": policy_ref,
        "as_of_utc_ns": UtcNs(1_200),
        "snapshot_ref": snapshot_ref,
    }
    link_digest = canonical_digest(link_identity)
    link = EphemerisLinkEvidence(
        ArtifactRef(
            f"ephlink_{link_digest.value[:32]}",
            link_digest,
            SchemaRef("org.leo-flow.recording-ephemeris-link"),
        ),
        RecordingId("rec_orbit"),
        digest("recording"),
        RecordingInterval(UtcNs(900), UtcNs(1_100)),
        EphemerisSource.HUGGING_FACE,
        "starlink",
        EphemerisSelectionPolicy.AVAILABLE_THEN,
        policy_ref,
        UtcNs(1_200),
        snapshot_ref,
    )
    request = RfAssociationRequest(
        link,
        station,
        propagation,
        measurement,
        calibration,
        tuple(
            SatelliteCarrierHypothesis(state.norad_id, 1_000.0, 0.0) for state in states
        ),
        AssociationPolicy(
            _artifact("association-policy-v1"), 5.0, 25.0, ambiguity_delta
        ),
    )
    return request, DeterministicOrbitSimulator(states)


def _state(norad_id: int, rate: float, *, elevation: float = 30.0, error=None):
    return PropagatedState(norad_id, TIME, rate, 2.0, elevation, error)


def test_exact_pins_and_uncertainty_weighted_score_select_best_candidate() -> None:
    request, simulator = _request((_state(10, 10.0), _state(20, 13.0)))

    decision = associate_rf_measurement(request, simulator)

    assert decision.status is AssociationStatus.MATCH
    assert decision.selected_norad_id == 10
    assert [candidate.norad_id for candidate in decision.candidates] == [10, 20]
    assert decision.candidates[0].normalized_squared_residual == 0.0
    assert decision.request_digest == canonical_digest(request)


def test_uncertainty_changes_residual_gate_without_changing_prediction() -> None:
    narrow, simulator = _request(
        (_state(10, 10.0),), measurement_frequency=996.0, frequency_variance=1.0
    )
    wide = replace(
        narrow,
        measurement=replace(narrow.measurement, frequency_variance_hz2=4.0),
    )

    assert (
        associate_rf_measurement(narrow, simulator).status is AssociationStatus.NO_MATCH
    )
    accepted = associate_rf_measurement(wide, simulator)
    assert accepted.status is AssociationStatus.MATCH
    assert accepted.candidates[0].predicted_frequency_hz == 990.0


def test_elevation_and_propagation_errors_are_explicit_gates() -> None:
    request, simulator = _request(
        (_state(10, 10.0, elevation=1.0), _state(20, 10.0, error="decayed"))
    )

    decision = associate_rf_measurement(request, simulator)

    assert decision.status is AssociationStatus.NO_MATCH
    assert decision.reason_codes == (
        "below-elevation-gate:10",
        "propagation-error:20:decayed",
    )


def test_equal_candidates_are_ambiguous_with_deterministic_norad_order() -> None:
    request, simulator = _request(
        (_state(20, 10.0), _state(10, 10.0)), ambiguity_delta=0.0
    )

    decision = associate_rf_measurement(request, simulator)

    assert decision.status is AssociationStatus.AMBIGUOUS
    assert decision.selected_norad_id is None
    assert [candidate.norad_id for candidate in decision.candidates] == [10, 20]
    assert "ambiguous-best-candidates" in decision.reason_codes


def test_simulator_requires_exact_norad_and_time_key() -> None:
    request, simulator = _request((_state(10, 10.0),))
    changed = replace(
        request,
        measurement=replace(request.measurement, midpoint_utc_ns=UtcNs(TIME + 1)),
    )
    try:
        associate_rf_measurement(changed, simulator)
    except LookupError as error:
        assert "exact orbit state" in str(error)
    else:
        raise AssertionError("missing exact state should fail closed")


def test_link_snapshot_and_hardware_station_cannot_be_cross_paired() -> None:
    request, _ = _request((_state(10, 10.0),))
    with pytest.raises(ValueError, match="artifact identity"):
        replace(
            request.ephemeris_link,
            snapshot_ref=replace(
                request.ephemeris_link.snapshot_ref,
                normalized_digest=digest("another-snapshot"),
            ),
        )
    with pytest.raises(ValueError, match="hardware station"):
        replace(
            request,
            calibration=replace(
                request.calibration, station_id=StationId("station_other")
            ),
        )
