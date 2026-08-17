from __future__ import annotations

import json
from dataclasses import replace
from io import StringIO
from pathlib import Path

import pytest

from leo_flow.adapters.supercycle_canary_sqlite import SQLiteSupercycleCanaryJournal
from leo_flow.capture.campaign import CAMPAIGN_CELLS
from leo_flow.capture.campaign_codec import decode_campaign_definition
from leo_flow.capture.supercycle_canary import (
    CANARY_ANALYSIS_STAGES,
    CANARY_CAPTURE_TRANSITION_LIMIT,
    CANARY_RAW_BYTES,
    CANARY_SLOTS,
    V8_QUALIFICATION_RECEIPT_DIGEST,
    CanaryPhase,
    CanaryRecord,
    CanaryRecordPhase,
    CanaryStageBenchmark,
    CanaryState,
    InMemoryCanaryJournal,
    SupercycleCanaryCoordinator,
    SupercycleCanaryDefinition,
    SupercycleCanaryReceipt,
    build_canary_unit,
    canary_cell,
    canary_geometry,
    materialize_canary_station,
)
from leo_flow.capture.supercycle_canary_codec import (
    decode_canary_definition,
    decode_canary_receipt,
    encode_canary_definition,
    encode_canary_receipt,
)
from leo_flow.capture.v5_station import load_v5_capture_station
from leo_flow.contracts.capture_batch import (
    CaptureAttemptOutcome,
    CaptureAttemptState,
    CaptureBatchSnapshot,
)
from leo_flow.contracts.core import (
    Digest,
    JobId,
    RecordingId,
    SchemaRef,
    UtcNs,
    canonical_digest,
)
from leo_flow.contracts.deferred_analysis import (
    DeferredAnalysisLaneResultV1,
    DeferredAnalysisLaneState,
    DeferredAnalysisStage,
    DeferredAnalysisWindowV1,
)
from leo_flow.contracts.storage import (
    ObjectRef,
    PublishedRecordingRef,
    RecordingObjectRef,
)
from leo_flow.deployments.supercycle_canary_analysis import (
    CanaryAnalysisError,
    SupercycleCanaryStagedAnalysis,
)
from leo_flow.deployments.v5_supercycle_canary_operator import main as canary_main

BASE_A = Path("deploy/v5-scan/gauss-r20-science-postreboot-passive-v1.station.json")
BASE_B = Path("deploy/v5-scan/gauss-r21-science-postreboot-passive-v1.station.json")
START = 3_000_000_000_000


def _definition() -> SupercycleCanaryDefinition:
    first = load_v5_capture_station(BASE_A)
    second = load_v5_capture_station(BASE_B)
    return SupercycleCanaryDefinition(
        "canary_test36",
        UtcNs(START),
        first.radio.radio_id,
        second.radio.radio_id,
        first.specification_digest,
        second.specification_digest,
        5_000_000_000,
        V8_QUALIFICATION_RECEIPT_DIGEST,
    )


def _published(recording_id: RecordingId) -> PublishedRecordingRef:
    digest = Digest.sha256(f"data:{recording_id}".encode())
    metadata = Digest.sha256(f"metadata:{recording_id}".encode())
    return PublishedRecordingRef(
        RecordingObjectRef(
            recording_id,
            ObjectRef(
                digest, 64, "application/octet-stream", "data-v1", f"cas:{digest}"
            ),
            ObjectRef(
                metadata,
                64,
                "application/json",
                "metadata-v1",
                f"cas:{metadata}",
            ),
            digest,
        )
    )


def _snapshot(slot: int) -> CaptureBatchSnapshot:
    unit = build_canary_unit(_definition(), slot_index=slot)
    outcomes = tuple(
        CaptureAttemptOutcome(
            SchemaRef(CaptureAttemptOutcome.SCHEMA_ID),
            unit.batch.batch_id,
            attempt.attempt_id,
            attempt.radio_id,
            attempt.plan_id,
            CaptureAttemptState.SUCCEEDED,
            UtcNs(int(unit.requested_start_utc_ns) + 2_000_000 + index),
            UtcNs(int(unit.requested_start_utc_ns) + index),
            _published(RecordingId(f"rec_canary_{slot:03d}_{index}")),
        )
        for index, attempt in enumerate(unit.batch.expected_attempts)
    )
    return CaptureBatchSnapshot(
        SchemaRef(CaptureBatchSnapshot.SCHEMA_ID), unit.batch, outcomes, 2
    )


