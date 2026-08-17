from __future__ import annotations

import pytest

from leo_flow.contracts.core import (
    V0_1,
    CaptureAttemptId,
    CaptureBatchId,
    RadioId,
    SchemaRef,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.radio_lifecycle import (
    Ad9361LifecycleIdentityV0_1,
    CaptureAttemptLifecycleFactV0_1,
    CaptureBatchLifecycleFactV0_1,
    IiodProcessIdentityV0_1,
    RadioLifecycleConfidence,
    RadioLifecycleDiagnosisV0_1,
    RadioLifecycleObservationSource,
    RadioLifecycleObservationStatus,
    RadioLifecycleObservationV0_1,
    RadioLifecycleObserverUnavailableReason,
    RadioLifecycleReason,
    RadioLifecycleTrust,
    RadioTransportOutcome,
)

RADIO = RadioId("radio_test_a")


def observation(
    *,
    radio_id: RadioId = RADIO,
    observed: int = 2_000_000_000,
    boot_id: str = "41974bfd-7aa8-4d28-b1c8-57d21c3e05bb",
    uptime: int = 1_000_000_000,
    pid: int = 41,
    start_ticks: int = 100,
    ad_epoch: int | None = 2,
) -> RadioLifecycleObservationV0_1:
    return RadioLifecycleObservationV0_1(
        SchemaRef(RadioLifecycleObservationV0_1.SCHEMA_ID, V0_1),
        radio_id,
        UtcNs(observed),
        RadioLifecycleObservationStatus.AVAILABLE,
        RadioLifecycleObservationSource.AUTHENTICATED_DIAGNOSTIC_V1,
        RadioLifecycleTrust.RADIO_AUTHENTICATED,
        boot_id,
        uptime,
        UtcNs(max(0, observed - uptime)),
        5_000_000,
        IiodProcessIdentityV0_1(pid, start_ticks, 100),
        None if ad_epoch is None else Ad9361LifecycleIdentityV0_1(ad_epoch),
    )


def unavailable(*, observed: int = 3_000_000_000) -> RadioLifecycleObservationV0_1:
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


def fact(attempt: str, radio: str) -> CaptureAttemptLifecycleFactV0_1:
    radio_id = RadioId(radio)
    before = observation(radio_id=radio_id)
    after = observation(radio_id=radio_id, observed=3_000_000_000, uptime=2_000_000_000)
    return CaptureAttemptLifecycleFactV0_1(
        SchemaRef(CaptureAttemptLifecycleFactV0_1.SCHEMA_ID, V0_1),
        CaptureBatchId("cbatch_test"),
        CaptureAttemptId(attempt),
        radio_id,
        before,
        after,
        RadioTransportOutcome.COMPLETE,
        RadioLifecycleDiagnosisV0_1(None, None, ()),
    )


def test_available_observation_is_canonical_and_contains_no_raw_diagnostics() -> None:
    encoded = canonical_json_bytes(observation())
    assert b'"boot_id":"41974bfd-7aa8-4d28-b1c8-57d21c3e05bb"' in encoded
    for forbidden in (b"password", b"stderr", b"journal", b"command", b"traceback"):
        assert forbidden not in encoded


def test_available_observation_requires_canonical_boot_uuid_and_full_identity() -> None:
    with pytest.raises(ValueError, match="canonical UUID"):
        observation(boot_id="41974BFD-7AA8-4D28-B1C8-57D21C3E05BB")
    value = observation()
    with pytest.raises(ValueError, match="complete OS identity"):
        RadioLifecycleObservationV0_1(
            value.schema,
            value.radio_id,
            value.observed_utc_ns,
            value.status,
            value.source,
            value.trust,
            value.boot_id,
            value.uptime_ns,
            value.estimated_boot_utc_ns,
            value.boot_time_uncertainty_ns,
            None,
        )


def test_unavailable_observation_cannot_smuggle_identity_or_raw_reason() -> None:
    value = unavailable()
    with pytest.raises(ValueError, match="cannot claim identity"):
        RadioLifecycleObservationV0_1(
            value.schema,
            value.radio_id,
            value.observed_utc_ns,
            value.status,
            value.source,
            value.trust,
            "41974bfd-7aa8-4d28-b1c8-57d21c3e05bb",
            None,
            None,
            None,
            None,
            None,
            RadioLifecycleObserverUnavailableReason.DEADLINE_EXCEEDED,
        )
    with pytest.raises(TypeError, match="fixed observer reason"):
        RadioLifecycleObservationV0_1(
            value.schema,
            value.radio_id,
            value.observed_utc_ns,
            value.status,
            value.source,
            value.trust,
            None,
            None,
            None,
            None,
            None,
            None,
            "ssh: secret traceback",
        )


def test_batch_fact_is_exactly_two_unique_canonically_ordered_attempts() -> None:
    first = fact("cattempt_a", "radio_a")
    second = fact("cattempt_b", "radio_b")
    result = CaptureBatchLifecycleFactV0_1(
        SchemaRef(CaptureBatchLifecycleFactV0_1.SCHEMA_ID, V0_1),
        CaptureBatchId("cbatch_test"),
        (first, second),
    )
    assert result.attempts == (first, second)
    with pytest.raises(ValueError, match="canonical"):
        CaptureBatchLifecycleFactV0_1(result.schema, result.batch_id, (second, first))


def test_public_contract_rejects_in_place_minor_or_major_change() -> None:
    value = observation()
    with pytest.raises(ValueError, match="unsupported"):
        RadioLifecycleObservationV0_1(
            SchemaRef(value.SCHEMA_ID, type(V0_1)(0, 2)),
            value.radio_id,
            value.observed_utc_ns,
            value.status,
            value.source,
            value.trust,
            value.boot_id,
            value.uptime_ns,
            value.estimated_boot_utc_ns,
            value.boot_time_uncertainty_ns,
            value.iiod,
            value.ad9361,
        )


def test_attempt_fact_rejects_a_diagnosis_that_contradicts_identity_evidence() -> None:
    value = fact("cattempt_a", "radio_a")
    with pytest.raises(ValueError, match="contradicts"):
        CaptureAttemptLifecycleFactV0_1(
            value.schema,
            value.batch_id,
            value.attempt_id,
            value.radio_id,
            value.preflight,
            value.terminal,
            value.transport_outcome,
            RadioLifecycleDiagnosisV0_1(
                RadioLifecycleReason.RADIO_REBOOTED,
                RadioLifecycleConfidence.HIGH,
                ("boot_id_changed",),
            ),
        )


def test_observation_rejects_boot_time_inconsistent_with_utc_and_uptime() -> None:
    value = observation()
    with pytest.raises(ValueError, match="contradicts"):
        RadioLifecycleObservationV0_1(
            value.schema,
            value.radio_id,
            value.observed_utc_ns,
            value.status,
            value.source,
            value.trust,
            value.boot_id,
            value.uptime_ns,
            UtcNs(0),
            1,
            value.iiod,
            value.ad9361,
        )


def test_observation_rejects_source_trust_mismatch() -> None:
    value = observation()
    with pytest.raises(ValueError, match="source and trust authority"):
        RadioLifecycleObservationV0_1(
            value.schema,
            value.radio_id,
            value.observed_utc_ns,
            value.status,
            value.source,
            RadioLifecycleTrust.HOST_AUTHENTICATED,
            value.boot_id,
            value.uptime_ns,
            value.estimated_boot_utc_ns,
            value.boot_time_uncertainty_ns,
            value.iiod,
            value.ad9361,
        )
