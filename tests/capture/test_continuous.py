from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from leo_flow.adapters.continuous_collection_sqlite import (
    SQLiteContinuousCollectionJournal,
)
from leo_flow.capture.campaign import (
    SLOT_PERIOD_DENOMINATOR,
    SLOT_PERIOD_NUMERATOR_NS,
)
from leo_flow.capture.continuous import (
    ContinuousCollectionHaltReason,
    ContinuousCollectionPhase,
    ContinuousCollectionRecordPhase,
    ContinuousCollectionStatus,
    DeferredCampaignCoordinator,
    InMemoryContinuousCollectionJournal,
)
from leo_flow.contracts.core import UtcNs
from tests.capture.test_campaign import (
    START,
    _Analysis,
    _Capacity,
    _Capture,
    _main_definition,
)


def _coordinator(
    journal: InMemoryContinuousCollectionJournal | SQLiteContinuousCollectionJournal,
    capture: _Capture,
    analysis: _Analysis,
) -> DeferredCampaignCoordinator:
    definition, receipt = _main_definition()
    definition = replace(definition, analysis_after_each_capture=False)
    return DeferredCampaignCoordinator(
        definition, journal, capture, analysis, _Capacity(), 0, receipt
    )


def test_collects_fresh_dual_identities_without_invoking_analysis_then_drains() -> None:
    capture = _Capture()
    analysis = _Analysis()
    coordinator = _coordinator(InMemoryContinuousCollectionJournal(), capture, analysis)

    first = coordinator.capture_next(UtcNs(START))
    second_start = START + SLOT_PERIOD_NUMERATOR_NS // SLOT_PERIOD_DENOMINATOR
    second = coordinator.capture_next(UtcNs(second_start - 15_000_000_000))

    assert first.status is second.status is ContinuousCollectionStatus.CAPTURED
    assert analysis.calls == []
    assert first.unit is not None and second.unit is not None
    assert first.unit.batch.batch_id != second.unit.batch.batch_id
    assert first.unit.plan_a_id != second.unit.plan_a_id
    assert first.unit.plan_b_id != second.unit.plan_b_id
    assert second.state.captured_count == 2

    closed = coordinator.close_capture()
    blocked_capture = coordinator.capture_next(UtcNs(second_start))
    analyzed_a = coordinator.analyze_next(deadline_utc_ns=UtcNs(second_start + 10_000))
    analyzed_b = coordinator.analyze_next(deadline_utc_ns=UtcNs(second_start + 20_000))
    complete = coordinator.analyze_next(deadline_utc_ns=UtcNs(second_start + 30_000))

    assert closed.state.phase is ContinuousCollectionPhase.ANALYZING
    assert blocked_capture.status is ContinuousCollectionStatus.CAPTURE_PHASE_CLOSED
    assert analyzed_a.status is analyzed_b.status is ContinuousCollectionStatus.ANALYZED
    assert complete.status is ContinuousCollectionStatus.COMPLETE
    assert complete.state.analyzed_count == 2
    assert len(capture.calls) == len(analysis.calls) == 2


def test_analysis_is_inaccessible_until_capture_is_durably_closed() -> None:
    analysis = _Analysis()
    coordinator = _coordinator(
        InMemoryContinuousCollectionJournal(), _Capture(), analysis
    )

    result = coordinator.analyze_next(deadline_utc_ns=UtcNs(START + 1))

    assert result.status is ContinuousCollectionStatus.CAPTURE_PHASE_CLOSED
    assert result.state.phase is ContinuousCollectionPhase.CAPTURING
    assert analysis.calls == []


def test_parked_staged_work_can_durably_halt_open_analysis() -> None:
    coordinator = _coordinator(
        InMemoryContinuousCollectionJournal(), _Capture(), _Analysis()
    )
    coordinator.capture_next(UtcNs(START))
    coordinator.close_capture()

    halted = coordinator.halt_analysis()
    replay = coordinator.halt_analysis()

    assert halted.state.phase is ContinuousCollectionPhase.HALTED
    assert halted.state.halt_reason is ContinuousCollectionHaltReason.ANALYSIS_FAILED
    assert replay.state == halted.state


def test_main_collection_requires_the_exact_qualification_receipt() -> None:
    definition, receipt = _main_definition()
    definition = replace(definition, analysis_after_each_capture=False)

    with pytest.raises(ValueError, match="qualification receipt differs"):
        DeferredCampaignCoordinator(
            definition,
            InMemoryContinuousCollectionJournal(),
            _Capture(),
            _Analysis(),
            _Capacity(),
            0,
            replace(receipt, issued_utc_ns=UtcNs(int(receipt.issued_utc_ns) + 1)),
        )