class _Capacity:
    def available_bytes(self) -> int:
        return 10**15


class _Capture:
    def capture(self, unit, *, not_before_utc_ns, deadline_utc_ns):
        return _snapshot(unit.slot_index)


class _Analysis:
    def analyze(self, snapshot, *, deadline_utc_ns):
        raise AssertionError("ordinary analysis must not run during capture")


def _coordinator() -> tuple[SupercycleCanaryCoordinator, InMemoryCanaryJournal]:
    journal = InMemoryCanaryJournal()
    coordinator = SupercycleCanaryCoordinator(
        _definition(),
        journal,
        _Capture(),
        _Analysis(),
        _Capacity(),
        capacity_margin_bytes=0,
    )
    return coordinator, journal


def test_exact_supercycle_schedule_capacity_and_fresh_identity() -> None:
    definition = _definition()
    combinations = [
        (canary_cell(slot), canary_geometry(slot)) for slot in range(CANARY_SLOTS)
    ]
    assert len(set(combinations)) == 36
    assert set(combinations) == {
        (cell, geometry)
        for cell in CAMPAIGN_CELLS
        for geometry in (("L", "L"), ("L", "U"), ("U", "U"), ("U", "L"))
    }
    units = [build_canary_unit(definition, slot_index=slot) for slot in range(36)]
    assert len({unit.batch.batch_id for unit in units}) == 36
    assert all(unit.retry_index == 0 for unit in units)
    assert CANARY_CAPTURE_TRANSITION_LIMIT == 73
    assert sum(unit.cell.sample_count * 128 for unit in units) == CANARY_RAW_BYTES
    assert int(definition.end_utc_ns) - START == 36 * 400_000_000_000 // 13


def test_codec_is_canonical_and_cannot_cross_authority() -> None:
    encoded = encode_canary_definition(_definition())
    assert encode_canary_definition(decode_canary_definition(encoded)) == encoded
    with pytest.raises(ValueError, match="fields"):
        decode_campaign_definition(encoded)
    payload = json.loads(encoded)
    payload["slots"] = 936
    with pytest.raises(ValueError, match="canonical or policy-exact"):
        decode_canary_definition(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        )


def test_receipt_requires_exact_72_closure_and_carries_no_main_authority() -> None:
    definition = _definition()
    receipt = SupercycleCanaryReceipt(
        definition.digest,
        V8_QUALIFICATION_RECEIPT_DIGEST,
        UtcNs(START),
        tuple(Digest.sha256(f"unit:{index}".encode()) for index in range(36)),
        tuple(Digest.sha256(f"snapshot:{index}".encode()) for index in range(36)),
        tuple(Digest.sha256(f"analysis:{index}".encode()) for index in range(36)),
        tuple(f"rec_receipt_{index}" for index in range(72)),
        (1,) * 36,
        (1,) * 36,
        tuple(
            CanaryStageBenchmark(
                stage, 8 if stage.endswith("compute") else 4, 1, 0, 4096
            )
            for stage in CANARY_ANALYSIS_STAGES
        ),
        72,
        72,
        72,
        72,
    )
    encoded = encode_canary_receipt(receipt)
    assert encode_canary_receipt(decode_canary_receipt(encoded)) == encoded
    assert json.loads(encoded)["main_campaign_authorized"] is False
    with pytest.raises(ValueError, match="closure"):
        replace(receipt, dashboard_recording_count=71)


