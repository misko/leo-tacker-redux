"""Private finite-campaign policy for coordinated V5 capture and later analysis.

The campaign is deliberately not a public contract.  It schedules immutable
public capture batches, persists every transition through a narrow journal
port, and passes only an explicit terminal batch snapshot to analysis.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Protocol

from leo_flow.contracts.capture import GainMode, GainSetting
from leo_flow.contracts.capture_batch import (
    CaptureAttemptState,
    CaptureBatchDefinition,
    CaptureBatchMode,
    CaptureBatchSnapshot,
    ExpectedCaptureAttempt,
    PairedAnalysisEligibility,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    CaptureAttemptId,
    CaptureBatchId,
    Digest,
    JobId,
    PlanId,
    RadioId,
    RecordingId,
    SchemaRef,
    UtcNs,
    canonical_digest,
)
from leo_flow.contracts.features import FeatureSetBundle, FeatureSetRef

from .scan_plan import (
    StarlinkEdgeScanSpec,
    build_starlink_edge_scan_plan,
)
from .v5_station import V5CaptureStation

CAMPAIGN_WINDOW_NS = 8 * 60 * 60 * 1_000_000_000
CAMPAIGN_ROUNDS = 104
SLOTS_PER_ROUND = 9
CAMPAIGN_SUCCESS_TARGET = CAMPAIGN_ROUNDS * SLOTS_PER_ROUND
CAMPAIGN_RAW_BYTES = 32_614_400_000
SLOT_PERIOD_NUMERATOR_NS = 400_000_000_000
SLOT_PERIOD_DENOMINATOR = 13
QUALIFICATION_SLOT_PERIOD_NUMERATOR_NS = 400_000_000_000
QUALIFICATION_SLOT_PERIOD_DENOMINATOR = 3
MAXIMUM_OBSERVED_START_SKEW_NS = 100_000_000
PREFLIGHT_LEAD_NS = 15_000_000_000
CAMPAIGN_HARDWARE_BLOCK_DURATION_MS = 40
CAMPAIGN_SCHEMA = "org.leo-flow.gauss-v5-campaign/v3"
QUALIFICATION_CAMPAIGN_SCHEMA = "org.leo-flow.gauss-v5-campaign/v2"
QUALIFICATION_SCHEDULE_POLICY = "fixed_nine_cell_no_catch_up_grid"
MAIN_SCHEDULE_POLICY = "balanced_nine_cell_four_geometry_no_catch_up_grid"
MAIN_GEOMETRY_SCHEDULE = (("L", "L"), ("L", "U"), ("U", "U"), ("U", "L"))
_CAMPAIGN_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")


@dataclass(frozen=True, order=True, slots=True)
class CampaignCell:
    sample_rate_hz: int
    duration_ms: int

    def __post_init__(self) -> None:
        if self.sample_rate_hz not in (1_250_000, 2_500_000, 5_000_000):
            raise ValueError("campaign sample rate is not in the reviewed matrix")
        if self.duration_ms not in (40, 80, 160):
            raise ValueError("campaign duration is not in the reviewed matrix")

    @property
    def sample_count(self) -> int:
        return self.sample_rate_hz * self.duration_ms // 1_000

    @property
    def hardware_block_samples(self) -> int:
        """Bound post-release transport priming while preserving the dwell."""

        return (
            self.sample_rate_hz
            * min(self.duration_ms, CAMPAIGN_HARDWARE_BLOCK_DURATION_MS)
            // 1_000
        )

    @property
    def arm_name(self) -> str:
        return f"{self.duration_ms}ms-{self.sample_rate_hz // 10_000}x10kSps"

    def document(self) -> dict[str, int]:
        return {
            "sample_rate_hz": self.sample_rate_hz,
            "duration_ms": self.duration_ms,
            "sample_count": self.sample_count,
        }


CAMPAIGN_CELLS = tuple(
    CampaignCell(rate, duration)
    for rate in (1_250_000, 2_500_000, 5_000_000)
    for duration in (40, 80, 160)
)


@dataclass(frozen=True, slots=True)
class CampaignDefinition:
    campaign_id: str
    start_utc_ns: UtcNs
    radio_a_id: RadioId
    radio_b_id: RadioId
    station_a_digest: Digest
    station_b_digest: Digest
    maximum_start_lateness_ns: int
    qualification_receipt_digest: Digest | None = None
    qualification: bool = False
    maximum_capture_invocations: int = 2
    maximum_fresh_attempts_per_cell: int = 2
    maximum_analysis_invocations: int = 3
    maximum_observed_start_skew_ns: int = MAXIMUM_OBSERVED_START_SKEW_NS
    preflight_lead_ns: int = PREFLIGHT_LEAD_NS
    analysis_after_each_capture: bool = True

    def __post_init__(self) -> None:
        if _CAMPAIGN_ID.fullmatch(self.campaign_id) is None:
            raise ValueError("campaign_id must be a bounded lowercase token")
        if int(self.start_utc_ns) < 0:
            raise ValueError("campaign start must be non-negative")
        if self.radio_a_id == self.radio_b_id:
            raise ValueError("campaign radios must be distinct")
        for value, name in (
            (self.maximum_capture_invocations, "maximum_capture_invocations"),
            (
                self.maximum_fresh_attempts_per_cell,
                "maximum_fresh_attempts_per_cell",
            ),
            (self.maximum_analysis_invocations, "maximum_analysis_invocations"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.maximum_observed_start_skew_ns != MAXIMUM_OBSERVED_START_SKEW_NS:
            raise ValueError("campaign skew bound must be the reviewed 100 ms")
        if self.preflight_lead_ns != PREFLIGHT_LEAD_NS:
            raise ValueError("campaign preflight lead must be the reviewed 15 seconds")
        if not isinstance(self.analysis_after_each_capture, bool):
            raise TypeError("campaign analysis phase policy must be a boolean")
        if self.qualification and not self.analysis_after_each_capture:
            raise ValueError("qualification requires per-capture analysis")
        if (
            isinstance(self.maximum_start_lateness_ns, bool)
            or not isinstance(self.maximum_start_lateness_ns, int)
            or self.maximum_start_lateness_ns < 0
        ):
            raise ValueError("campaign lateness bound must be non-negative")
        if self.qualification == (self.qualification_receipt_digest is not None):
            raise ValueError(
                "main campaign requires a qualification receipt; qualification does not"
            )

    @property
    def end_utc_ns(self) -> UtcNs:
        return UtcNs(int(self.start_utc_ns) + CAMPAIGN_WINDOW_NS)

    @property
    def target_successes(self) -> int:
        return SLOTS_PER_ROUND if self.qualification else CAMPAIGN_SUCCESS_TARGET

    @property
    def slot_period_numerator_ns(self) -> int:
        return (
            QUALIFICATION_SLOT_PERIOD_NUMERATOR_NS
            if self.qualification
            else SLOT_PERIOD_NUMERATOR_NS
        )

    @property
    def slot_period_denominator(self) -> int:
        return (
            QUALIFICATION_SLOT_PERIOD_DENOMINATOR
            if self.qualification
            else SLOT_PERIOD_DENOMINATOR
        )

    @property
    def capture_run_transition_limit(self) -> int:
        """Worst bounded loop calls: one NOT_DUE plus capture per slot and close."""

        return 2 * self.target_successes + 1

    @property
    def analysis_drain_transition_limit(self) -> int:
        return self.target_successes + 1

    @property
    def staged_analysis_drain_transition_limit(self) -> int:
        """One transition per balanced 36-batch window plus phase closure."""

        if self.target_successes % 36:
            raise ValueError("campaign target is not divisible by 36")
        return self.target_successes // 36 + 1

    def document(self) -> dict[str, object]:
        document: dict[str, object] = {
            "schema": (
                QUALIFICATION_CAMPAIGN_SCHEMA if self.qualification else CAMPAIGN_SCHEMA
            ),
            "campaign_id": self.campaign_id,
            "start_utc_ns": int(self.start_utc_ns),
            "campaign_kind": "qualification" if self.qualification else "main",
            "radios": [str(self.radio_a_id), str(self.radio_b_id)],
            "station_digests": [
                str(self.station_a_digest),
                str(self.station_b_digest),
            ],
            "cells": [item.document() for item in CAMPAIGN_CELLS],
            "maximum_capture_invocations": self.maximum_capture_invocations,
            "maximum_fresh_attempts_per_cell": (self.maximum_fresh_attempts_per_cell),
            "maximum_analysis_invocations": self.maximum_analysis_invocations,
            "maximum_observed_start_skew_ns": (self.maximum_observed_start_skew_ns),
            "maximum_start_lateness_ns": self.maximum_start_lateness_ns,
            "preflight_lead_ns": self.preflight_lead_ns,
            "capture_mode": CaptureBatchMode.COORDINATED.value,
            "analysis_after_each_capture": self.analysis_after_each_capture,
            "unit_schedule": (
                QUALIFICATION_SCHEDULE_POLICY
                if self.qualification
                else MAIN_SCHEDULE_POLICY
            ),
            "qualification_receipt_digest": (
                str(self.qualification_receipt_digest)
                if self.qualification_receipt_digest is not None
                else None
            ),
            "slot_period_numerator_ns": self.slot_period_numerator_ns,
            "slot_period_denominator": self.slot_period_denominator,
            "no_catch_up": True,
        }
        if self.qualification:
            document.update({"rounds": 1, "slots_per_round": SLOTS_PER_ROUND})
        else:
            document.update(
                {
                    "window_duration_ns": CAMPAIGN_WINDOW_NS,
                    "rounds": CAMPAIGN_ROUNDS,
                    "slots_per_round": SLOTS_PER_ROUND,
                    "geometry_schedule": [
                        list(item) for item in MAIN_GEOMETRY_SCHEDULE
                    ],
                    "capture_run_transition_limit": (self.capture_run_transition_limit),
                    "analysis_drain_transition_limit": (
                        self.analysis_drain_transition_limit
                    ),
                }
            )
        return document

    @property
    def digest(self) -> Digest:
        return canonical_digest(self.document())


@dataclass(frozen=True, slots=True)
class CampaignUnit:
    success_index: int
    slot_index: int
    retry_index: int
    cell: CampaignCell
    requested_start_utc_ns: UtcNs
    batch: CaptureBatchDefinition
    plan_a_id: PlanId
    plan_b_id: PlanId

    @property
    def unit_id(self) -> str:
        return (
            f"{self.batch.batch_id}_s{self.success_index:03d}_r{self.retry_index:02d}"
        )

    @property
    def digest(self) -> Digest:
        return canonical_digest(
            {
                "success_index": self.success_index,
                "slot_index": self.slot_index,
                "retry_index": self.retry_index,
                "cell": self.cell.document(),
                "batch_digest": str(canonical_digest(self.batch)),
                "plan_ids": [str(self.plan_a_id), str(self.plan_b_id)],
            }
        )


class CampaignUnitPhase(str, Enum):
    PLANNED = "planned"
    CAPTURED = "captured"
    TERMINAL_FAILED = "terminal_failed"
    ANALYSIS_FAILED = "analysis_failed"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class CampaignUnitRecord:
    unit: CampaignUnit
    phase: CampaignUnitPhase = CampaignUnitPhase.PLANNED
    capture_invocations: int = 0
    analysis_invocations: int = 0
    snapshot: CaptureBatchSnapshot | None = None
    analysis_receipt: CampaignAnalysisReceipt | None = None


@dataclass(frozen=True, slots=True)
class CampaignJournalState:
    definition_digest: Digest
    records: tuple[CampaignUnitRecord, ...] = ()
    revision: int = 0

    @property
    def successful_counts(self) -> tuple[int, ...]:
        counts = [0] * len(CAMPAIGN_CELLS)
        for record in self.records:
            if record.phase is CampaignUnitPhase.COMPLETE:
                counts[CAMPAIGN_CELLS.index(record.unit.cell)] += 1
        return tuple(counts)

    @property
    def completed_successes(self) -> int:
        return sum(self.successful_counts)

    @property
    def accepted_balanced_rounds(self) -> int:
        """Number of complete equal-count rounds; excludes any partial round."""

        return min(self.successful_counts)


class CampaignJournal(Protocol):
    def initialize(self, definition: CampaignDefinition) -> CampaignJournalState: ...

    def load(self, definition: CampaignDefinition) -> CampaignJournalState: ...

    def compare_and_swap(
        self,
        definition: CampaignDefinition,
        expected_revision: int,
        replacement: CampaignJournalState,
    ) -> CampaignJournalState: ...


class CampaignCapturePort(Protocol):
    """Exact dual execution; implementation owns mode lock and admission."""

    def capture(
        self,
        unit: CampaignUnit,
        *,
        not_before_utc_ns: UtcNs,
        deadline_utc_ns: UtcNs,
    ) -> CaptureBatchSnapshot: ...


@dataclass(frozen=True, slots=True)
class CampaignAnalysisSuccess:
    recording_id: RecordingId
    analysis_job_id: JobId
    result_ref: ArtifactRef
    projection_work_id: str
    projected_feature_set_ref: FeatureSetRef
    projected_utc_ns: UtcNs

    def __post_init__(self) -> None:
        if not self.projection_work_id.startswith("fpwork_"):
            raise ValueError("analysis receipt projection work ID is invalid")
        if int(self.projected_utc_ns) < 0:
            raise ValueError("analysis receipt projection time is invalid")
        if self.result_ref.digest != self.projected_feature_set_ref.bundle_ref.digest:
            raise ValueError("analysis and projected FeatureSet artifacts differ")
        if self.result_ref.artifact_id != str(
            self.projected_feature_set_ref.feature_set_id
        ):
            raise ValueError("analysis and projected FeatureSet identities differ")
        if self.result_ref.schema != SchemaRef(FeatureSetBundle.SCHEMA_ID):
            raise ValueError("analysis result is not a FeatureSet bundle")


@dataclass(frozen=True, slots=True)
class CampaignAnalysisReceipt:
    batch_id: CaptureBatchId
    successes: tuple[CampaignAnalysisSuccess, CampaignAnalysisSuccess]
    completed_utc_ns: UtcNs

    def __post_init__(self) -> None:
        if int(self.completed_utc_ns) < 0:
            raise ValueError("analysis receipt completion time is invalid")
        if any(
            item.projected_utc_ns > self.completed_utc_ns for item in self.successes
        ):
            raise ValueError("projection completes after its analysis receipt")
        if tuple(sorted(self.successes, key=lambda item: str(item.recording_id))) != (
            self.successes
        ):
            raise ValueError("analysis receipt successes are not canonical")
        for values, name in (
            ((item.recording_id for item in self.successes), "recording"),
            ((item.analysis_job_id for item in self.successes), "job"),
            ((item.projection_work_id for item in self.successes), "projection work"),
        ):
            materialized = tuple(values)
            if len(set(materialized)) != 2:
                raise ValueError(f"analysis receipt requires two distinct {name} IDs")

    @property
    def recording_ids(self) -> tuple[RecordingId, RecordingId]:
        return tuple(item.recording_id for item in self.successes)  # type: ignore[return-value]


class CampaignAnalysisPort(Protocol):
    """Exact batch drain; implementation owns mode lock and terminal DB checks."""

    def analyze(
        self, snapshot: CaptureBatchSnapshot, *, deadline_utc_ns: UtcNs
    ) -> CampaignAnalysisReceipt: ...


class CampaignCapacityPort(Protocol):
    """Read-only local capacity evidence used before every new capture."""

    def available_bytes(self) -> int: ...


class CampaignRunStatus(str, Enum):
    UNIT_COMPLETE = "unit_complete"
    NOT_DUE = "not_due"
    CAMPAIGN_COMPLETE = "campaign_complete"
    WINDOW_ENDED = "window_ended"
    MISSED_SLOT = "missed_slot"
    CAPACITY_BLOCKED = "capacity_blocked"
    CAPTURE_UNCERTAIN = "capture_uncertain"
    CAPTURE_FAILED = "capture_failed"
    ANALYSIS_FAILED = "analysis_failed"


@dataclass(frozen=True, slots=True)
class CampaignRunResult:
    status: CampaignRunStatus
    state: CampaignJournalState
    unit: CampaignUnit | None = None


class InMemoryCampaignJournal:
    def __init__(self) -> None:
        self._state: CampaignJournalState | None = None

    def initialize(self, definition: CampaignDefinition) -> CampaignJournalState:
        if self._state is None:
            self._state = CampaignJournalState(definition.digest)
        return self.load(definition)

    def load(self, definition: CampaignDefinition) -> CampaignJournalState:
        if self._state is None:
            raise RuntimeError("campaign journal is not initialized")
        if self._state.definition_digest != definition.digest:
            raise RuntimeError("campaign definition differs from durable journal")
        return self._state

    def compare_and_swap(
        self,
        definition: CampaignDefinition,
        expected_revision: int,
        replacement: CampaignJournalState,
    ) -> CampaignJournalState:
        current = self.load(definition)
        if current.revision != expected_revision:
            raise RuntimeError("campaign journal revision changed")
        if (
            replacement.definition_digest != current.definition_digest
            or replacement.revision != current.revision + 1
        ):
            raise RuntimeError("campaign journal replacement is invalid")
        self._state = replacement
        return replacement


class CampaignCoordinator:
    """Advance at most one durable capture/analysis unit per call."""

    def __init__(
        self,
        definition: CampaignDefinition,
        journal: CampaignJournal,
        capture: CampaignCapturePort,
        analysis: CampaignAnalysisPort,
        capacity: CampaignCapacityPort,
        capacity_margin_bytes: int,
        qualification_receipt: CampaignQualificationReceipt | None = None,
    ) -> None:
        if not definition.analysis_after_each_capture:
            raise ValueError("campaign coordinator requires per-capture analysis")
        self._definition = definition
        self._journal = journal
        self._capture = capture
        self._analysis = analysis
        self._capacity = capacity
        if (
            isinstance(capacity_margin_bytes, bool)
            or not isinstance(capacity_margin_bytes, int)
            or capacity_margin_bytes < 0
        ):
            raise ValueError("campaign capacity margin must be non-negative")
        self._capacity_margin_bytes = capacity_margin_bytes
        if definition.qualification:
            if qualification_receipt is not None:
                raise ValueError("qualification campaign cannot consume a receipt")
        elif (
            qualification_receipt is None
            or qualification_receipt.digest != definition.qualification_receipt_digest
        ):
            raise ValueError("main campaign qualification receipt differs")
        journal.initialize(definition)

    def run_next(self, now_utc_ns: UtcNs) -> CampaignRunResult:
        state = self._journal.load(self._definition)
        target = self._definition.target_successes
        if state.completed_successes >= target:
            return CampaignRunResult(CampaignRunStatus.CAMPAIGN_COMPLETE, state)

        record = state.records[-1] if state.records else None
        if record is not None and record.phase in (
            CampaignUnitPhase.CAPTURED,
            CampaignUnitPhase.ANALYSIS_FAILED,
        ):
            return self._run_analysis(state, record, now_utc_ns)
        if record is not None and record.phase is CampaignUnitPhase.PLANNED:
            if (
                record.capture_invocations
                >= self._definition.maximum_capture_invocations
            ):
                return CampaignRunResult(
                    CampaignRunStatus.CAPTURE_UNCERTAIN, state, record.unit
                )
            if int(now_utc_ns) < (
                int(record.unit.requested_start_utc_ns)
                - self._definition.preflight_lead_ns
            ):
                return CampaignRunResult(CampaignRunStatus.NOT_DUE, state, record.unit)
            if int(now_utc_ns) > (
                int(record.unit.requested_start_utc_ns)
                + self._definition.maximum_start_lateness_ns
            ):
                return CampaignRunResult(
                    CampaignRunStatus.MISSED_SLOT, state, record.unit
                )
            if not self._has_capacity(state):
                return CampaignRunResult(
                    CampaignRunStatus.CAPACITY_BLOCKED, state, record.unit
                )
            return self._run_capture(state, record)

        success_index = state.completed_successes
        slot_index = 0 if record is None else record.unit.slot_index + 1
        retry_index = 0
        if record is not None and record.phase is CampaignUnitPhase.TERMINAL_FAILED:
            if self._definition.qualification:
                return CampaignRunResult(
                    CampaignRunStatus.CAPTURE_FAILED, state, record.unit
                )
            retry_index = record.unit.retry_index + 1
            if retry_index >= self._definition.maximum_fresh_attempts_per_cell:
                return CampaignRunResult(
                    CampaignRunStatus.CAPTURE_FAILED, state, record.unit
                )
        requested = self._next_requested_start(slot_index)
        if (
            not self._definition.qualification
            and requested >= self._definition.end_utc_ns
        ):
            return CampaignRunResult(CampaignRunStatus.WINDOW_ENDED, state)
        if int(now_utc_ns) > (
            int(requested) + self._definition.maximum_start_lateness_ns
        ):
            return CampaignRunResult(CampaignRunStatus.MISSED_SLOT, state)
        unit = build_campaign_unit(
            self._definition,
            success_index=success_index,
            slot_index=slot_index,
            retry_index=retry_index,
            requested_start_utc_ns=requested,
        )
        state = self._store(
            replace(
                state,
                records=(*state.records, CampaignUnitRecord(unit)),
            ),
            state,
        )
        if int(now_utc_ns) < (
            int(unit.requested_start_utc_ns) - self._definition.preflight_lead_ns
        ):
            return CampaignRunResult(CampaignRunStatus.NOT_DUE, state, unit)
        if not self._has_capacity(state):
            return CampaignRunResult(CampaignRunStatus.CAPACITY_BLOCKED, state, unit)
        return self._run_capture(state, state.records[-1])

    def _has_capacity(self, state: CampaignJournalState) -> bool:
        available = self._capacity.available_bytes()
        if (
            isinstance(available, bool)
            or not isinstance(available, int)
            or available < 0
        ):
            raise RuntimeError("campaign capacity evidence is invalid")
        return available >= required_remaining_capacity_bytes(
            self._definition,
            state,
            margin_bytes=self._capacity_margin_bytes,
        )

    def _next_requested_start(self, slot_index: int) -> UtcNs:
        return UtcNs(
            int(self._definition.start_utc_ns)
            + (
                slot_index
                * self._definition.slot_period_numerator_ns
                // self._definition.slot_period_denominator
            )
        )

    def _run_capture(
        self, state: CampaignJournalState, record: CampaignUnitRecord
    ) -> CampaignRunResult:
        invoked = replace(record, capture_invocations=record.capture_invocations + 1)
        state = self._replace_last(state, invoked)
        try:
            snapshot = self._capture.capture(
                record.unit,
                not_before_utc_ns=record.unit.requested_start_utc_ns,
                deadline_utc_ns=self._slot_deadline(record.unit),
            )
            self._validate_snapshot(record.unit, snapshot)
        except Exception:  # noqa: BLE001 - uncertain exact identity must be replayed
            return CampaignRunResult(
                CampaignRunStatus.CAPTURE_UNCERTAIN, state, record.unit
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
            failed = replace(
                invoked,
                phase=CampaignUnitPhase.TERMINAL_FAILED,
                snapshot=snapshot,
            )
            state = self._replace_last(state, failed)
            return CampaignRunResult(
                CampaignRunStatus.CAPTURE_FAILED, state, record.unit
            )
        captured = replace(invoked, phase=CampaignUnitPhase.CAPTURED, snapshot=snapshot)
        state = self._replace_last(state, captured)
        return self._run_analysis(state, captured, record.unit.requested_start_utc_ns)

    def _run_analysis(
        self,
        state: CampaignJournalState,
        record: CampaignUnitRecord,
        now_utc_ns: UtcNs,
    ) -> CampaignRunResult:
        if record.snapshot is None:
            raise RuntimeError("captured campaign unit has no terminal snapshot")
        if record.analysis_invocations >= self._definition.maximum_analysis_invocations:
            return CampaignRunResult(
                CampaignRunStatus.ANALYSIS_FAILED, state, record.unit
            )
        if now_utc_ns >= self._slot_deadline(record.unit):
            return CampaignRunResult(
                CampaignRunStatus.ANALYSIS_FAILED, state, record.unit
            )
        invoked = replace(record, analysis_invocations=record.analysis_invocations + 1)
        state = self._replace_last(state, invoked)
        try:
            receipt = self._analysis.analyze(
                record.snapshot, deadline_utc_ns=self._slot_deadline(record.unit)
            )
            self._validate_analysis_receipt(record.snapshot, receipt)
        except Exception:  # noqa: BLE001 - explicit snapshot remains the retry point
            failed = replace(invoked, phase=CampaignUnitPhase.ANALYSIS_FAILED)
            state = self._replace_last(state, failed)
            return CampaignRunResult(
                CampaignRunStatus.ANALYSIS_FAILED, state, record.unit
            )
        if receipt.completed_utc_ns > self._slot_deadline(record.unit):
            failed = replace(
                invoked,
                phase=CampaignUnitPhase.ANALYSIS_FAILED,
                analysis_receipt=receipt,
            )
            state = self._replace_last(state, failed)
            return CampaignRunResult(
                CampaignRunStatus.ANALYSIS_FAILED, state, record.unit
            )
        complete = replace(
            invoked,
            phase=CampaignUnitPhase.COMPLETE,
            analysis_receipt=receipt,
        )
        state = self._replace_last(state, complete)
        return CampaignRunResult(CampaignRunStatus.UNIT_COMPLETE, state, record.unit)

    def _slot_deadline(self, unit: CampaignUnit) -> UtcNs:
        if self._definition.qualification:
            return UtcNs(
                int(unit.requested_start_utc_ns)
                + self._definition.slot_period_numerator_ns
                // self._definition.slot_period_denominator
            )
        return UtcNs(
            int(self._definition.start_utc_ns)
            + ((unit.slot_index + 1) * self._definition.slot_period_numerator_ns)
            // self._definition.slot_period_denominator
        )

    @staticmethod
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
            raise RuntimeError(
                "analysis receipt identifies another batch or recordings"
            )

    def _validate_snapshot(
        self, unit: CampaignUnit, snapshot: CaptureBatchSnapshot
    ) -> None:
        if not snapshot.terminal or snapshot.definition != unit.batch:
            raise RuntimeError("capture returned another or incomplete batch")

    def _replace_last(
        self, state: CampaignJournalState, replacement: CampaignUnitRecord
    ) -> CampaignJournalState:
        if not state.records or state.records[-1].unit != replacement.unit:
            raise RuntimeError("campaign journal transition targets another unit")
        return self._store(
            replace(state, records=(*state.records[:-1], replacement)), state
        )

    def _store(
        self, replacement: CampaignJournalState, current: CampaignJournalState
    ) -> CampaignJournalState:
        return self._journal.compare_and_swap(
            self._definition,
            current.revision,
            replace(replacement, revision=current.revision + 1),
        )


def campaign_cell(success_index: int) -> CampaignCell:
    if not 0 <= success_index < CAMPAIGN_SUCCESS_TARGET:
        raise ValueError("campaign success index is outside the reviewed schedule")
    return CAMPAIGN_CELLS[success_index % SLOTS_PER_ROUND]


def campaign_edge_order(
    definition: CampaignDefinition, *, success_index: int, side: str
) -> str:
    """Return the reviewed per-radio edge order for one accepted-cell index."""

    if side not in {"a", "b"}:
        raise ValueError("campaign station side must be a or b")
    if not 0 <= success_index < definition.target_successes:
        raise ValueError("campaign success index is outside the reviewed schedule")
    if definition.qualification:
        return "L" if success_index % 2 == 0 else "U"
    geometry = MAIN_GEOMETRY_SCHEDULE[success_index % len(MAIN_GEOMETRY_SCHEDULE)]
    return geometry[0 if side == "a" else 1]


def required_remaining_capacity_bytes(
    definition: CampaignDefinition,
    state: CampaignJournalState,
    *,
    margin_bytes: int,
) -> int:
    """Worst-case raw staging + durable copy reserve for remaining successes."""

    if (
        isinstance(margin_bytes, bool)
        or not isinstance(margin_bytes, int)
        or margin_bytes < 0
    ):
        raise ValueError("campaign capacity margin must be non-negative")
    target_per_cell = 1 if definition.qualification else CAMPAIGN_ROUNDS
    remaining_raw = sum(
        max(0, target_per_cell - completed) * cell.sample_count * 128
        for cell, completed in zip(CAMPAIGN_CELLS, state.successful_counts, strict=True)
    )
    return remaining_raw * 2 + margin_bytes


def build_campaign_unit(
    definition: CampaignDefinition,
    *,
    success_index: int,
    slot_index: int,
    retry_index: int,
    requested_start_utc_ns: UtcNs,
) -> CampaignUnit:
    cell = campaign_cell(success_index)
    stem = f"{definition.campaign_id}_u{success_index:03d}_s{slot_index:03d}_r{retry_index:02d}"
    plan_a = PlanId(f"plan_{stem}_a")
    plan_b = PlanId(f"plan_{stem}_b")
    attempts = (
        ExpectedCaptureAttempt(
            CaptureAttemptId(f"cattempt_{stem}_a"),
            definition.radio_a_id,
            plan_a,
            requested_start_utc_ns,
        ),
        ExpectedCaptureAttempt(
            CaptureAttemptId(f"cattempt_{stem}_b"),
            definition.radio_b_id,
            plan_b,
            requested_start_utc_ns,
        ),
    )
    batch = CaptureBatchDefinition(
        SchemaRef(CaptureBatchDefinition.SCHEMA_ID),
        CaptureBatchId(f"cbatch_{stem}"),
        CaptureBatchMode.COORDINATED,
        attempts,
        definition.maximum_observed_start_skew_ns,
    )
    return CampaignUnit(
        success_index,
        slot_index,
        retry_index,
        cell,
        requested_start_utc_ns,
        batch,
        plan_a,
        plan_b,
    )


def materialize_campaign_station(
    definition: CampaignDefinition,
    base: V5CaptureStation,
    unit: CampaignUnit,
    *,
    side: str,
    campaign_state_root: Path,
) -> V5CaptureStation:
    """Bind one fresh unit plan to one exact reviewed radio/runtime identity."""

    if side not in {"a", "b"}:
        raise ValueError("campaign station side must be a or b")
    expected_digest = (
        definition.station_a_digest if side == "a" else definition.station_b_digest
    )
    expected_radio = definition.radio_a_id if side == "a" else definition.radio_b_id
    plan_id = unit.plan_a_id if side == "a" else unit.plan_b_id
    if (
        base.specification_digest != expected_digest
        or base.radio.radio_id != expected_radio
    ):
        raise ValueError("campaign base station differs from its reviewed identity")
    if not campaign_state_root.is_absolute() or ".." in campaign_state_root.parts:
        raise ValueError("campaign state root must be absolute and normalized")
    edge_order = campaign_edge_order(
        definition, success_index=unit.success_index, side=side
    )
    provisional = replace(
        base.plan,
        plan_id=plan_id,
        plan_digest=Digest.sha256(b"provisional"),
        sample_rate_hz=float(unit.cell.sample_rate_hz),
        bandwidth_hz=float(unit.cell.sample_rate_hz),
        sample_count=unit.cell.sample_count,
        edge_order=edge_order,
        edge_order_draw_u32=0 if edge_order == "L" else 1,
        arm_name=(
            f"{definition.campaign_id}-{unit.success_index:03d}-"
            f"{unit.cell.duration_ms}ms-{unit.cell.sample_rate_hz}Sps"
        ),
        hardware_block_samples=unit.cell.hardware_block_samples,
        allow_clipped_pilot=unit.cell.sample_rate_hz < 1_875_000,
    )
    root = campaign_unit_state_root(campaign_state_root, unit) / f"radio-{side}"
    capture_plan = build_starlink_edge_scan_plan(
        StarlinkEdgeScanSpec(
            plan_id=plan_id,
            radio_id=base.radio.radio_id,
            receiver_chain_ids=base.radio.receiver_chain_ids,
            gain=GainSetting(GainMode.AGC),
            sample_rate_hz=provisional.sample_rate_hz,
            bandwidth_hz=provisional.bandwidth_hz,
            sample_count=provisional.sample_count,
            edge_order=provisional.edge_order,
            lnb_lo_hz=provisional.lnb_lo_hz,
            edge_order_draw_u32=provisional.edge_order_draw_u32,
            arm_name=provisional.arm_name,
            hardware_block_samples=provisional.hardware_block_samples,
            allow_clipped_pilot=provisional.allow_clipped_pilot,
        )
    )
    final_plan = replace(provisional, plan_digest=canonical_digest(capture_plan))
    return replace(
        base,
        plan=final_plan,
        state=replace(
            base.state,
            state_root=root,
            recording_root=root / "recordings",
            spool_database=root / "capture-spool.sqlite3",
            lock_path=root / "instance.lock",
        ),
    )


def campaign_unit_state_root(campaign_state_root: Path, unit: CampaignUnit) -> Path:
    """Return the immutable state namespace for one exact campaign attempt."""

    if not campaign_state_root.is_absolute() or ".." in campaign_state_root.parts:
        raise ValueError("campaign state root must be absolute and normalized")
    return (
        campaign_state_root
        / "units"
        / (f"u{unit.success_index:03d}_s{unit.slot_index:03d}_r{unit.retry_index:02d}")
    )


@dataclass(frozen=True, slots=True)
class CampaignQualificationReceipt:
    qualification_definition_digest: Digest
    issued_utc_ns: UtcNs
    unit_digests: tuple[Digest, ...]
    snapshot_digests: tuple[Digest, ...]
    analysis_receipt_digests: tuple[Digest, ...]
    successful_counts: tuple[int, ...]

    def __post_init__(self) -> None:
        if int(self.issued_utc_ns) < 0:
            raise ValueError("qualification receipt time must be non-negative")
        if (
            len(self.unit_digests) != SLOTS_PER_ROUND
            or len(self.snapshot_digests) != SLOTS_PER_ROUND
        ):
            raise ValueError("qualification receipt requires nine exact units")
        if len(self.analysis_receipt_digests) != SLOTS_PER_ROUND:
            raise ValueError("qualification receipt requires nine analysis receipts")
        if self.successful_counts != (1,) * SLOTS_PER_ROUND:
            raise ValueError("qualification receipt must be exactly balanced")

    def document(self) -> dict[str, object]:
        return {
            "schema": "org.leo-flow.gauss-v5-campaign-qualification/v1",
            "qualification_definition_digest": str(
                self.qualification_definition_digest
            ),
            "issued_utc_ns": int(self.issued_utc_ns),
            "unit_digests": [str(item) for item in self.unit_digests],
            "snapshot_digests": [str(item) for item in self.snapshot_digests],
            "analysis_receipt_digests": [
                str(item) for item in self.analysis_receipt_digests
            ],
            "successful_counts": list(self.successful_counts),
        }

    @property
    def digest(self) -> Digest:
        return canonical_digest(self.document())


def build_qualification_receipt(
    definition: CampaignDefinition,
    state: CampaignJournalState,
    *,
    issued_utc_ns: UtcNs,
) -> CampaignQualificationReceipt:
    if not definition.qualification or state.definition_digest != definition.digest:
        raise ValueError("qualification state belongs to another definition")
    complete = tuple(
        item for item in state.records if item.phase is CampaignUnitPhase.COMPLETE
    )
    if (
        len(complete) != SLOTS_PER_ROUND
        or state.successful_counts != (1,) * SLOTS_PER_ROUND
    ):
        raise ValueError("qualification round is incomplete")
    if any(item.snapshot is None or item.analysis_receipt is None for item in complete):
        raise ValueError("qualification recording evidence is incomplete")
    return CampaignQualificationReceipt(
        definition.digest,
        issued_utc_ns,
        tuple(item.unit.digest for item in complete),
        tuple(canonical_digest(item.snapshot) for item in complete),
        tuple(canonical_digest(item.analysis_receipt) for item in complete),
        state.successful_counts,
    )
