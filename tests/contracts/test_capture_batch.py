from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.contracts.capture_batch import (
    CaptureAttemptOutcome,
    CaptureAttemptState,
    CaptureBatchDefinition,
    CaptureBatchMode,
    CaptureBatchSnapshot,
    ExpectedCaptureAttempt,
    PairedAnalysisEligibility,
)
from leo_flow.contracts.core import (
    CaptureAttemptId,
    CaptureBatchId,
    Digest,
    PlanId,
    RadioId,
    RecordingId,
    SchemaRef,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.storage import (
    ObjectRef,
    PublishedRecordingRef,
    RecordingObjectRef,
)


def _expected(suffix: str, *, requested_start_utc_ns: int) -> ExpectedCaptureAttempt:
    return ExpectedCaptureAttempt(
        CaptureAttemptId(f"cattempt_{suffix}"),
        RadioId(f"radio_{suffix}"),
        PlanId(f"plan_{suffix}"),
        UtcNs(requested_start_utc_ns),
    )


def _definition(
    mode: CaptureBatchMode = CaptureBatchMode.INDEPENDENT,
) -> CaptureBatchDefinition:
    start_b = 100 if mode is CaptureBatchMode.COORDINATED else 120
    return CaptureBatchDefinition(
        SchemaRef(CaptureBatchDefinition.SCHEMA_ID),
        CaptureBatchId("cbatch_test"),
        mode,
        (
            _expected("a", requested_start_utc_ns=100),
            _expected("b", requested_start_utc_ns=start_b),
        ),
        10 if mode is CaptureBatchMode.COORDINATED else None,
    )


def _published(suffix: str) -> PublishedRecordingRef:
    data_digest = Digest.sha256(f"{suffix}:data".encode())
    metadata_digest = Digest.sha256(f"{suffix}:metadata".encode())
    return PublishedRecordingRef(
        RecordingObjectRef(
            RecordingId(f"rec_{suffix}"),
            ObjectRef(
                data_digest,
                64,
                "application/octet-stream",
                "recording-data-v1",
                f"cas:sha256:{data_digest.value}",
            ),
            ObjectRef(
                metadata_digest,
                128,
                "application/json",
                "recording-metadata-v1",
                f"cas:sha256:{metadata_digest.value}",
            ),
            Digest.sha256(f"{suffix}:manifest".encode()),
        )
    )


def _success(
    definition: CaptureBatchDefinition, index: int, *, observed: int
) -> CaptureAttemptOutcome:
    expected = definition.expected_attempts[index]
    return CaptureAttemptOutcome(
        SchemaRef(CaptureAttemptOutcome.SCHEMA_ID),
        definition.batch_id,
        expected.attempt_id,
        expected.radio_id,
        expected.plan_id,
        CaptureAttemptState.SUCCEEDED,
        UtcNs(observed + 10),
        UtcNs(observed),
        _published(str(index)),
    )


def test_batch_definition_requires_two_canonical_distinct_attempts() -> None:
    definition = _definition()
    first, second = definition.expected_attempts
    assert definition.requested_start_skew_ns == 20

    with pytest.raises(ValueError, match="exactly two"):
        replace(definition, expected_attempts=(first,))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="canonical"):
        replace(definition, expected_attempts=(second, first))
    with pytest.raises(ValueError, match="radio IDs"):
        replace(
            definition,
            expected_attempts=(first, replace(second, radio_id=first.radio_id)),
        )


def test_mode_specific_timing_policy_fails_closed() -> None:
    independent = _definition()
    with pytest.raises(TypeError, match="CaptureBatchMode"):
        replace(independent, mode="independent")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="independent capture"):
        replace(independent, maximum_observed_start_skew_ns=10)

    coordinated = _definition(CaptureBatchMode.COORDINATED)
    first, second = coordinated.expected_attempts
    with pytest.raises(ValueError, match="one requested start"):
        replace(
            coordinated,
            expected_attempts=(
                first,
                replace(second, requested_start_utc_ns=UtcNs(101)),
            ),
        )
    with pytest.raises(ValueError, match="requires an observed skew limit"):
        replace(coordinated, maximum_observed_start_skew_ns=None)
    with pytest.raises(ValueError, match="non-negative integer"):
        replace(coordinated, maximum_observed_start_skew_ns=10.5)  # type: ignore[arg-type]


def test_terminal_outcome_requires_exact_success_or_failure_evidence() -> None:
    definition = _definition()
    success = _success(definition, 0, observed=100)
    with pytest.raises(ValueError, match="observed start and recording"):
        replace(success, recording_ref=None)
    with pytest.raises(ValueError, match="reason code"):
        replace(
            success,
            state=CaptureAttemptState.FAILED,
            recording_ref=None,
            failure_reason="Raw failure text is forbidden",
        )
    with pytest.raises(ValueError, match="terminates before"):
        replace(success, terminal_utc_ns=UtcNs(99))
    with pytest.raises(TypeError, match="CaptureAttemptState"):
        replace(success, state="succeeded")  # type: ignore[arg-type]


def test_snapshot_orders_outcomes_and_retains_successful_solo_evidence() -> None:
    definition = _definition()
    initial = CaptureBatchSnapshot(
        SchemaRef(CaptureBatchSnapshot.SCHEMA_ID), definition
    )
    success = _success(definition, 0, observed=100)
    peer = definition.expected_attempts[1]
    failed = CaptureAttemptOutcome(
        SchemaRef(CaptureAttemptOutcome.SCHEMA_ID),
        definition.batch_id,
        peer.attempt_id,
        peer.radio_id,
        peer.plan_id,
        CaptureAttemptState.FAILED,
        UtcNs(150),
        failure_reason="radio_disconnected",
    )

    terminal = initial.record(failed).record(success)

    assert tuple(item.attempt_id for item in terminal.outcomes) == tuple(
        item.attempt_id for item in definition.expected_attempts
    )
    assert terminal.terminal
    assert terminal.successful_recordings == (success.recording_ref,)
    assert terminal.paired_analysis_eligibility is PairedAnalysisEligibility.INELIGIBLE
    assert b"radio_disconnected" in canonical_json_bytes(terminal)
    with pytest.raises(ValueError, match="revision"):
        replace(terminal, revision=1)