def test_capture_phase_is_exact_and_closes_before_analysis() -> None:
    coordinator, journal = _coordinator()
    definition = _definition()
    for slot in range(36):
        unit = build_canary_unit(definition, slot_index=slot)
        result = coordinator.capture_next(
            UtcNs(int(unit.requested_start_utc_ns) - 15_000_000_000)
        )
        assert result.status.value == "captured"
    result = coordinator.capture_next(definition.end_utc_ns)
    assert result.status.value == "capture_phase_closed"
    assert result.state.phase is CanaryPhase.ANALYZING
    assert result.state.captured_count == 36
    assert len({record.unit.batch.batch_id for record in result.state.records}) == 36
    assert journal.load(definition) == result.state


def test_missed_slot_halts_and_never_replays() -> None:
    coordinator, journal = _coordinator()
    result = coordinator.capture_next(UtcNs(START + 5_000_000_001))
    assert result.state.phase is CanaryPhase.HALTED
    revision = result.state.revision
    again = coordinator.capture_next(UtcNs(START + 6_000_000_000))
    assert again.state.revision == revision
    assert len(journal.load(_definition()).records) == 1


def test_restart_after_durable_capture_invocation_halts_without_replay() -> None:
    coordinator, journal = _coordinator()
    definition = _definition()
    unit = build_canary_unit(definition, slot_index=0)
    journal.compare_and_swap(
        definition,
        0,
        CanaryState(
            definition.digest,
            records=(CanaryRecord(unit, CanaryRecordPhase.CAPTURE_INVOKED),),
            revision=1,
        ),
    )
    result = coordinator.capture_next(UtcNs(START))
    assert result.state.phase is CanaryPhase.HALTED
    assert result.state.halt_reason.value == "capture_uncertain"


def test_sqlite_journal_restart_and_definition_binding(tmp_path: Path) -> None:
    definition = _definition()
    path = tmp_path / "canary-supercycles" / "state.sqlite3"
    first = SQLiteSupercycleCanaryJournal(path)
    initial = first.initialize(definition)
    unit = build_canary_unit(definition, slot_index=0)
    replacement = CanaryState(
        definition.digest,
        records=(CanaryRecord(unit, CanaryRecordPhase.CAPTURED, _snapshot(0)),),
        revision=1,
    )
    first.compare_and_swap(definition, initial.revision, replacement)
    restarted = SQLiteSupercycleCanaryJournal(path)
    assert restarted.load(definition) == replacement
    changed = replace(definition, start_utc_ns=UtcNs(START + 1))
    with pytest.raises(RuntimeError, match="integrity"):
        restarted.load(changed)


def test_materialization_preserves_40ms_blocks_and_isolated_root() -> None:
    definition = _definition()
    bases = (load_v5_capture_station(BASE_A), load_v5_capture_station(BASE_B))
    for slot in range(36):
        unit = build_canary_unit(definition, slot_index=slot)
        for side, base in zip(("a", "b"), bases, strict=True):
            station = materialize_canary_station(
                definition,
                base,
                unit,
                side=side,
                canary_state_root=Path("/var/lib/leo-flow/canary-supercycles"),
            )
            assert station.plan.hardware_block_samples == min(
                station.plan.sample_count,
                int(station.plan.sample_rate_hz) * 40 // 1_000,
            )
            assert (
                station.plan.edge_order
                == canary_geometry(slot)[0 if side == "a" else 1]
            )
            assert "canary-supercycles" in station.state.state_root.parts
    with pytest.raises(ValueError, match="isolated"):
        materialize_canary_station(
            definition,
            bases[0],
            build_canary_unit(definition, slot_index=0),
            side="a",
            canary_state_root=Path("/var/lib/leo-flow/continuous"),
        )


class _Preparer:
    def prepare(self, definition, first_success_index, snapshots):
        recordings = tuple(
            item for snapshot in snapshots for item in snapshot.successful_recordings
        )
        return DeferredAnalysisWindowV1(
            definition.digest,
            first_success_index,
            tuple(snapshot.batch_id for snapshot in snapshots),
            tuple(item.recording_id for item in recordings),
            tuple(canonical_digest(item.recording_object) for item in recordings),
            tuple(JobId(f"job_feature_{index}") for index in range(72)),
            tuple(JobId(f"job_waterfall_{index}") for index in range(72)),
            tuple(JobId(f"job_suite_{index}") for index in range(72)),
        )


