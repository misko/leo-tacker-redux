"""Durable capture-first coordination for the finite Gauss V5 campaign.

This policy deliberately reuses the reviewed campaign unit identities and the
narrow capture/analysis ports. Receipt reconciliation remains deferred until
collection closes. A separate read-only online coordinator may compute and
project complete terminal windows while collection is open. Every call here
advances at most one durable transition, so a process may stop and resume
without inventing a new identity or bursting missed work.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Protocol

from leo_flow.contracts.capture_batch import (
    CaptureAttemptState,
    CaptureBatchSnapshot,
    PairedAnalysisEligibility,
)
from leo_flow.contracts.core import Digest, UtcNs

from .campaign import (
    CAMPAIGN_SUCCESS_TARGET,
    SLOT_PERIOD_DENOMINATOR,
    SLOT_PERIOD_NUMERATOR_NS,
    CampaignAnalysisPort,
    CampaignAnalysisReceipt,
    CampaignCapacityPort,
    CampaignCapturePort,
    CampaignDefinition,
    CampaignQualificationReceipt,
    CampaignUnit,
    build_campaign_unit,
    campaign_cell,
)


class ContinuousCollectionPhase(str, Enum):
    CAPTURING = "capturing"
    ANALYZING = "analyzing"
    COMPLETE = "complete"
    HALTED = "halted"


class ContinuousCollectionHaltReason(str, Enum):
    INTEGRITY_FAILURE = "integrity_failure"
    CAPTURE_UNCERTAIN = "capture_uncertain"
    MISSED_SLOT = "missed_slot"
    WINDOW_ENDED = "window_ended"
    ANALYSIS_FAILED = "analysis_failed"


class ContinuousCollectionRecordPhase(str, Enum):
    PLANNED = "planned"
    ABANDONED = "abandoned"
    CAPTURED = "captured"
    TERMINAL_FAILED = "terminal_failed"
    ANALYSIS_FAILED = "analysis_failed"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class ContinuousCollectionRecord:
    unit: CampaignUnit
    phase: ContinuousCollectionRecordPhase = ContinuousCollectionRecordPhase.PLANNED
    capture_invocations: int = 0
    analysis_invocations: int = 0
    snapshot: CaptureBatchSnapshot | None = None
    analysis_receipt: CampaignAnalysisReceipt | None = None

    def __post_init__(self) -> None:
        if self.capture_invocations < 0 or self.analysis_invocations < 0:
            raise ValueError("continuous invocation counts must be non-negative")
        if (
            self.phase
            in {
                ContinuousCollectionRecordPhase.CAPTURED,
                ContinuousCollectionRecordPhase.TERMINAL_FAILED,
                ContinuousCollectionRecordPhase.ANALYSIS_FAILED,
                ContinuousCollectionRecordPhase.COMPLETE,
            }
            and self.snapshot is None
        ):
            raise ValueError("terminal continuous record requires a snapshot")
        if self.phase in {
            ContinuousCollectionRecordPhase.PLANNED,
            ContinuousCollectionRecordPhase.ABANDONED,
        } and (self.snapshot is not None or self.analysis_receipt is not None):
            raise ValueError("nonterminal continuous record carries terminal evidence")
        if self.phase is ContinuousCollectionRecordPhase.COMPLETE:
            if self.analysis_receipt is None:
                raise ValueError(
                    "complete continuous record requires analysis evidence"
                )
        elif self.analysis_receipt is not None:
            raise ValueError("incomplete continuous record carries analysis evidence")
        if self.analysis_invocations and self.phase in {
            ContinuousCollectionRecordPhase.PLANNED,
            ContinuousCollectionRecordPhase.ABANDONED,
            ContinuousCollectionRecordPhase.TERMINAL_FAILED,
        }:
            raise ValueError("uncaptured continuous record has analysis invocations")


@dataclass(frozen=True, slots=True)
class ContinuousCollectionState:
    definition_digest: Digest
    phase: ContinuousCollectionPhase = ContinuousCollectionPhase.CAPTURING
    records: tuple[ContinuousCollectionRecord, ...] = ()
    halt_reason: ContinuousCollectionHaltReason | None = None
    revision: int = 0

    def __post_init__(self) -> None:
        if self.revision < 0:
            raise ValueError("continuous collection revision must be non-negative")
        if (self.phase is ContinuousCollectionPhase.HALTED) != (
            self.halt_reason is not None
        ):
            raise ValueError("continuous collection halt evidence differs from phase")
        accepted = 0
        for index, record in enumerate(self.records):
            if record.unit.slot_index != index or record.unit.success_index != accepted:
                raise ValueError(
                    "continuous collection record schedule is not canonical"
                )
            if record.phase in {
                ContinuousCollectionRecordPhase.CAPTURED,
                ContinuousCollectionRecordPhase.ANALYSIS_FAILED,
                ContinuousCollectionRecordPhase.COMPLETE,
            }:
                accepted += 1
        if accepted > CAMPAIGN_SUCCESS_TARGET:
            raise ValueError("continuous collection exceeds the campaign target")
        if any(
            item.phase is ContinuousCollectionRecordPhase.PLANNED
            for item in self.records[:-1]
        ):
            raise ValueError("only the last continuous identity may remain planned")
        if self.phase in {
            ContinuousCollectionPhase.ANALYZING,
            ContinuousCollectionPhase.COMPLETE,
        } and any(
            item.phase is ContinuousCollectionRecordPhase.PLANNED
            for item in self.records
        ):
            raise ValueError("closed collection retains an uncertain capture identity")
        if self.phase is ContinuousCollectionPhase.COMPLETE and any(
            item.phase
            in {
                ContinuousCollectionRecordPhase.CAPTURED,
                ContinuousCollectionRecordPhase.ANALYSIS_FAILED,
            }
            for item in self.records
        ):
            raise ValueError("complete collection retains pending analysis")
        if (
            self.phase is ContinuousCollectionPhase.CAPTURING
            and self.records
            and self.records[-1].phase
            in {
                ContinuousCollectionRecordPhase.ABANDONED,
                ContinuousCollectionRecordPhase.TERMINAL_FAILED,
            }
        ):
            raise ValueError("capture stop evidence requires a halted phase")

    @property
    def captured_count(self) -> int:
        return sum(
            item.phase
            in {
                ContinuousCollectionRecordPhase.CAPTURED,
                ContinuousCollectionRecordPhase.ANALYSIS_FAILED,
                ContinuousCollectionRecordPhase.COMPLETE,
            }
            for item in self.records
        )

    @property
    def analyzed_count(self) -> int:
        return sum(
            item.phase is ContinuousCollectionRecordPhase.COMPLETE
            for item in self.records
        )


class ContinuousCollectionJournal(Protocol):
    """Digest-bound durable compare-and-swap state."""

    def initialize(
        self, definition: CampaignDefinition
    ) -> ContinuousCollectionState: ...

    def load(self, definition: CampaignDefinition) -> ContinuousCollectionState: ...

    def compare_and_swap(
        self,
        definition: CampaignDefinition,
        expected_revision: int,
        replacement: ContinuousCollectionState,
    ) -> ContinuousCollectionState: ...


class ContinuousCollectionStatus(str, Enum):
    CAPTURED = "captured"
    NOT_DUE = "not_due"
    CAPACITY_BLOCKED = "capacity_blocked"
    CAPTURE_UNCERTAIN = "capture_uncertain"
    CAPTURE_PHASE_CLOSED = "capture_phase_closed"
    ANALYZED = "analyzed"
    ANALYSIS_PENDING = "analysis_pending"
    COMPLETE = "complete"
    HALTED = "halted"


@dataclass(frozen=True, slots=True)
class ContinuousCollectionResult:
    status: ContinuousCollectionStatus
    state: ContinuousCollectionState
    unit: CampaignUnit | None = None


class InMemoryContinuousCollectionJournal:
    """Test/local port implementation with the same CAS semantics as durable stores."""

    def __init__(self) -> None:
        self._state: ContinuousCollectionState | None = None

    def initialize(self, definition: CampaignDefinition) -> ContinuousCollectionState:
        if self._state is None:
            self._state = ContinuousCollectionState(definition.digest)
        return self.load(definition)

    def load(self, definition: CampaignDefinition) -> ContinuousCollectionState:
        if self._state is None:
            raise RuntimeError("continuous collection journal is not initialized")
        if self._state.definition_digest != definition.digest:
            raise RuntimeError("continuous collection definition differs")
        return self._state

    def compare_and_swap(
        self,
        definition: CampaignDefinition,
        expected_revision: int,
        replacement: ContinuousCollectionState,
    ) -> ContinuousCollectionState:
        current = self.load(definition)
        if current.revision != expected_revision:
            raise RuntimeError("continuous collection revision changed")
        if (
            replacement.definition_digest != definition.digest
            or replacement.revision != expected_revision + 1
        ):
            raise RuntimeError("continuous collection replacement is invalid")
        self._state = replacement
        return replacement


class DeferredCampaignCoordinator:
    """Collect exact dual batches first, then analyze their durable snapshots.

    Serial receipt reconciliation remains behind the campaign lock. The
    persisted phase is a fail-closed boundary: this coordinator cannot invoke
    receipt analysis before capture is closed, nor capture after that point.
    """

    def __init__(
        self,
        definition: CampaignDefinition,
        journal: ContinuousCollectionJournal,
        capture: CampaignCapturePort,
        analysis: CampaignAnalysisPort,
        capacity: CampaignCapacityPort,
        capacity_margin_bytes: int,
        qualification_receipt: CampaignQualificationReceipt,
    ) -> None:
        if definition.qualification or definition.analysis_after_each_capture:
            raise ValueError("continuous collection requires a main campaign")
        if (
            isinstance(capacity_margin_bytes, bool)
            or not isinstance(capacity_margin_bytes, int)
            or capacity_margin_bytes < 0
        ):
            raise ValueError("continuous capacity margin must be non-negative")
        if qualification_receipt.digest != definition.qualification_receipt_digest:
            raise ValueError("continuous collection qualification receipt differs")
        self._definition = definition
        self._journal = journal
        self._capture = capture
        self._analysis = analysis
        self._capacity = capacity
        self._capacity_margin_bytes = capacity_margin_bytes
        self._qualification_receipt = qualification_receipt
        journal.initialize(definition)

    def capture_next(self, now_utc_ns: UtcNs) -> ContinuousCollectionResult:
        """Advance no more than one exact capture attempt."""

        state = self._journal.load(self._definition)
        if state.phase is ContinuousCollectionPhase.HALTED:
            return ContinuousCollectionResult(ContinuousCollectionStatus.HALTED, state)
        if state.phase is not ContinuousCollectionPhase.CAPTURING:
            return ContinuousCollectionResult(
                ContinuousCollectionStatus.CAPTURE_PHASE_CLOSED, state
            )
        if state.captured_count >= CAMPAIGN_SUCCESS_TARGET:
            closed = self._store(
                replace(state, phase=ContinuousCollectionPhase.ANALYZING), state
            )
            return ContinuousCollectionResult(
                ContinuousCollectionStatus.CAPTURE_PHASE_CLOSED, closed
            )

        record = state.records[-1] if state.records else None
        if (
            record is not None
            and record.phase is ContinuousCollectionRecordPhase.PLANNED
        ):
            if (
                record.capture_invocations
                >= self._definition.maximum_capture_invocations
            ):
                halted = self._halt(
                    state, ContinuousCollectionHaltReason.CAPTURE_UNCERTAIN
                )
                return ContinuousCollectionResult(
                    ContinuousCollectionStatus.HALTED, halted, record.unit
                )
            return self._capture_planned(state, record, now_utc_ns)

        slot_index = len(state.records)
        requested = UtcNs(
            int(self._definition.start_utc_ns)
            + slot_index * SLOT_PERIOD_NUMERATOR_NS // SLOT_PERIOD_DENOMINATOR
        )
        if requested >= self._definition.end_utc_ns:
            halted = self._halt(state, ContinuousCollectionHaltReason.WINDOW_ENDED)
            return ContinuousCollectionResult(ContinuousCollectionStatus.HALTED, halted)
        if (
            int(now_utc_ns)
            > int(requested) + self._definition.maximum_start_lateness_ns
        ):
            halted = self._halt(state, ContinuousCollectionHaltReason.MISSED_SLOT)
            return ContinuousCollectionResult(ContinuousCollectionStatus.HALTED, halted)
        unit = build_campaign_unit(
            self._definition,
            success_index=state.captured_count,
            slot_index=slot_index,
            retry_index=0,
            requested_start_utc_ns=requested,
        )
        state = self._store(
            replace(state, records=(*state.records, ContinuousCollectionRecord(unit))),
            state,
        )
        if int(now_utc_ns) < int(requested) - self._definition.preflight_lead_ns:
            return ContinuousCollectionResult(
                ContinuousCollectionStatus.NOT_DUE, state, unit
            )
        return self._capture_planned(state, state.records[-1], now_utc_ns)

    def close_capture(self) -> ContinuousCollectionResult:
        """Durably and irreversibly enter deferred analysis.

        A terminal integrity halt remains an explicit stop until the operator
        calls this method; successful snapshots are still available for local
        analysis, but no further RF work can be resumed under this definition.
        """

        state = self._journal.load(self._definition)
        if state.phase is ContinuousCollectionPhase.COMPLETE:
            return ContinuousCollectionResult(
                ContinuousCollectionStatus.COMPLETE, state
            )
        if state.phase is ContinuousCollectionPhase.ANALYZING:
            return ContinuousCollectionResult(
                ContinuousCollectionStatus.ANALYSIS_PENDING, state
            )
        if (
            state.records
            and state.records[-1].phase is ContinuousCollectionRecordPhase.PLANNED
        ):
            raise RuntimeError(
                "uncertain capture identity must be resolved before close"
            )
        closed = self._store(
            replace(
                state,
                phase=ContinuousCollectionPhase.ANALYZING,
                halt_reason=None,
            ),
            state,
        )
        return ContinuousCollectionResult(
            ContinuousCollectionStatus.ANALYSIS_PENDING, closed
        )

    def analyze_next(self, *, deadline_utc_ns: UtcNs) -> ContinuousCollectionResult:
        """Analyze no more than one captured batch after collection is closed."""

        state = self._journal.load(self._definition)
        if state.phase is ContinuousCollectionPhase.HALTED:
            return ContinuousCollectionResult(ContinuousCollectionStatus.HALTED, state)
        if state.phase is ContinuousCollectionPhase.CAPTURING:
            return ContinuousCollectionResult(
                ContinuousCollectionStatus.CAPTURE_PHASE_CLOSED, state
            )
        if state.phase is ContinuousCollectionPhase.COMPLETE:
            return ContinuousCollectionResult(
                ContinuousCollectionStatus.COMPLETE, state
            )
        candidate = next(
            (
                (index, item)
                for index, item in enumerate(state.records)
                if item.phase
                in {
                    ContinuousCollectionRecordPhase.CAPTURED,
                    ContinuousCollectionRecordPhase.ANALYSIS_FAILED,
                }
            ),
            None,
        )
        if candidate is None:
            complete = self._store(
                replace(state, phase=ContinuousCollectionPhase.COMPLETE), state
            )
            return ContinuousCollectionResult(
                ContinuousCollectionStatus.COMPLETE, complete
            )
        index, record = candidate
        if record.snapshot is None:
            raise RuntimeError("deferred analysis record has no terminal snapshot")
        if record.analysis_invocations >= self._definition.maximum_analysis_invocations:
            halted = self._halt(state, ContinuousCollectionHaltReason.ANALYSIS_FAILED)
            return ContinuousCollectionResult(
                ContinuousCollectionStatus.HALTED, halted, record.unit
            )
        invoked = replace(record, analysis_invocations=record.analysis_invocations + 1)
        state = self._replace_record(state, index, invoked)
        try:
            receipt = self._analysis.analyze(
                record.snapshot, deadline_utc_ns=deadline_utc_ns
            )
            _validate_analysis_receipt(record.snapshot, receipt)
            if receipt.completed_utc_ns > deadline_utc_ns:
                raise RuntimeError("deferred analysis exceeded its deadline")
        except Exception:  # noqa: BLE001 - exact durable snapshot is the retry point
            failed = replace(
                invoked, phase=ContinuousCollectionRecordPhase.ANALYSIS_FAILED
            )
            state = self._replace_record(state, index, failed)
            return ContinuousCollectionResult(
                ContinuousCollectionStatus.ANALYSIS_PENDING, state, record.unit
            )
        complete_record = replace(
            invoked,
            phase=ContinuousCollectionRecordPhase.COMPLETE,
            analysis_receipt=receipt,
        )
        state = self._replace_record(state, index, complete_record)
        return ContinuousCollectionResult(
            ContinuousCollectionStatus.ANALYZED, state, record.unit
        )

    def halt_analysis(self) -> ContinuousCollectionResult:
        """Durably halt an open drain after exact scoped work parks."""

        state = self._journal.load(self._definition)
        if state.phase is ContinuousCollectionPhase.HALTED:
            return ContinuousCollectionResult(ContinuousCollectionStatus.HALTED, state)
        if state.phase is not ContinuousCollectionPhase.ANALYZING:
            raise RuntimeError("campaign analysis phase is not open")
        halted = self._halt(state, ContinuousCollectionHaltReason.ANALYSIS_FAILED)
        return ContinuousCollectionResult(ContinuousCollectionStatus.HALTED, halted)

    def _capture_planned(
        self,
        state: ContinuousCollectionState,
        record: ContinuousCollectionRecord,
        now_utc_ns: UtcNs,
    ) -> ContinuousCollectionResult:
        requested = record.unit.requested_start_utc_ns
        if int(now_utc_ns) < int(requested) - self._definition.preflight_lead_ns:
            return ContinuousCollectionResult(
                ContinuousCollectionStatus.NOT_DUE, state, record.unit
            )
        if (
            int(now_utc_ns)
            > int(requested) + self._definition.maximum_start_lateness_ns
        ):
            abandoned = replace(record, phase=ContinuousCollectionRecordPhase.ABANDONED)
            halted = self._store(
                replace(
                    state,
                    records=self._records_with(
                        state, len(state.records) - 1, abandoned
                    ),
                    phase=ContinuousCollectionPhase.HALTED,
                    halt_reason=ContinuousCollectionHaltReason.MISSED_SLOT,
                ),
                state,
            )
            return ContinuousCollectionResult(
                ContinuousCollectionStatus.HALTED, halted, record.unit
            )
        if not self._has_capacity(state):
            return ContinuousCollectionResult(
                ContinuousCollectionStatus.CAPACITY_BLOCKED, state, record.unit
            )
        invoked = replace(record, capture_invocations=record.capture_invocations + 1)
        state = self._replace_record(state, len(state.records) - 1, invoked)
        try:
            snapshot = self._capture.capture(
                record.unit,
                not_before_utc_ns=requested,
                deadline_utc_ns=self._slot_deadline(record.unit),
            )
            if not snapshot.terminal or snapshot.definition != record.unit.batch:
                raise RuntimeError("capture returned another or incomplete batch")
        except Exception:  # noqa: BLE001 - exact identity remains the bounded retry point
            return ContinuousCollectionResult(
                ContinuousCollectionStatus.CAPTURE_UNCERTAIN, state, record.unit
            )
        if (
            any(
                outcome.state is not CaptureAttemptState.SUCCEEDED
                for outcome in snapshot.outcomes
            )
            or snapshot.paired_analysis_eligibility
            is not PairedAnalysisEligibility.ELIGIBLE
            or any(
                outcome.terminal_utc_ns > self._slot_deadline(record.unit)
                for outcome in snapshot.outcomes
            )
        ):
            captured_failure = replace(
                invoked,
                phase=ContinuousCollectionRecordPhase.TERMINAL_FAILED,
                snapshot=snapshot,
            )
            halted = self._store(
                replace(
                    state,
                    records=self._records_with(
                        state, len(state.records) - 1, captured_failure
                    ),
                    phase=ContinuousCollectionPhase.HALTED,
                    halt_reason=ContinuousCollectionHaltReason.INTEGRITY_FAILURE,
                ),
                state,
            )
            return ContinuousCollectionResult(
                ContinuousCollectionStatus.HALTED, halted, record.unit
            )
        captured = replace(
            invoked,
            phase=ContinuousCollectionRecordPhase.CAPTURED,
            snapshot=snapshot,
        )
        state = self._replace_record(state, len(state.records) - 1, captured)
        return ContinuousCollectionResult(
            ContinuousCollectionStatus.CAPTURED, state, record.unit
        )

    def _has_capacity(self, state: ContinuousCollectionState) -> bool:
        available = self._capacity.available_bytes()
        if (
            isinstance(available, bool)
            or not isinstance(available, int)
            or available < 0
        ):
            raise RuntimeError("continuous capacity evidence is invalid")
        remaining_raw = sum(
            campaign_cell(index).sample_count * 128
            for index in range(state.captured_count, CAMPAIGN_SUCCESS_TARGET)
        )
        required = remaining_raw * 2 + self._capacity_margin_bytes
        return available >= required

    def _slot_deadline(self, unit: CampaignUnit) -> UtcNs:
        return UtcNs(
            int(self._definition.start_utc_ns)
            + (unit.slot_index + 1)
            * SLOT_PERIOD_NUMERATOR_NS
            // SLOT_PERIOD_DENOMINATOR
        )

    def _halt(
        self,
        state: ContinuousCollectionState,
        reason: ContinuousCollectionHaltReason,
    ) -> ContinuousCollectionState:
        return self._store(
            replace(
                state,
                phase=ContinuousCollectionPhase.HALTED,
                halt_reason=reason,
            ),
            state,
        )

    def _replace_record(
        self,
        state: ContinuousCollectionState,
        index: int,
        replacement: ContinuousCollectionRecord,
    ) -> ContinuousCollectionState:
        return self._store(
            replace(state, records=self._records_with(state, index, replacement)),
            state,
        )

    @staticmethod
    def _records_with(
        state: ContinuousCollectionState,
        index: int,
        replacement: ContinuousCollectionRecord,
    ) -> tuple[ContinuousCollectionRecord, ...]:
        if state.records[index].unit != replacement.unit:
            raise RuntimeError("continuous transition targets another unit")
        records = list(state.records)
        records[index] = replacement
        return tuple(records)

    def _store(
        self,
        replacement: ContinuousCollectionState,
        current: ContinuousCollectionState,
    ) -> ContinuousCollectionState:
        return self._journal.compare_and_swap(
            self._definition,
            current.revision,
            replace(replacement, revision=current.revision + 1),
        )


def _validate_analysis_receipt(
    snapshot: CaptureBatchSnapshot, receipt: CampaignAnalysisReceipt
) -> None:
    expected = tuple(
        sorted(
            (
                item.recording_ref.recording_id
                for item in snapshot.outcomes
                if item.recording_ref is not None
            ),
            key=str,
        )
    )
    if receipt.batch_id != snapshot.batch_id or receipt.recording_ids != expected:
        raise RuntimeError("analysis receipt identifies another batch or recordings")
