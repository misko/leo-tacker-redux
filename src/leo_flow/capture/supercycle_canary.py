"""Finite capture-first policy for one reviewed 36-slot Gauss canary.

This is intentionally separate from qualification and the 936-slot main
campaign.  A canary definition cannot be decoded by either campaign codec and
its receipt has no main-campaign authorization semantics.
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
    CaptureAttemptId,
    CaptureBatchId,
    Digest,
    DigestAlgorithm,
    PlanId,
    RadioId,
    SchemaRef,
    UtcNs,
    canonical_digest,
)

from .campaign import (
    CAMPAIGN_CELLS,
    MAIN_GEOMETRY_SCHEDULE,
    CampaignAnalysisPort,
    CampaignAnalysisReceipt,
    CampaignCapacityPort,
    CampaignCapturePort,
    CampaignCell,
    CampaignUnit,
)
from .scan_plan import StarlinkEdgeScanSpec, build_starlink_edge_scan_plan
from .v5_station import V5CaptureStation

CANARY_SCHEMA = "org.leo-flow.gauss-v5-supercycle-canary/v1"
CANARY_RECEIPT_SCHEMA = "org.leo-flow.gauss-v5-supercycle-canary-receipt/v1"
CANARY_SCHEDULE_POLICY = "single_nine_cell_four_geometry_no_catch_up_supercycle"
CANARY_SLOTS = 36
CANARY_RECORDINGS = 72
CANARY_SLOT_PERIOD_NUMERATOR_NS = 400_000_000_000
CANARY_SLOT_PERIOD_DENOMINATOR = 13
CANARY_PREFLIGHT_LEAD_NS = 15_000_000_000
CANARY_MAXIMUM_SKEW_NS = 100_000_000
CANARY_HARDWARE_BLOCK_DURATION_MS = 40
CANARY_RAW_BYTES = 1_254_400_000
CANARY_CAPTURE_TRANSITION_LIMIT = 2 * CANARY_SLOTS + 1
CANARY_ANALYSIS_TRANSITION_LIMIT = CANARY_SLOTS + 1
CANARY_COMPUTE_WORKERS = 8
CANARY_PROJECTION_WORKERS = 4
V8_QUALIFICATION_RECEIPT_DIGEST = Digest(
    DigestAlgorithm.SHA256,
    "6a816b5da9be8cb86361610fe42004074512e64b0748de2be2b62e832cdbcb8d",
)
_CANARY_ID = re.compile(r"canary_[a-z0-9][a-z0-9_-]{0,31}")


@dataclass(frozen=True, slots=True)
class SupercycleCanaryDefinition:
    canary_id: str
    start_utc_ns: UtcNs
    radio_a_id: RadioId
    radio_b_id: RadioId
    station_a_digest: Digest
    station_b_digest: Digest
    maximum_start_lateness_ns: int
    qualification_receipt_digest: Digest

    def __post_init__(self) -> None:
        if _CANARY_ID.fullmatch(self.canary_id) is None:
            raise ValueError("canary ID must use the dedicated canary_ namespace")
        if int(self.start_utc_ns) < 0:
            raise ValueError("canary start must be non-negative")
        if self.radio_a_id == self.radio_b_id:
            raise ValueError("canary radios must be distinct")
        if self.qualification_receipt_digest != V8_QUALIFICATION_RECEIPT_DIGEST:
            raise ValueError("canary requires the exact reviewed v8 receipt")
        if (
            isinstance(self.maximum_start_lateness_ns, bool)
            or not isinstance(self.maximum_start_lateness_ns, int)
            or not 0 <= self.maximum_start_lateness_ns <= 5_000_000_000
        ):
            raise ValueError("canary lateness bound must be within five seconds")

    @property
    def target_successes(self) -> int:
        return CANARY_SLOTS

    @property
    def end_utc_ns(self) -> UtcNs:
        return UtcNs(
            int(self.start_utc_ns)
            + CANARY_SLOTS
            * CANARY_SLOT_PERIOD_NUMERATOR_NS
            // CANARY_SLOT_PERIOD_DENOMINATOR
        )

    @property
    def digest(self) -> Digest:
        return canonical_digest(self.document())

    def document(self) -> dict[str, object]:
        return {
            "schema": CANARY_SCHEMA,
            "canary_id": self.canary_id,
            "campaign_kind": "supercycle_canary",
            "authorization_scope": "canary_only",
            "main_campaign_authorized": False,
            "start_utc_ns": int(self.start_utc_ns),
            "radios": [str(self.radio_a_id), str(self.radio_b_id)],
            "station_digests": [
                str(self.station_a_digest),
                str(self.station_b_digest),
            ],
            "qualification_receipt_digest": str(self.qualification_receipt_digest),
            "slots": CANARY_SLOTS,
            "recordings": CANARY_RECORDINGS,
            "cells": [cell.document() for cell in CAMPAIGN_CELLS],
            "geometry_schedule": [list(item) for item in MAIN_GEOMETRY_SCHEDULE],
            "unit_schedule": CANARY_SCHEDULE_POLICY,
            "slot_period_numerator_ns": CANARY_SLOT_PERIOD_NUMERATOR_NS,
            "slot_period_denominator": CANARY_SLOT_PERIOD_DENOMINATOR,
            "preflight_lead_ns": CANARY_PREFLIGHT_LEAD_NS,
            "maximum_start_lateness_ns": self.maximum_start_lateness_ns,
            "maximum_observed_start_skew_ns": CANARY_MAXIMUM_SKEW_NS,
            "hardware_block_duration_ms": CANARY_HARDWARE_BLOCK_DURATION_MS,
            "capture_mode": CaptureBatchMode.COORDINATED.value,
            "capture_first": True,
            "no_catch_up": True,
            "replay_allowed": False,
            "raw_bytes": CANARY_RAW_BYTES,
            "capture_transition_limit": CANARY_CAPTURE_TRANSITION_LIMIT,
            "analysis_transition_limit": CANARY_ANALYSIS_TRANSITION_LIMIT,
            "staged_analysis": {
                "migration_head": "0030_campaign_scoped_analysis_claims.sql",
                "window_batches": CANARY_SLOTS,
                "compute_workers": CANARY_COMPUTE_WORKERS,
                "projection_workers": CANARY_PROJECTION_WORKERS,
            },
            "result_semantics": "candidate_only_no_detection_count",
        }


def canary_cell(slot_index: int) -> CampaignCell:
    if not 0 <= slot_index < CANARY_SLOTS:
        raise ValueError("canary slot is outside the reviewed supercycle")
    return CAMPAIGN_CELLS[slot_index % len(CAMPAIGN_CELLS)]


def canary_geometry(slot_index: int) -> tuple[str, str]:
    if not 0 <= slot_index < CANARY_SLOTS:
        raise ValueError("canary slot is outside the reviewed supercycle")
    return MAIN_GEOMETRY_SCHEDULE[slot_index % len(MAIN_GEOMETRY_SCHEDULE)]


def build_canary_unit(
    definition: SupercycleCanaryDefinition, *, slot_index: int
) -> CampaignUnit:
    cell = canary_cell(slot_index)
    requested = UtcNs(
        int(definition.start_utc_ns)
        + slot_index * CANARY_SLOT_PERIOD_NUMERATOR_NS // CANARY_SLOT_PERIOD_DENOMINATOR
    )

    stem = f"{definition.canary_id}_u{slot_index:03d}_s{slot_index:03d}_r00"
    plan_a = PlanId(f"plan_{stem}_a")
    plan_b = PlanId(f"plan_{stem}_b")
    attempts = (
        ExpectedCaptureAttempt(
            CaptureAttemptId(f"cattempt_{stem}_a"),
            definition.radio_a_id,
            plan_a,
            requested,
        ),
        ExpectedCaptureAttempt(
            CaptureAttemptId(f"cattempt_{stem}_b"),
            definition.radio_b_id,
            plan_b,
            requested,
        ),
    )
    return CampaignUnit(
        slot_index,
        slot_index,
        0,
        cell,
        requested,
        CaptureBatchDefinition(
            SchemaRef(CaptureBatchDefinition.SCHEMA_ID),
            CaptureBatchId(f"cbatch_{stem}"),
            CaptureBatchMode.COORDINATED,
            attempts,
            CANARY_MAXIMUM_SKEW_NS,
        ),
        plan_a,
        plan_b,
    )


def materialize_canary_station(
    definition: SupercycleCanaryDefinition,
    base: V5CaptureStation,
    unit: CampaignUnit,
    *,
    side: str,
    canary_state_root: Path,
) -> V5CaptureStation:
    """Materialize only inside the dedicated canary state namespace."""
    if side not in {"a", "b"}:
        raise ValueError("canary station side must be a or b")
    expected_digest = (
        definition.station_a_digest if side == "a" else definition.station_b_digest
    )
    expected_radio = definition.radio_a_id if side == "a" else definition.radio_b_id
    plan_id = unit.plan_a_id if side == "a" else unit.plan_b_id
    if (
        base.specification_digest != expected_digest
        or base.radio.radio_id != expected_radio
    ):
        raise ValueError("canary base station differs from its reviewed identity")
    if (
        not canary_state_root.is_absolute()
        or ".." in canary_state_root.parts
        or "continuous" in canary_state_root.parts
        or "qualification" in canary_state_root.parts
    ):
        raise ValueError("canary state root is not isolated")
    edge_order = canary_geometry(unit.slot_index)[0 if side == "a" else 1]
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
            f"{definition.canary_id}-{unit.slot_index:03d}-"
            f"{unit.cell.duration_ms}ms-{unit.cell.sample_rate_hz}Sps"
        ),
        hardware_block_samples=unit.cell.hardware_block_samples,
        allow_clipped_pilot=unit.cell.sample_rate_hz < 1_875_000,
    )
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
    root = (
        canary_state_root
        / definition.canary_id
        / "units"
        / f"u{unit.slot_index:03d}_s{unit.slot_index:03d}_r00"
        / f"radio-{side}"
    )
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


class CanaryPhase(str, Enum):
    CAPTURING = "capturing"
    ANALYZING = "analyzing"
    COMPLETE = "complete"
    HALTED = "halted"


class CanaryHaltReason(str, Enum):
    MISSED_SLOT = "missed_slot"
    CAPTURE_UNCERTAIN = "capture_uncertain"
    INTEGRITY_FAILURE = "integrity_failure"
    ANALYSIS_FAILURE = "analysis_failure"


class CanaryRecordPhase(str, Enum):
    PLANNED = "planned"
    CAPTURE_INVOKED = "capture_invoked"
    CAPTURED = "captured"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class CanaryRecord:
    unit: CampaignUnit
    phase: CanaryRecordPhase = CanaryRecordPhase.PLANNED
    snapshot: CaptureBatchSnapshot | None = None
    analysis_receipt: CampaignAnalysisReceipt | None = None

    def __post_init__(self) -> None:
        if (
            self.unit.retry_index != 0
            or self.unit.success_index != self.unit.slot_index
        ):
            raise ValueError("canary record identity permits no replay")
        if self.phase in {
            CanaryRecordPhase.PLANNED,
            CanaryRecordPhase.CAPTURE_INVOKED,
        }:
            if self.snapshot is not None or self.analysis_receipt is not None:
                raise ValueError("planned canary record carries terminal evidence")
        elif self.snapshot is None:
            raise ValueError("terminal canary record requires its snapshot")
        if (self.phase is CanaryRecordPhase.COMPLETE) != (
            self.analysis_receipt is not None
        ):
            raise ValueError("canary analysis receipt differs from record phase")


@dataclass(frozen=True, slots=True)
class CanaryState:
    definition_digest: Digest
    phase: CanaryPhase = CanaryPhase.CAPTURING
    records: tuple[CanaryRecord, ...] = ()
    halt_reason: CanaryHaltReason | None = None
    revision: int = 0

    def __post_init__(self) -> None:
        if self.revision < 0 or len(self.records) > CANARY_SLOTS:
            raise ValueError("canary state bounds differ")
        if (self.phase is CanaryPhase.HALTED) != (self.halt_reason is not None):
            raise ValueError("canary halt evidence differs from phase")
        if tuple(record.unit.slot_index for record in self.records) != tuple(
            range(len(self.records))
        ):
            raise ValueError("canary record schedule is not canonical")
        if any(
            record.phase
            in {CanaryRecordPhase.PLANNED, CanaryRecordPhase.CAPTURE_INVOKED}
            for record in self.records[:-1]
        ):
            raise ValueError("only the last canary record may remain nonterminal")
        if self.phase in {CanaryPhase.ANALYZING, CanaryPhase.COMPLETE} and any(
            record.phase
            in {CanaryRecordPhase.PLANNED, CanaryRecordPhase.CAPTURE_INVOKED}
            for record in self.records
        ):
            raise ValueError("closed canary retains a planned capture")
        if self.phase is CanaryPhase.COMPLETE and self.analyzed_count != CANARY_SLOTS:
            raise ValueError("complete canary lacks 36 analysis receipts")

    @property
    def captured_count(self) -> int:
        return sum(record.snapshot is not None for record in self.records)

    @property
    def analyzed_count(self) -> int:
        return sum(
            record.phase is CanaryRecordPhase.COMPLETE for record in self.records
        )


class CanaryJournal(Protocol):
    def initialize(self, definition: SupercycleCanaryDefinition) -> CanaryState: ...
    def load(self, definition: SupercycleCanaryDefinition) -> CanaryState: ...
    def compare_and_swap(
        self,
        definition: SupercycleCanaryDefinition,
        expected_revision: int,
        replacement: CanaryState,
    ) -> CanaryState: ...


class InMemoryCanaryJournal:
    def __init__(self) -> None:
        self._state: CanaryState | None = None

    def initialize(self, definition: SupercycleCanaryDefinition) -> CanaryState:
        if self._state is None:
            self._state = CanaryState(definition.digest)
        return self.load(definition)

    def load(self, definition: SupercycleCanaryDefinition) -> CanaryState:
        if self._state is None or self._state.definition_digest != definition.digest:
            raise RuntimeError("canary journal definition differs")
        return self._state

    def compare_and_swap(
        self,
        definition: SupercycleCanaryDefinition,
        expected_revision: int,
        replacement: CanaryState,
    ) -> CanaryState:
        current = self.load(definition)
        if (
            current.revision != expected_revision
            or replacement.definition_digest != definition.digest
            or replacement.revision != expected_revision + 1
        ):
            raise RuntimeError("canary journal revision changed")
        self._state = replacement
        return replacement


class CanaryStatus(str, Enum):
    NOT_DUE = "not_due"
    CAPTURED = "captured"
    CAPTURE_PHASE_CLOSED = "capture_phase_closed"
    ANALYZED = "analyzed"
    COMPLETE = "complete"
    CAPACITY_BLOCKED = "capacity_blocked"
    HALTED = "halted"


@dataclass(frozen=True, slots=True)
class CanaryResult:
    status: CanaryStatus
    state: CanaryState
    unit: CampaignUnit | None = None


class SupercycleCanaryCoordinator:
    """Advance one no-replay capture or receipt reconciliation transition."""

    def __init__(
        self,
        definition: SupercycleCanaryDefinition,
        journal: CanaryJournal,
        capture: CampaignCapturePort,
        analysis: CampaignAnalysisPort,
        capacity: CampaignCapacityPort,
        *,
        capacity_margin_bytes: int,
    ) -> None:
        if (
            isinstance(capacity_margin_bytes, bool)
            or not isinstance(capacity_margin_bytes, int)
            or capacity_margin_bytes < 0
        ):
            raise ValueError("canary capacity margin must be non-negative")
        self.definition = definition
        self.journal = journal
        self.capture = capture
        self.analysis = analysis
        self.capacity = capacity
        self.capacity_margin_bytes = capacity_margin_bytes
        journal.initialize(definition)

    def capture_next(self, now_utc_ns: UtcNs) -> CanaryResult:
        state = self.journal.load(self.definition)
        if state.phase is CanaryPhase.HALTED:
            return CanaryResult(CanaryStatus.HALTED, state)
        if state.phase is not CanaryPhase.CAPTURING:
            return CanaryResult(CanaryStatus.CAPTURE_PHASE_CLOSED, state)
        if state.captured_count == CANARY_SLOTS:
            closed = self._store(replace(state, phase=CanaryPhase.ANALYZING), state)
            return CanaryResult(CanaryStatus.CAPTURE_PHASE_CLOSED, closed)
        if (
            state.records
            and state.records[-1].phase is CanaryRecordPhase.CAPTURE_INVOKED
        ):
            return self._halt(
                state, CanaryHaltReason.CAPTURE_UNCERTAIN, state.records[-1].unit
            )
        if state.records and state.records[-1].phase is CanaryRecordPhase.PLANNED:
            record = state.records[-1]
        else:
            unit = build_canary_unit(self.definition, slot_index=state.captured_count)
            state = self._store(
                replace(state, records=(*state.records, CanaryRecord(unit))), state
            )
            record = state.records[-1]
        requested = int(record.unit.requested_start_utc_ns)
        now = int(now_utc_ns)
        if now < requested - CANARY_PREFLIGHT_LEAD_NS:
            return CanaryResult(CanaryStatus.NOT_DUE, state, record.unit)
        if now > requested + self.definition.maximum_start_lateness_ns:
            return self._halt(state, CanaryHaltReason.MISSED_SLOT, record.unit)
        if self.capacity.available_bytes() < self.required_remaining_capacity(state):
            return CanaryResult(CanaryStatus.CAPACITY_BLOCKED, state, record.unit)
        record = replace(record, phase=CanaryRecordPhase.CAPTURE_INVOKED)
        state = self._replace_last(state, record)
        try:
            snapshot = self.capture.capture(
                record.unit,
                not_before_utc_ns=record.unit.requested_start_utc_ns,
                deadline_utc_ns=self._slot_deadline(record.unit.slot_index),
            )
        except Exception:  # noqa: BLE001 - no replay is permitted
            return self._halt(state, CanaryHaltReason.CAPTURE_UNCERTAIN, record.unit)
        if (
            not snapshot.terminal
            or snapshot.definition != record.unit.batch
            or snapshot.paired_analysis_eligibility
            is not PairedAnalysisEligibility.ELIGIBLE
            or any(
                outcome.state is not CaptureAttemptState.SUCCEEDED
                or outcome.terminal_utc_ns > self._slot_deadline(record.unit.slot_index)
                for outcome in snapshot.outcomes
            )
        ):
            return self._halt(state, CanaryHaltReason.INTEGRITY_FAILURE, record.unit)
        replacement = replace(
            record, phase=CanaryRecordPhase.CAPTURED, snapshot=snapshot
        )
        state = self._replace_last(state, replacement)
        return CanaryResult(CanaryStatus.CAPTURED, state, record.unit)

    def reconcile_next(self, *, deadline_utc_ns: UtcNs) -> CanaryResult:
        state = self.journal.load(self.definition)
        if state.phase is CanaryPhase.HALTED:
            return CanaryResult(CanaryStatus.HALTED, state)
        if state.phase is CanaryPhase.CAPTURING:
            return CanaryResult(CanaryStatus.CAPTURE_PHASE_CLOSED, state)
        candidate = (
            next(
                (i, record)
                for i, record in enumerate(state.records)
                if record.phase is CanaryRecordPhase.CAPTURED
            )
            if state.analyzed_count < CANARY_SLOTS
            else None
        )
        if candidate is None:
            completed = self._store(replace(state, phase=CanaryPhase.COMPLETE), state)
            return CanaryResult(CanaryStatus.COMPLETE, completed)
        index, record = candidate
        assert record.snapshot is not None
        try:
            receipt = self.analysis.analyze(
                record.snapshot, deadline_utc_ns=deadline_utc_ns
            )
            if receipt.batch_id != record.snapshot.batch_id:
                raise RuntimeError("canary analysis receipt identifies another batch")
            expected = tuple(
                sorted(
                    (
                        item.recording_id
                        for item in record.snapshot.successful_recordings
                    ),
                    key=str,
                )
            )
            if (
                receipt.recording_ids != expected
                or receipt.completed_utc_ns > deadline_utc_ns
            ):
                raise RuntimeError("canary analysis receipt closure differs")
        except Exception:  # noqa: BLE001 - analysis failure is terminal
            return self._halt(state, CanaryHaltReason.ANALYSIS_FAILURE, record.unit)
        records = list(state.records)
        records[index] = replace(
            record, phase=CanaryRecordPhase.COMPLETE, analysis_receipt=receipt
        )
        state = self._store(replace(state, records=tuple(records)), state)
        return CanaryResult(CanaryStatus.ANALYZED, state, record.unit)

    def required_remaining_capacity(self, state: CanaryState) -> int:
        remaining_raw = sum(
            canary_cell(index).sample_count * 128
            for index in range(state.captured_count, CANARY_SLOTS)
        )
        return remaining_raw * 2 + self.capacity_margin_bytes

    def halt_analysis(self) -> CanaryResult:
        """Persist a terminal analysis failure without retrying any capture."""
        state = self.journal.load(self.definition)
        if state.phase is CanaryPhase.HALTED:
            return CanaryResult(CanaryStatus.HALTED, state)
        if state.phase is not CanaryPhase.ANALYZING:
            raise RuntimeError("canary analysis phase is not open")
        unit = next(
            record.unit
            for record in state.records
            if record.phase is CanaryRecordPhase.CAPTURED
        )
        return self._halt(state, CanaryHaltReason.ANALYSIS_FAILURE, unit)

    def _slot_deadline(self, slot_index: int) -> UtcNs:
        return UtcNs(
            int(self.definition.start_utc_ns)
            + (slot_index + 1)
            * CANARY_SLOT_PERIOD_NUMERATOR_NS
            // CANARY_SLOT_PERIOD_DENOMINATOR
        )

    def _halt(
        self, state: CanaryState, reason: CanaryHaltReason, unit: CampaignUnit
    ) -> CanaryResult:
        halted = self._store(
            replace(state, phase=CanaryPhase.HALTED, halt_reason=reason), state
        )
        return CanaryResult(CanaryStatus.HALTED, halted, unit)

    def _replace_last(self, state: CanaryState, record: CanaryRecord) -> CanaryState:
        if not state.records or state.records[-1].unit != record.unit:
            raise RuntimeError("canary transition targets another unit")
        return self._store(replace(state, records=(*state.records[:-1], record)), state)

    def _store(self, replacement: CanaryState, current: CanaryState) -> CanaryState:
        return self.journal.compare_and_swap(
            self.definition,
            current.revision,
            replace(replacement, revision=current.revision + 1),
        )


CANARY_ANALYSIS_STAGES = (
    "feature_compute",
    "feature_projection",
    "waterfall_compute",
    "waterfall_projection",
    "starlink_suite_compute",
    "starlink_suite_projection",
)


@dataclass(frozen=True, slots=True)
class CanaryStageBenchmark:
    stage: str
    workers: int
    wall_time_ns: int
    cpu_time_ns: int
    peak_rss_bytes: int

    def __post_init__(self) -> None:
        if self.stage not in CANARY_ANALYSIS_STAGES:
            raise ValueError("canary benchmark stage is not reviewed")
        expected_workers = 8 if self.stage.endswith("compute") else 4
        if self.workers != expected_workers:
            raise ValueError("canary benchmark worker count differs")
        if (
            isinstance(self.wall_time_ns, bool)
            or self.wall_time_ns <= 0
            or isinstance(self.cpu_time_ns, bool)
            or self.cpu_time_ns < 0
            or isinstance(self.peak_rss_bytes, bool)
            or self.peak_rss_bytes <= 0
        ):
            raise ValueError("canary benchmark resource evidence is invalid")

    def document(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "workers": self.workers,
            "wall_time_ns": self.wall_time_ns,
            "cpu_time_ns": self.cpu_time_ns,
            "peak_rss_bytes": self.peak_rss_bytes,
        }


@dataclass(frozen=True, slots=True)
class SupercycleCanaryReceipt:
    definition_digest: Digest
    qualification_receipt_digest: Digest
    issued_utc_ns: UtcNs
    unit_digests: tuple[Digest, ...]
    snapshot_digests: tuple[Digest, ...]
    analysis_receipt_digests: tuple[Digest, ...]
    recording_ids: tuple[str, ...]
    capture_completion_latency_ns: tuple[int, ...]
    observed_skew_ns: tuple[int, ...]
    benchmarks: tuple[CanaryStageBenchmark, ...]
    feature_set_count: int
    waterfall_count: int
    starlink_suite_terminal_count: int
    dashboard_recording_count: int

    def __post_init__(self) -> None:
        if self.qualification_receipt_digest != V8_QUALIFICATION_RECEIPT_DIGEST:
            raise ValueError("canary receipt is not bound to v8 qualification")
        if int(self.issued_utc_ns) < 0:
            raise ValueError("canary receipt issue time is invalid")
        if not all(
            len(values) == expected
            for values, expected in (
                (self.unit_digests, CANARY_SLOTS),
                (self.snapshot_digests, CANARY_SLOTS),
                (self.analysis_receipt_digests, CANARY_SLOTS),
                (self.recording_ids, CANARY_RECORDINGS),
                (self.capture_completion_latency_ns, CANARY_SLOTS),
                (self.observed_skew_ns, CANARY_SLOTS),
            )
        ):
            raise ValueError("canary receipt inventory is incomplete")
        if len(set(self.recording_ids)) != CANARY_RECORDINGS:
            raise ValueError("canary receipt recording identities are reused")
        if any(value < 0 for value in self.capture_completion_latency_ns):
            raise ValueError("canary capture timing is invalid")
        if any(
            not 0 <= value < CANARY_MAXIMUM_SKEW_NS for value in self.observed_skew_ns
        ):
            raise ValueError("canary receipt exceeds the skew bound")
        if tuple(item.stage for item in self.benchmarks) != CANARY_ANALYSIS_STAGES:
            raise ValueError("canary benchmark stage order differs")
        if (
            self.feature_set_count,
            self.waterfall_count,
            self.starlink_suite_terminal_count,
            self.dashboard_recording_count,
        ) != (CANARY_RECORDINGS,) * 4:
            raise ValueError("canary 72-recording closure differs")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self.document())

    def document(self) -> dict[str, object]:
        return {
            "schema": CANARY_RECEIPT_SCHEMA,
            "authorization_scope": "canary_only",
            "main_campaign_authorized": False,
            "definition_digest": str(self.definition_digest),
            "qualification_receipt_digest": str(self.qualification_receipt_digest),
            "issued_utc_ns": int(self.issued_utc_ns),
            "unit_digests": [str(value) for value in self.unit_digests],
            "snapshot_digests": [str(value) for value in self.snapshot_digests],
            "analysis_receipt_digests": [
                str(value) for value in self.analysis_receipt_digests
            ],
            "recording_ids": list(self.recording_ids),
            "capture_completion_latency_ns": list(self.capture_completion_latency_ns),
            "observed_skew_ns": list(self.observed_skew_ns),
            "maximum_observed_skew_ns": max(self.observed_skew_ns),
            "benchmarks": [value.document() for value in self.benchmarks],
            "closure": {
                "feature_set_count": self.feature_set_count,
                "waterfall_count": self.waterfall_count,
                "starlink_suite_terminal_count": self.starlink_suite_terminal_count,
                "dashboard_recording_count": self.dashboard_recording_count,
            },
            "result_semantics": "candidate_only_no_detection_count",
        }


def build_canary_receipt(
    definition: SupercycleCanaryDefinition,
    state: CanaryState,
    *,
    issued_utc_ns: UtcNs,
    benchmarks: tuple[CanaryStageBenchmark, ...],
    feature_set_count: int,
    waterfall_count: int,
    starlink_suite_terminal_count: int,
    dashboard_recording_count: int,
) -> SupercycleCanaryReceipt:
    if (
        state.definition_digest != definition.digest
        or state.phase is not CanaryPhase.COMPLETE
        or len(state.records) != CANARY_SLOTS
    ):
        raise ValueError("canary state is not complete")
    snapshots = tuple(record.snapshot for record in state.records)
    receipts = tuple(record.analysis_receipt for record in state.records)
    if any(value is None for value in snapshots + receipts):
        raise ValueError("canary state lacks terminal evidence")
    exact_snapshots = tuple(value for value in snapshots if value is not None)
    exact_receipts = tuple(value for value in receipts if value is not None)
    recording_ids = tuple(
        str(recording.recording_id)
        for snapshot in exact_snapshots
        for recording in sorted(
            snapshot.successful_recordings, key=lambda item: str(item.recording_id)
        )
    )
    latency = tuple(
        max(int(outcome.terminal_utc_ns) for outcome in snapshot.outcomes)
        - int(record.unit.requested_start_utc_ns)
        for record, snapshot in zip(state.records, exact_snapshots, strict=True)
    )
    skew = tuple(
        abs(_observed_start(snapshot, 0) - _observed_start(snapshot, 1))
        for snapshot in exact_snapshots
    )
    return SupercycleCanaryReceipt(
        definition.digest,
        definition.qualification_receipt_digest,
        issued_utc_ns,
        tuple(record.unit.digest for record in state.records),
        tuple(canonical_digest(value) for value in exact_snapshots),
        tuple(canonical_digest(value) for value in exact_receipts),
        recording_ids,
        latency,
        skew,
        benchmarks,
        feature_set_count,
        waterfall_count,
        starlink_suite_terminal_count,
        dashboard_recording_count,
    )


def _observed_start(snapshot: CaptureBatchSnapshot, index: int) -> int:
    value = snapshot.outcomes[index].observed_start_utc_ns
    if value is None:
        raise ValueError("canary successful outcome lacks observed start")
    return int(value)