class _Lane:
    def __init__(self, *, fail_stage: DeferredAnalysisStage | None = None) -> None:
        self.calls = []
        self.fail_stage = fail_stage

    def drain(self, window, stage, *, workers, deadline_utc_ns):
        self.calls.append((stage, workers))
        if stage is self.fail_stage:
            return DeferredAnalysisLaneResultV1(
                stage, DeferredAnalysisLaneState.PARKED, 72, 71, 0, ("job_bad",)
            )
        return DeferredAnalysisLaneResultV1(
            stage, DeferredAnalysisLaneState.COMPLETE, 72, 72, 0
        )


def _analysis_coordinator(lane: _Lane):
    coordinator, journal = _coordinator()
    definition = _definition()
    records = tuple(
        CanaryRecord(
            build_canary_unit(definition, slot_index=slot),
            CanaryRecordPhase.CAPTURED,
            _snapshot(slot),
        )
        for slot in range(36)
    )
    journal.compare_and_swap(
        definition,
        0,
        CanaryState(definition.digest, CanaryPhase.ANALYZING, records, revision=1),
    )
    ticks = iter(range(1, 100))
    staged = SupercycleCanaryStagedAnalysis(
        definition,
        coordinator,
        _Preparer(),
        lane,
        monotonic_ns=lambda: next(ticks) * 10,
        process_time_ns=lambda: next(ticks),
        peak_rss_bytes=lambda: 4096,
    )
    return staged, journal


def test_staged_analysis_uses_exact_8_4_barriers_and_benchmarks() -> None:
    lane = _Lane()
    staged, _ = _analysis_coordinator(lane)
    run = staged.run(deadline_utc_ns=UtcNs(START + 10**12))
    assert lane.calls == [
        (stage, 8 if stage.value.endswith("compute") else 4)
        for stage in DeferredAnalysisStage
    ]
    assert tuple(item.stage for item in run.benchmarks) == CANARY_ANALYSIS_STAGES
    assert all(
        item.wall_time_ns > 0 and item.peak_rss_bytes == 4096 for item in run.benchmarks
    )


def test_staged_analysis_failure_is_terminal() -> None:
    lane = _Lane(fail_stage=DeferredAnalysisStage.STARLINK_SUITE_COMPUTE)
    staged, journal = _analysis_coordinator(lane)
    with pytest.raises(CanaryAnalysisError, match="did not close"):
        staged.run(deadline_utc_ns=UtcNs(START + 10**12))
    assert journal.load(_definition()).phase is CanaryPhase.HALTED


def test_operator_status_is_canary_only_and_arm_fails_closed(tmp_path: Path) -> None:
    definition = _definition()
    root = tmp_path / "canary-supercycles" / "canary_test36"
    definition_path = tmp_path / "definition.json"
    definition_path.write_bytes(encode_canary_definition(definition))
    journal_path = root / "journal.sqlite3"
    SQLiteSupercycleCanaryJournal(journal_path).initialize(definition)
    output = StringIO()
    assert (
        canary_main(
            [
                "status",
                "--definition",
                str(definition_path),
                "--journal",
                str(journal_path),
            ],
            stdout=output,
        )
        == 0
    )
    assert json.loads(output.getvalue())["main_campaign_authorized"] is False

    fake_receipt = tmp_path / "wrong-v8.receipt.json"
    fake_receipt.write_bytes(b"{}")
    errors = StringIO()
    assert (
        canary_main(
            [
                "capture-run",
                "--definition",
                str(definition_path),
                "--journal",
                str(journal_path),
                "--station-a",
                str(BASE_A),
                "--station-b",
                str(BASE_B),
                "--qualification-receipt",
                str(fake_receipt),
                "--canary-state-root",
                str(root),
                "--capacity-margin-bytes",
                "0",
                "--maximum-transitions",
                "73",
                "--arm",
                "--confirm-definition-digest",
                str(definition.digest),
            ],
            stderr=errors,
        )
        == 4
    )
    assert json.loads(errors.getvalue())["event"] == "canary_transition_failed"
    assert SQLiteSupercycleCanaryJournal(journal_path).load(definition).records == ()
