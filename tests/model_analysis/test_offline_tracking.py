from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from leo_flow.analysis.orbit import (
    AssociationCandidate,
    AssociationDecision,
    AssociationPolicy,
    AssociationStatus,
    EphemerisLinkEvidence,
    PropagationSpecification,
    ReceiverRfCalibration,
    RfAssociationRequest,
    RfMeasurement,
    SatelliteCarrierHypothesis,
    StationGeometrySnapshot,
)
from leo_flow.analysis.tracking import (
    AssociatedTrackingObservation,
    SyntheticTrackingSpecification,
    TrackingSpecification,
    inject_synthetic_tracking_observation,
    synthetic_truth_state,
    track_associated_observations,
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

FIXTURE = Path(__file__).with_name("fixtures") / "tracking_synthetic_v1.json"
BASE_NS = 2_000_000_000_000
NORAD_ID = 42_001


def _artifact(name: str) -> ArtifactRef:
    return ArtifactRef(name, digest(name))


def _observation(
    index: int,
    *,
    seconds: float,
    status: AssociationStatus = AssociationStatus.MATCH,
    selected_norad_id: int | None = NORAD_ID,
    receiver: str = "rx_tracking",
    hardware: str = "hw_tracking",
    station: str = "station_tracking",
    reason_codes: tuple[str, ...] = (),
) -> AssociatedTrackingObservation:
    instant = UtcNs(BASE_NS + round(seconds * 1_000_000_000))
    recording_id = RecordingId(f"rec_tracking_{index}")
    station_id = StationId(station)
    station_identity = {
        "station_id": str(station_id),
        "frame": "ITRF",
        "position_m": (1.0, 2.0, 3.0),
    }
    station_snapshot = StationGeometrySnapshot(
        station_id,
        "ITRF",
        (1.0, 2.0, 3.0),
        canonical_digest(station_identity),
    )
    feature_ref = FeatureSetRef(
        FeatureSetId(f"fset_tracking_{index}"),
        AnalysisRunId(f"arun_tracking_{index}"),
        ObjectRef(
            digest(f"feature-{index}"),
            1,
            "application/json",
            "feature-v1",
            f"memory:feature-{index}",
        ),
    )
    measurement = RfMeasurement(
        feature_ref,
        FeatureId(f"feature_tracking_{index}"),
        recording_id,
        ReceiverChainId(receiver),
        instant,
        1000.0,
        -2.0,
        1.0,
        0.04,
    )
    snapshot_ref = EphemerisSnapshotRef(
        EphemerisSnapshotId("eph_tracking"),
        EphemerisSource.SPACE_TRACK,
        digest("raw-tracking"),
        digest("normalized-tracking"),
    )
    interval = RecordingInterval(UtcNs(int(instant) - 1), UtcNs(int(instant) + 1))
    recording_digest = canonical_digest({"recording_id": str(recording_id)})
    policy_ref = _artifact("selection-policy-tracking-v1")
    link_identity = {
        "recording_identity_digest": str(recording_digest),
        "recording_interval": interval,
        "source": EphemerisSource.SPACE_TRACK.value,
        "scope": "tracking-validation",
        "policy": EphemerisSelectionPolicy.AVAILABLE_THEN.value,
        "policy_ref": policy_ref,
        "as_of_utc_ns": UtcNs(int(instant) + 2),
        "snapshot_ref": snapshot_ref,
    }
    link_digest = canonical_digest(link_identity)
    link = EphemerisLinkEvidence(
        ArtifactRef(
            f"ephlink_{link_digest.value[:32]}",
            link_digest,
            SchemaRef("org.leo-flow.recording-ephemeris-link"),
        ),
        recording_id,
        recording_digest,
        interval,
        EphemerisSource.SPACE_TRACK,
        "tracking-validation",
        EphemerisSelectionPolicy.AVAILABLE_THEN,
        policy_ref,
        UtcNs(int(instant) + 2),
        snapshot_ref,
    )
    hardware_ref = HardwareMetadataSnapshotRef(
        HardwareSnapshotId(hardware), digest(hardware)
    )
    request = RfAssociationRequest(
        link,
        station_snapshot,
        PropagationSpecification(
            _artifact("propagator-tracking-v1"),
            _artifact("gravity-tracking-v1"),
            _artifact("time-tracking-v1"),
            _artifact("eop-tracking-v1"),
            _artifact("error-tracking-v1"),
        ),
        measurement,
        ReceiverRfCalibration(
            ReceiverChainId(receiver),
            hardware_ref,
            station_id,
            0.0,
            0.0,
            0.0,
            0.0,
        ),
        (SatelliteCarrierHypothesis(NORAD_ID, 1_000.0, 0.0),),
        AssociationPolicy(_artifact("association-tracking-v1"), 5.0, 25.0, 0.1),
    )
    candidates = (AssociationCandidate(NORAD_ID, 0.0, 1_000.0, -2.0, 35.0),)
    decision = AssociationDecision(
        status,
        selected_norad_id,
        candidates if status is not AssociationStatus.NO_MATCH else (),
        canonical_digest(request),
        reason_codes,
    )
    return AssociatedTrackingObservation(decision, request, 0.5, 0.01)


def _tracking_spec(
    *,
    initial_covariance: tuple[tuple[float, float], tuple[float, float]] = (
        (100.0, 0.0),
        (0.0, 1.0),
    ),
    maximum_normalized_innovation_squared: float = 30.0,
    enable_rts_smoothing: bool = True,
    segment_on_context_change: bool = True,
) -> TrackingSpecification:
    return TrackingSpecification(
        _artifact("experimental-tracker-v1"),
        NORAD_ID,
        (0.0, 0.0),
        initial_covariance,
        0.002,
        maximum_normalized_innovation_squared,
        20.0,
        segment_on_context_change=segment_on_context_change,
        enable_rts_smoothing=enable_rts_smoothing,
    )


def test_seeded_independent_software_truth_is_recovered_with_reported_uncertainty() -> (
    None
):
    document = json.loads(FIXTURE.read_text())
    injection = SyntheticTrackingSpecification(
        _artifact("tracking-injection-v1"),
        document["seed"],
        document["initial_frequency_residual_hz"],
        document["initial_drift_residual_hz_s"],
        document["drift_acceleration_hz_s2"],
        document["frequency_noise_span_hz"],
        document["drift_noise_span_hz_s"],
    )
    observations = tuple(
        inject_synthetic_tracking_observation(
            _observation(index, seconds=index * document["sample_period_s"]),
            injection,
            elapsed_s=index * document["sample_period_s"],
            case_key=f"sample-{index}",
        )
        for index in range(document["sample_count"])
    )

    report = track_associated_observations(observations, _tracking_spec())
    repeated = track_associated_observations(observations, _tracking_spec())

    assert report == repeated
    assert report.experimental and report.software_truth_only
    assert len(report.segments) == 1
    estimates = report.segments[0].estimates
    assert all(estimate.normalized_innovation_squared <= 30.0 for estimate in estimates)
    assert all(estimate.smoothed_state is not None for estimate in estimates)
    for index, estimate in enumerate(estimates):
        truth = synthetic_truth_state(injection, index * document["sample_period_s"])
        assert estimate.smoothed_state is not None
        assert (
            abs(estimate.smoothed_state[0] - truth[0])
            <= document["maximum_smoothed_frequency_error_hz"]
        )
        assert (
            abs(estimate.smoothed_state[1] - truth[1])
            <= document["maximum_smoothed_drift_error_hz_s"]
        )
        assert estimate.smoothed_covariance is not None
        assert estimate.smoothed_covariance[0][0] > 0.0
        assert estimate.smoothed_covariance[1][1] > 0.0


def test_exact_first_update_reports_basis_innovation_gate_and_covariance() -> None:
    observation = _observation(0, seconds=0.0)
    request = replace(
        observation.request,
        measurement=replace(
            observation.request.measurement,
            frequency_hz=1010.0,
            drift_hz_s=0.0,
        ),
    )
    observation = replace(
        observation,
        request=request,
        decision=replace(
            observation.decision, request_digest=canonical_digest(request)
        ),
        prediction_frequency_variance_hz2=0.0,
        prediction_drift_variance_hz2_s2=0.96,
    )
    specification = _tracking_spec(
        initial_covariance=((4.0, 0.0), (0.0, 1.0)),
        maximum_normalized_innovation_squared=25.0,
        enable_rts_smoothing=False,
    )

    estimate = (
        track_associated_observations((observation,), specification)
        .segments[0]
        .estimates[0]
    )

    assert estimate.state_basis == (
        "frequency_residual_hz",
        "frequency_drift_residual_hz_s",
    )
    assert estimate.innovation == (10.0, 2.0)
    assert estimate.innovation_covariance == ((5.0, 0.0), (0.0, 2.0))
    assert estimate.normalized_innovation_squared == 22.0
    assert estimate.filtered_state == (8.0, 1.0)
    assert estimate.filtered_covariance[0][0] == pytest.approx(0.8)
    assert estimate.filtered_covariance[1][1] == pytest.approx(0.5)


@pytest.mark.parametrize(
    ("receiver", "hardware", "station"),
    [
        ("rx_replaced", "hw_tracking", "station_tracking"),
        ("rx_tracking", "hw_replaced", "station_tracking"),
        ("rx_tracking", "hw_tracking", "station_replaced"),
    ],
)
def test_context_and_gap_changes_create_explicit_segments(
    receiver: str, hardware: str, station: str
) -> None:
    observations = (
        _observation(0, seconds=0.0),
        _observation(
            1,
            seconds=5.0,
            receiver=receiver,
            hardware=hardware,
            station=station,
        ),
        _observation(
            2,
            seconds=40.0,
            receiver=receiver,
            hardware=hardware,
            station=station,
        ),
    )

    report = track_associated_observations(observations, _tracking_spec())

    assert [segment.termination_reason for segment in report.segments] == [
        "context-change",
        "excessive-gap",
        "end-of-input",
    ]
    assert [segment.start_reason for segment in report.segments] == [
        "first-accepted-observation",
        "after-context-change",
        "after-excessive-gap",
    ]


@pytest.mark.parametrize(
    ("status", "selected", "reasons", "expected"),
    [
        (AssociationStatus.AMBIGUOUS, None, (), "ambiguous-association"),
        (AssociationStatus.NO_MATCH, None, (), "no-match-association"),
        (
            AssociationStatus.NO_MATCH,
            None,
            ("propagation-error:42001:decayed",),
            "propagation-error",
        ),
        (AssociationStatus.MATCH, 99_999, (), "norad-identity-mismatch"),
    ],
)
def test_invalid_associations_are_rejected_and_never_switch_identity(
    status: AssociationStatus,
    selected: int | None,
    reasons: tuple[str, ...],
    expected: str,
) -> None:
    report = track_associated_observations(
        (
            _observation(0, seconds=0.0),
            _observation(
                1,
                seconds=5.0,
                status=status,
                selected_norad_id=selected,
                reason_codes=reasons,
            ),
            _observation(2, seconds=10.0),
        ),
        _tracking_spec(),
    )

    assert [item.reason_code for item in report.rejected] == [expected]
    assert len(report.segments) == 2
    assert all(segment.norad_id == NORAD_ID for segment in report.segments)


def test_substituted_request_and_non_monotonic_time_fail_closed() -> None:
    observation = _observation(0, seconds=0.0)
    substituted = _observation(1, seconds=5.0).request
    with pytest.raises(ValueError, match="exact request digest differ"):
        replace(observation, request=substituted)

    with pytest.raises(ValueError, match="strictly increasing"):
        track_associated_observations(
            (_observation(1, seconds=5.0), _observation(0, seconds=0.0)),
            _tracking_spec(),
        )


def test_innovation_outlier_and_unmodeled_context_change_are_rejected() -> None:
    normal = _observation(0, seconds=0.0)
    changed = _observation(1, seconds=5.0, receiver="rx_other")
    outlier = _observation(2, seconds=10.0)
    request = replace(
        outlier.request,
        measurement=replace(outlier.request.measurement, frequency_hz=100_000.0),
    )
    outlier = replace(
        outlier,
        request=request,
        decision=replace(outlier.decision, request_digest=canonical_digest(request)),
    )
    report = track_associated_observations(
        (normal, changed, outlier),
        _tracking_spec(segment_on_context_change=False),
    )

    assert [item.reason_code for item in report.rejected] == [
        "context-change",
        "innovation-gate",
    ]
    assert len(report.segments) == 1
