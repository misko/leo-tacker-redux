from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.capture.radio_lifecycle import (
    InMemoryRadioLifecycleFactRecorderV0_1,
    build_attempt_lifecycle_fact,
    build_interval_lifecycle_fact,
    classify_radio_lifecycle,
    lifecycle_dashboard_view,
)
from leo_flow.capture.radio_lifecycle_codec import (
    decode_attempt_lifecycle_fact,
    encode_attempt_lifecycle_fact,
)
from leo_flow.contracts.core import (
    V0_1,
    CaptureAttemptId,
    CaptureBatchId,
    RadioId,
    SchemaRef,
    UtcNs,
)
from leo_flow.contracts.radio_lifecycle import (
    Ad9361LifecycleIdentityV0_1,
    CaptureAttemptLifecycleDashboardViewV0_1,
    CaptureAttemptLifecycleFactV0_1,
    IiodProcessIdentityV0_1,
    RadioLifecycleConfidence,
    RadioLifecycleIntervalFactV0_1,
    RadioLifecycleObservationSource,
    RadioLifecycleObservationStatus,
    RadioLifecycleObservationV0_1,
    RadioLifecycleObserverUnavailableReason,
    RadioLifecycleReason,
    RadioLifecycleTrust,
    RadioTransportOutcome,
)

RADIO = RadioId("radio_test")
BOOT_A = "41974bfd-7aa8-4d28-b1c8-57d21c3e05bb"
BOOT_B = "d6f89d3a-6856-441f-83db-96c71728e15b"


def observation(
    *,
    observed: int = 10_000_000_000,
    boot: str = BOOT_A,
    uptime: int = 5_000_000_000,
    pid: int = 20,
    start_ticks: int = 1000,
    ad_epoch: int | None = 7,
) -> RadioLifecycleObservationV0_1:
    return RadioLifecycleObservationV0_1(
        SchemaRef(RadioLifecycleObservationV0_1.SCHEMA_ID, V0_1),
        RADIO,
        UtcNs(observed),
        RadioLifecycleObservationStatus.AVAILABLE,
        RadioLifecycleObservationSource.AUTHENTICATED_DIAGNOSTIC_V1,
        RadioLifecycleTrust.RADIO_AUTHENTICATED,
        boot,
        uptime,
        UtcNs(max(0, observed - uptime)),
        10_000_000,
        IiodProcessIdentityV0_1(pid, start_ticks, 100),
        None if ad_epoch is None else Ad9361LifecycleIdentityV0_1(ad_epoch),
    )


def unavailable(observed: int = 20_000_000_000) -> RadioLifecycleObservationV0_1:
    return RadioLifecycleObservationV0_1(
        SchemaRef(RadioLifecycleObservationV0_1.SCHEMA_ID, V0_1),
        RADIO,
        UtcNs(observed),
        RadioLifecycleObservationStatus.UNAVAILABLE,
        RadioLifecycleObservationSource.AUTHENTICATED_DIAGNOSTIC_V1,
        RadioLifecycleTrust.RADIO_AUTHENTICATED,
        None,
        None,
        None,
        None,
        None,
        None,
        RadioLifecycleObserverUnavailableReason.DEADLINE_EXCEEDED,
    )


def classify(before, after, outcome=RadioTransportOutcome.COMPLETE):
    return classify_radio_lifecycle(before, after, transport_outcome=outcome)


def test_same_boot_and_process_is_no_lifecycle_event() -> None:
    result = classify(
        observation(), observation(observed=20_000_000_000, uptime=15_000_000_000)
    )
    assert result.reason is None
    assert result.confidence is None


def test_mid_capture_boot_id_change_is_high_confidence_reboot() -> None:
    result = classify(
        observation(),
        observation(observed=20_000_000_000, boot=BOOT_B, uptime=2_000_000_000),
        RadioTransportOutcome.DISCONNECTED,
    )
    assert result.reason is RadioLifecycleReason.RADIO_REBOOTED
    assert result.confidence is RadioLifecycleConfidence.HIGH
    assert result.evidence_codes == ("boot_id_changed",)


def test_same_boot_iiod_process_identity_change_is_iiod_restart() -> None:
    result = classify(
        observation(),
        observation(
            observed=20_000_000_000, uptime=15_000_000_000, pid=21, start_ticks=1900
        ),
    )
    assert result.reason is RadioLifecycleReason.IIOD_RESTARTED
    assert result.confidence is RadioLifecycleConfidence.HIGH


def test_pid_reuse_is_detected_by_proc_start_ticks() -> None:
    result = classify(
        observation(),
        observation(
            observed=20_000_000_000, uptime=15_000_000_000, pid=20, start_ticks=1900
        ),
    )
    assert result.reason is RadioLifecycleReason.IIOD_RESTARTED