def test_deferred_coordinator_rejects_per_capture_analysis_definition() -> None:
    definition, receipt = _main_definition()

    with pytest.raises(ValueError, match="continuous collection requires"):
        DeferredCampaignCoordinator(
            definition,
            InMemoryContinuousCollectionJournal(),
            _Capture(),
            _Analysis(),
            _Capacity(),
            0,
            receipt,
        )


def test_terminal_integrity_or_skew_failure_halts_without_fresh_retry() -> None:
    capture = _Capture(skew_ns=100_000_001)
    analysis = _Analysis()
    coordinator = _coordinator(InMemoryContinuousCollectionJournal(), capture, analysis)

    failed = coordinator.capture_next(UtcNs(START))
    repeated = coordinator.capture_next(UtcNs(START + 1))

    assert failed.status is repeated.status is ContinuousCollectionStatus.HALTED
    assert failed.state.halt_reason is ContinuousCollectionHaltReason.INTEGRITY_FAILURE
    assert failed.state.records[-1].phase is (
        ContinuousCollectionRecordPhase.TERMINAL_FAILED
    )
    assert len(capture.calls) == 1
    assert analysis.calls == []


def test_uncertain_attempt_reuses_exact_identity_and_is_bounded() -> None:
    capture = _Capture(fail_once=True)
    coordinator = _coordinator(
        InMemoryContinuousCollectionJournal(), capture, _Analysis()
    )

    uncertain = coordinator.capture_next(UtcNs(START))
    resumed = coordinator.capture_next(UtcNs(START + 1))

    assert uncertain.status is ContinuousCollectionStatus.CAPTURE_UNCERTAIN
    assert resumed.status is ContinuousCollectionStatus.CAPTURED
    assert capture.calls[0][0].digest == capture.calls[1][0].digest

    class FailingCapture(_Capture):
        def capture(self, *args: object, **kwargs: object):
            super().capture(*args, **kwargs)  # type: ignore[arg-type]
            raise RuntimeError("uncertain")

    failing = FailingCapture()
    bounded = _coordinator(InMemoryContinuousCollectionJournal(), failing, _Analysis())
    assert bounded.capture_next(UtcNs(START)).status is (
        ContinuousCollectionStatus.CAPTURE_UNCERTAIN
    )
    assert bounded.capture_next(UtcNs(START + 1)).status is (
        ContinuousCollectionStatus.CAPTURE_UNCERTAIN
    )
    halted = bounded.capture_next(UtcNs(START + 2))
    assert halted.status is ContinuousCollectionStatus.HALTED
    assert halted.state.halt_reason is ContinuousCollectionHaltReason.CAPTURE_UNCERTAIN
    assert len(failing.calls) == 2


def test_sqlite_restart_preserves_capture_snapshot_and_deferred_analysis(
    tmp_path: Path,
) -> None:
    definition, receipt = _main_definition()
    definition = replace(definition, analysis_after_each_capture=False)
    database = tmp_path / "continuous.sqlite3"
    capture = _Capture()
    analysis = _Analysis()
    first = DeferredCampaignCoordinator(
        definition,
        SQLiteContinuousCollectionJournal(database),
        capture,
        analysis,
        _Capacity(),
        0,
        receipt,
    )
    captured = first.capture_next(UtcNs(START))
    first.close_capture()

    restarted = DeferredCampaignCoordinator(
        definition,
        SQLiteContinuousCollectionJournal(database),
        capture,
        analysis,
        _Capacity(),
        0,
        receipt,
    )
    analyzed = restarted.analyze_next(deadline_utc_ns=UtcNs(START + 10_000))
    complete = restarted.analyze_next(deadline_utc_ns=UtcNs(START + 20_000))

    assert captured.status is ContinuousCollectionStatus.CAPTURED
    assert analyzed.status is ContinuousCollectionStatus.ANALYZED
    assert complete.status is ContinuousCollectionStatus.COMPLETE
    assert len(capture.calls) == 1
    assert len(analysis.calls) == 1
    assert (
        SQLiteContinuousCollectionJournal(database).load(definition) == complete.state
    )


def test_sqlite_journal_fails_closed_on_tampering(tmp_path: Path) -> None:
    definition, _ = _main_definition()
    definition = replace(definition, analysis_after_each_capture=False)
    database = tmp_path / "continuous.sqlite3"
    journal = SQLiteContinuousCollectionJournal(database)
    journal.initialize(definition)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE continuous_collection_journal SET state_payload = ?",
            (b"{}",),
        )

    with pytest.raises(RuntimeError, match="integrity"):
        journal.load(definition)