def test_same_boot_and_iiod_ad9361_epoch_change_is_reinitialization() -> None:
    result = classify(
        observation(),
        observation(observed=20_000_000_000, uptime=15_000_000_000, ad_epoch=8),
    )
    assert result.reason is RadioLifecycleReason.AD9361_REINITIALIZED
    assert result.confidence is RadioLifecycleConfidence.HIGH


@pytest.mark.parametrize(
    "outcome", [RadioTransportOutcome.TIMEOUT, RadioTransportOutcome.DISCONNECTED]
)
def test_unavailable_observer_plus_transport_failure_is_unknown_not_reboot(
    outcome,
) -> None:
    result = classify(observation(), unavailable(), outcome)
    assert result.reason is RadioLifecycleReason.TRANSPORT_TIMEOUT_UNKNOWN
    assert result.confidence is RadioLifecycleConfidence.LOW
    assert "lifecycle_observer_unavailable" in result.evidence_codes


def test_transport_timeout_with_unchanged_identity_remains_unknown() -> None:
    result = classify(
        observation(),
        observation(observed=20_000_000_000, uptime=15_000_000_000),
        RadioTransportOutcome.TIMEOUT,
    )
    assert result.reason is RadioLifecycleReason.TRANSPORT_TIMEOUT_UNKNOWN
    assert result.confidence is RadioLifecycleConfidence.LOW


def test_observer_unavailable_without_transport_failure_makes_no_claim() -> None:
    result = classify(observation(), unavailable())
    assert result.reason is None


def test_uptime_regression_wrap_and_wall_clock_skew_do_not_fake_reboot() -> None:
    before = observation(observed=20_000_000_000, uptime=19_000_000_000)
    after = replace(
        observation(observed=21_000_000_000, uptime=10),
        estimated_boot_utc_ns=UtcNs(20_999_999_990),
    )
    assert classify(before, after).reason is None


def test_between_slot_change_is_an_immutable_interval_fact() -> None:
    result = build_interval_lifecycle_fact(
        schema=SchemaRef(RadioLifecycleIntervalFactV0_1.SCHEMA_ID, V0_1),
        radio_id=RADIO,
        previous_attempt_id=CaptureAttemptId("cattempt_previous"),
        current_attempt_id=CaptureAttemptId("cattempt_current"),
        previous_terminal=observation(),
        current_preflight=observation(observed=20_000_000_000, boot=BOOT_B, uptime=1),
    )
    assert result.diagnosis.reason is RadioLifecycleReason.RADIO_REBOOTED


def attempt_fact(after=None, outcome=RadioTransportOutcome.COMPLETE):
    return build_attempt_lifecycle_fact(
        schema=SchemaRef(CaptureAttemptLifecycleFactV0_1.SCHEMA_ID, V0_1),
        batch_id=CaptureBatchId("cbatch_test"),
        attempt_id=CaptureAttemptId("cattempt_test"),
        radio_id=RADIO,
        preflight=observation(),
        terminal=after or observation(observed=20_000_000_000, uptime=15_000_000_000),
        transport_outcome=outcome,
    )


def test_exact_replay_is_idempotent_and_conflicting_replay_is_rejected() -> None:
    recorder = InMemoryRadioLifecycleFactRecorderV0_1()
    first = attempt_fact()
    assert recorder.record_attempt(first) is first
    assert recorder.record_attempt(first) is first
    conflicting = attempt_fact(
        observation(observed=20_000_000_000, boot=BOOT_B, uptime=1),
        RadioTransportOutcome.DISCONNECTED,
    )
    with pytest.raises(ValueError, match="already differs"):
        recorder.record_attempt(conflicting)


def test_dashboard_view_exposes_only_bounded_lifecycle_fields() -> None:
    fact = attempt_fact(
        observation(observed=20_000_000_000, boot=BOOT_B, uptime=1),
        RadioTransportOutcome.DISCONNECTED,
    )
    view = lifecycle_dashboard_view(
        fact,
        schema=SchemaRef(CaptureAttemptLifecycleDashboardViewV0_1.SCHEMA_ID, V0_1),
    )
    assert view.reason is RadioLifecycleReason.RADIO_REBOOTED
    assert view.preflight_boot_id == BOOT_A
    assert view.terminal_boot_id == BOOT_B
    assert view.observer_available_at_terminal


def test_attempt_fact_canonical_codec_round_trips_exactly() -> None:
    value = attempt_fact(
        observation(observed=20_000_000_000, boot=BOOT_B, uptime=1),
        RadioTransportOutcome.DISCONNECTED,
    )
    encoded = encode_attempt_lifecycle_fact(value)
    assert decode_attempt_lifecycle_fact(encoded) == value
    for forbidden in (b"password", b"stderr", b"command", b"traceback"):
        assert forbidden not in encoded
