from __future__ import annotations

import json
from dataclasses import replace
from io import StringIO
from pathlib import Path

import pytest

from leo_flow.adapters.campaign_sqlite import SQLiteCampaignJournal
from leo_flow.capture.campaign import (
    CAMPAIGN_CELLS,
    CAMPAIGN_HARDWARE_BLOCK_DURATION_MS,
    CAMPAIGN_RAW_BYTES,
    CAMPAIGN_SUCCESS_TARGET,
    CAMPAIGN_WINDOW_NS,
    MAIN_GEOMETRY_SCHEDULE,
    SLOT_PERIOD_DENOMINATOR,
    SLOT_PERIOD_NUMERATOR_NS,
    CampaignAnalysisReceipt,
    CampaignAnalysisSuccess,
    CampaignCoordinator,
    CampaignDefinition,
    CampaignQualificationReceipt,
    CampaignRunStatus,
    CampaignUnit,
    InMemoryCampaignJournal,
    build_campaign_unit,
    build_qualification_receipt,
    campaign_cell,
    campaign_edge_order,
    materialize_campaign_station,
    required_remaining_capacity_bytes,
)
from leo_flow.capture.campaign_codec import (
    decode_campaign_definition,
    encode_campaign_definition,
    encode_qualification_receipt,
)
from leo_flow.capture.v5_station import load_v5_capture_station
from leo_flow.contracts.capture_batch import (
    CaptureAttemptOutcome,
    CaptureAttemptState,
    CaptureBatchSnapshot,
)
from leo_flow.contracts.core import (
    AnalysisRunId,
    ArtifactRef,
    Digest,
    FeatureSetId,
    JobId,
    RecordingId,
    SchemaRef,
    UtcNs,
)
from leo_flow.contracts.features import FeatureSetRef
from leo_flow.contracts.storage import (
    ObjectRef,
    PublishedRecordingRef,
    RecordingObjectRef,
)
from leo_flow.deployments.v5_campaign_operator import main as campaign_main

BASE_A = Path("deploy/v5-scan/gauss-r20-science-postreboot-passive-v1.station.json")
BASE_B = Path("deploy/v5-scan/gauss-r21-science-postreboot-passive-v1.station.json")
START = 2_000_000_000_000


def _qualification_definition() -> CampaignDefinition:
    first = load_v5_capture_station(BASE_A)
    second = load_v5_capture_station(BASE_B)
    return CampaignDefinition(
        "qual_20260816",
        UtcNs(START - 1_000_000_000),
        first.radio.radio_id,
        second.radio.radio_id,
        first.specification_digest,
        second.specification_digest,
        maximum_start_lateness_ns=5_000_000_000,
        qualification=True,
    )


def _receipt() -> CampaignQualificationReceipt:
    return CampaignQualificationReceipt(
        Digest.sha256(b"qualification-definition"),
        UtcNs(START - 1),
        tuple(Digest.sha256(f"unit-{index}".encode()) for index in range(9)),
        tuple(Digest.sha256(f"snapshot-{index}".encode()) for index in range(9)),
        tuple(Digest.sha256(f"analysis-{index}".encode()) for index in range(9)),
        (1,) * 9,
    )


def _main_definition() -> tuple[CampaignDefinition, CampaignQualificationReceipt]:
    first = load_v5_capture_station(BASE_A)
    second = load_v5_capture_station(BASE_B)
    receipt = _receipt()
    return (
        CampaignDefinition(
            "main_20260816",
            UtcNs(START),
            first.radio.radio_id,
            second.radio.radio_id,
            first.specification_digest,
            second.specification_digest,
            maximum_start_lateness_ns=5_000_000_000,
            qualification_receipt_digest=receipt.digest,
        ),
        receipt,
    )


def _published(recording_id: RecordingId) -> PublishedRecordingRef:
    data = Digest.sha256(f"{recording_id}:data".encode())
    metadata = Digest.sha256(f"{recording_id}:metadata".encode())
    return PublishedRecordingRef(
        RecordingObjectRef(
            recording_id,
            ObjectRef(data, 64, "application/octet-stream", "data-v1", f"cas:{data}"),
            ObjectRef(
                metadata,
                128,
                "application/json",
                "metadata-v1",
                f"cas:{metadata}",
            ),
            Digest.sha256(f"{recording_id}:manifest".encode()),
        )
    )


def _snapshot(unit: CampaignUnit, *, skew_ns: int = 10) -> CaptureBatchSnapshot:
    outcomes = []
    for index, attempt in enumerate(unit.batch.expected_attempts):
        observed = int(unit.requested_start_utc_ns) + index * skew_ns
        outcomes.append(
            CaptureAttemptOutcome(
                SchemaRef(CaptureAttemptOutcome.SCHEMA_ID),
                unit.batch.batch_id,
                attempt.attempt_id,
                attempt.radio_id,
                attempt.plan_id,
                CaptureAttemptState.SUCCEEDED,
                UtcNs(observed + 1),
                UtcNs(observed),
                _published(
                    RecordingId(
                        f"rec_{unit.success_index:03d}_{unit.slot_index:03d}_{index}"
                    )
                ),
            )
        )
    return CaptureBatchSnapshot(
        SchemaRef(CaptureBatchSnapshot.SCHEMA_ID), unit.batch, tuple(outcomes), 2
    )


class _Capture:
    def __init__(self, *, skew_ns: int = 10, fail_once: bool = False) -> None:
        self.skew_ns = skew_ns
        self.fail_once = fail_once
        self.calls: list[tuple[CampaignUnit, UtcNs, UtcNs]] = []

    def capture(
        self,
        unit: CampaignUnit,
        *,
        not_before_utc_ns: UtcNs,
        deadline_utc_ns: UtcNs,
    ) -> CaptureBatchSnapshot:
        self.calls.append((unit, not_before_utc_ns, deadline_utc_ns))
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("lost response")
        return _snapshot(unit, skew_ns=self.skew_ns)


class _Analysis:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.fail_once = fail_once
        self.calls: list[tuple[CaptureBatchSnapshot, UtcNs]] = []

    def analyze(
        self, snapshot: CaptureBatchSnapshot, *, deadline_utc_ns: UtcNs
    ) -> CampaignAnalysisReceipt:
        self.calls.append((snapshot, deadline_utc_ns))
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("analysis interrupted")
        recordings = tuple(
            sorted(
                (
                    item.recording_ref.recording_id
                    for item in snapshot.outcomes
                    if item.recording_ref
                ),
                key=str,
            )
        )
        assert len(recordings) == 2
        successes = []
        for index, recording_id in enumerate(recordings):
            digest = Digest.sha256(f"feature-{recording_id}".encode())
            feature_ref = FeatureSetRef(
                FeatureSetId(f"fset_{index}_{recording_id}"),
                AnalysisRunId(f"arun_{index}_{recording_id}"),
                ObjectRef(
                    digest,
                    64,
                    "application/json",
                    "feature-set-v1",
                    f"cas:{digest}",
                ),
            )
            successes.append(
                CampaignAnalysisSuccess(
                    recording_id,
                    JobId(f"job_{index}_{recording_id}"),
                    ArtifactRef(
                        str(feature_ref.feature_set_id),
                        digest,
                        SchemaRef("org.leo-flow.feature-set-bundle"),
                    ),
                    f"fpwork_{index}_{recording_id}",
                    feature_ref,
                    UtcNs(int(deadline_utc_ns) - 2),
                )
            )
        return CampaignAnalysisReceipt(
            snapshot.batch_id,
            (successes[0], successes[1]),
            UtcNs(int(deadline_utc_ns) - 1),
        )


class _Capacity:
    def __init__(self, available: int = 100_000_000_000) -> None:
        self.available = available

    def available_bytes(self) -> int:
        return self.available


def test_matrix_schedule_and_storage_accounting_are_exact() -> None:
    definition, _ = _main_definition()
    units = tuple(
        build_campaign_unit(
            definition,
            success_index=index,
            slot_index=index,
            retry_index=0,
            requested_start_utc_ns=UtcNs(
                START + index * SLOT_PERIOD_NUMERATOR_NS // SLOT_PERIOD_DENOMINATOR
            ),
        )
        for index in range(CAMPAIGN_SUCCESS_TARGET)
    )

    assert len({unit.batch.batch_id for unit in units}) == 936
    assert (
        len({unit.plan_a_id for unit in units} | {unit.plan_b_id for unit in units})
        == 1872
    )
    assert units[-1].requested_start_utc_ns < UtcNs(START + CAMPAIGN_WINDOW_NS)
    assert definition.capture_run_transition_limit == 1_873
    assert definition.analysis_drain_transition_limit == 937
    assert definition.document()["slot_period_numerator_ns"] == 400_000_000_000
    assert definition.document()["slot_period_denominator"] == 13
    assert (
        tuple(
            tuple(
                campaign_cell(index) for index in range(CAMPAIGN_SUCCESS_TARGET)
            ).count(cell)
            for cell in CAMPAIGN_CELLS
        )
        == (104,) * 9
    )
    geometry_counts = {
        (campaign_cell(index), MAIN_GEOMETRY_SCHEDULE[index % 4]): 0
        for index in range(36)
    }
    for index in range(CAMPAIGN_SUCCESS_TARGET):
        geometry_counts[
            (
                campaign_cell(index),
                (
                    campaign_edge_order(definition, success_index=index, side="a"),
                    campaign_edge_order(definition, success_index=index, side="b"),
                ),
            )
        ] += 1
    assert set(geometry_counts.values()) == {26}
    assert {cell.sample_count for cell in CAMPAIGN_CELLS} == {
        50_000,
        100_000,
        200_000,
        400_000,
        800_000,
    }
    journal = InMemoryCampaignJournal()
    state = journal.initialize(definition)
    assert (
        required_remaining_capacity_bytes(definition, state, margin_bytes=0)
        == CAMPAIGN_RAW_BYTES * 2
    )
    assert (
        required_remaining_capacity_bytes(
            definition, state, margin_bytes=10_737_418_240
        )
        == 75_966_218_240
    )


def test_main_waits_for_future_slot_and_completes_capture_then_exact_analysis() -> None:
    definition, receipt = _main_definition()
    capture = _Capture()
    analysis = _Analysis()
    coordinator = CampaignCoordinator(
        definition,
        InMemoryCampaignJournal(),
        capture,
        analysis,
        _Capacity(),
        0,
        receipt,
    )

    early = coordinator.run_next(UtcNs(START - 15_000_000_001))
    complete = coordinator.run_next(UtcNs(START - 15_000_000_000))
    next_slot = coordinator.run_next(UtcNs(START))

    assert early.status is CampaignRunStatus.NOT_DUE
    assert complete.status is CampaignRunStatus.UNIT_COMPLETE
    assert next_slot.status is CampaignRunStatus.NOT_DUE
    assert len(capture.calls) == len(analysis.calls) == 1
    assert capture.calls[0][1] == UtcNs(START)
    assert capture.calls[0][2] == analysis.calls[0][1]
    assert complete.state.successful_counts == (1, 0, 0, 0, 0, 0, 0, 0, 0)
    assert complete.state.accepted_balanced_rounds == 0


def test_missed_slot_and_uncertain_capture_never_burst_or_change_identity() -> None:
    definition, receipt = _main_definition()
    missed_capture = _Capture()
    missed = CampaignCoordinator(
        definition,
        InMemoryCampaignJournal(),
        missed_capture,
        _Analysis(),
        _Capacity(),
        0,
        receipt,
    ).run_next(UtcNs(START + definition.maximum_start_lateness_ns + 1))
    assert missed.status is CampaignRunStatus.MISSED_SLOT
    assert missed_capture.calls == []

    capture = _Capture(fail_once=True)
    journal = InMemoryCampaignJournal()
    coordinator = CampaignCoordinator(
        definition, journal, capture, _Analysis(), _Capacity(), 0, receipt
    )
    uncertain = coordinator.run_next(UtcNs(START))
    replayed = coordinator.run_next(UtcNs(START))
    assert uncertain.status is CampaignRunStatus.CAPTURE_UNCERTAIN
    assert replayed.status is CampaignRunStatus.UNIT_COMPLETE
    assert capture.calls[0][0].digest == capture.calls[1][0].digest


def test_qualification_first_unit_honors_definition_start_and_preflight_lead() -> None:
    definition = _qualification_definition()
    capture = _Capture()
    coordinator = CampaignCoordinator(
        definition, InMemoryCampaignJournal(), capture, _Analysis(), _Capacity(), 0
    )
    scheduled = int(definition.start_utc_ns)

    early = coordinator.run_next(UtcNs(scheduled - definition.preflight_lead_ns - 1))
    released = coordinator.run_next(UtcNs(scheduled - definition.preflight_lead_ns))

    assert early.status is CampaignRunStatus.NOT_DUE
    assert early.unit is not None
    assert early.unit.requested_start_utc_ns == definition.start_utc_ns
    assert released.unit is not None
    assert capture.calls == [
        (
            released.unit,
            definition.start_utc_ns,
            UtcNs(scheduled + 400_000_000_000 // 3),
        )
    ]
    assert released.status is CampaignRunStatus.UNIT_COMPLETE


def test_qualification_resume_keeps_first_schedule_and_late_start_is_terminal() -> None:
    definition = _qualification_definition()
    capture = _Capture()
    journal = InMemoryCampaignJournal()
    coordinator = CampaignCoordinator(
        definition, journal, capture, _Analysis(), _Capacity(), 0
    )
    scheduled = int(definition.start_utc_ns)

    first = coordinator.run_next(UtcNs(scheduled - definition.preflight_lead_ns - 1))
    resumed = coordinator.run_next(UtcNs(scheduled - definition.preflight_lead_ns - 2))
    missed = coordinator.run_next(
        UtcNs(scheduled + definition.maximum_start_lateness_ns + 1)
    )

    assert first.status is resumed.status is CampaignRunStatus.NOT_DUE
    assert first.unit == resumed.unit == missed.unit
    assert missed.status is CampaignRunStatus.MISSED_SLOT
    assert journal.load(definition).records == first.state.records
    assert capture.calls == []


def test_qualification_subsequent_unit_uses_immutable_no_catch_up_grid() -> None:
    definition = _qualification_definition()
    capture = _Capture()
    coordinator = CampaignCoordinator(
        definition, InMemoryCampaignJournal(), capture, _Analysis(), _Capacity(), 0
    )
    scheduled = int(definition.start_utc_ns)
    first = coordinator.run_next(UtcNs(scheduled - definition.preflight_lead_ns))
    second_invocation = UtcNs(START + 123_456_789)

    second = coordinator.run_next(second_invocation)

    assert first.status is CampaignRunStatus.UNIT_COMPLETE
    assert second.status is CampaignRunStatus.NOT_DUE
    assert second.unit is not None
    assert second.unit.requested_start_utc_ns == UtcNs(
        int(definition.start_utc_ns)
        + definition.slot_period_numerator_ns // definition.slot_period_denominator
    )
    assert len(capture.calls) == 1


def test_excess_skew_is_persisted_terminal_and_analysis_is_not_called() -> None:
    definition, receipt = _main_definition()
    analysis = _Analysis()
    journal = InMemoryCampaignJournal()
    result = CampaignCoordinator(
        definition,
        journal,
        _Capture(skew_ns=100_000_001),
        analysis,
        _Capacity(),
        0,
        receipt,
    ).run_next(UtcNs(START))

    assert result.status is CampaignRunStatus.CAPTURE_FAILED
    assert result.state.records[-1].snapshot is not None
    assert result.state.records[-1].phase.value == "terminal_failed"
    assert analysis.calls == []


def test_main_terminal_failure_rearms_same_cell_with_fresh_next_slot_identity() -> None:
    definition, receipt = _main_definition()
    capture = _Capture(skew_ns=100_000_001)
    coordinator = CampaignCoordinator(
        definition,
        InMemoryCampaignJournal(),
        capture,
        _Analysis(),
        _Capacity(),
        0,
        receipt,
    )
    failed = coordinator.run_next(UtcNs(START))
    capture.skew_ns = 10
    retry_target = START + SLOT_PERIOD_NUMERATOR_NS // SLOT_PERIOD_DENOMINATOR
    succeeded = coordinator.run_next(UtcNs(retry_target - 15_000_000_000))

    assert failed.status is CampaignRunStatus.CAPTURE_FAILED
    assert succeeded.status is CampaignRunStatus.UNIT_COMPLETE
    assert capture.calls[0][0].cell == capture.calls[1][0].cell
    assert capture.calls[0][0].batch.batch_id != capture.calls[1][0].batch.batch_id
    assert capture.calls[1][0].retry_index == 1


def test_analysis_error_resumes_from_snapshot_without_recapture() -> None:
    definition, receipt = _main_definition()
    capture = _Capture()
    analysis = _Analysis(fail_once=True)
    coordinator = CampaignCoordinator(
        definition,
        InMemoryCampaignJournal(),
        capture,
        analysis,
        _Capacity(),
        0,
        receipt,
    )
    failed = coordinator.run_next(UtcNs(START))
    resumed = coordinator.run_next(UtcNs(START + 1))

    assert failed.status is CampaignRunStatus.ANALYSIS_FAILED
    assert resumed.status is CampaignRunStatus.UNIT_COMPLETE
    assert len(capture.calls) == 1
    assert len(analysis.calls) == 2


def test_late_analysis_receipt_is_persisted_but_never_completes_unit() -> None:
    definition, receipt = _main_definition()

    class LateAnalysis(_Analysis):
        def analyze(
            self, snapshot: CaptureBatchSnapshot, *, deadline_utc_ns: UtcNs
        ) -> CampaignAnalysisReceipt:
            result = super().analyze(snapshot, deadline_utc_ns=deadline_utc_ns)
            return replace(result, completed_utc_ns=UtcNs(int(deadline_utc_ns) + 1))

    result = CampaignCoordinator(
        definition,
        InMemoryCampaignJournal(),
        _Capture(),
        LateAnalysis(),
        _Capacity(),
        0,
        receipt,
    ).run_next(UtcNs(START))

    assert result.status is CampaignRunStatus.ANALYSIS_FAILED
    assert result.state.records[-1].analysis_receipt is not None
    assert result.state.completed_successes == 0


def test_analysis_receipt_binds_job_artifact_to_projected_feature_identity() -> None:
    unit_definition, _ = _main_definition()
    unit = build_campaign_unit(
        unit_definition,
        success_index=0,
        slot_index=0,
        retry_index=0,
        requested_start_utc_ns=UtcNs(START),
    )
    receipt = _Analysis().analyze(
        _snapshot(unit), deadline_utc_ns=UtcNs(START + 1_000_000_000)
    )
    first = receipt.successes[0]

    with pytest.raises(ValueError, match="identities differ"):
        replace(first, result_ref=replace(first.result_ref, artifact_id="wrong"))


def test_qualification_failure_halts_without_allocating_retry_identity() -> None:
    definition = _qualification_definition()
    journal = InMemoryCampaignJournal()

    class FailedCapture(_Capture):
        def capture(self, unit: CampaignUnit, **kwargs: UtcNs) -> CaptureBatchSnapshot:
            snapshot = _snapshot(unit)
            peer = snapshot.outcomes[1]
            failed = replace(
                peer,
                state=CaptureAttemptState.FAILED,
                observed_start_utc_ns=None,
                recording_ref=None,
                failure_reason="capture_runner_failed",
            )
            return replace(snapshot, outcomes=(snapshot.outcomes[0], failed))

    coordinator = CampaignCoordinator(
        definition, journal, FailedCapture(), _Analysis(), _Capacity(), 0
    )
    first = coordinator.run_next(UtcNs(START))
    second = coordinator.run_next(UtcNs(START + 1))
    assert first.status is second.status is CampaignRunStatus.CAPTURE_FAILED
    assert len(second.state.records) == 1


def test_separate_qualification_round_emits_exact_receipt() -> None:
    definition = _qualification_definition()
    journal = InMemoryCampaignJournal()
    coordinator = CampaignCoordinator(
        definition, journal, _Capture(), _Analysis(), _Capacity(), 0
    )

    for index in range(9):
        result = coordinator.run_next(
            UtcNs(
                int(definition.start_utc_ns)
                + index
                * definition.slot_period_numerator_ns
                // definition.slot_period_denominator
                - definition.preflight_lead_ns
            )
        )
        assert result.status is CampaignRunStatus.UNIT_COMPLETE
    state = journal.load(definition)
    receipt = build_qualification_receipt(
        definition,
        state,
        issued_utc_ns=UtcNs(
            int(definition.start_utc_ns)
            + 9
            * definition.slot_period_numerator_ns
            // definition.slot_period_denominator
        ),
    )

    assert receipt.successful_counts == (1,) * 9
    assert len(set(receipt.unit_digests)) == 9
    assert len(set(receipt.snapshot_digests)) == 9
    assert len(set(receipt.analysis_receipt_digests)) == 9


def test_capacity_failure_blocks_before_capture() -> None:
    definition, receipt = _main_definition()
    capture = _Capture()
    result = CampaignCoordinator(
        definition,
        InMemoryCampaignJournal(),
        capture,
        _Analysis(),
        _Capacity(0),
        0,
        receipt,
    ).run_next(UtcNs(START))

    assert result.status is CampaignRunStatus.CAPACITY_BLOCKED
    assert capture.calls == []


def test_shortest_station_materialization_uses_one_refill_and_clipped_opt_in(
    tmp_path: Path,
) -> None:
    definition, _ = _main_definition()
    base = load_v5_capture_station(BASE_A)
    unit = build_campaign_unit(
        definition,
        success_index=0,
        slot_index=0,
        retry_index=0,
        requested_start_utc_ns=UtcNs(START),
    )
    station = materialize_campaign_station(
        definition, base, unit, side="a", campaign_state_root=tmp_path
    )

    assert station.plan.sample_count == station.plan.hardware_block_samples == 50_000
    assert station.plan.allow_clipped_pilot is True
    assert dict(station.capture_plan().experiment_tags)["pilot_band_clipped"] is True
    assert station.state.cas_root == base.state.cas_root
    assert station.state.mode_lock_path == base.state.mode_lock_path
    assert station.state.state_root == (
        tmp_path / "units" / "u000_s000_r00" / "radio-a"
    )

    retry = build_campaign_unit(
        definition,
        success_index=0,
        slot_index=1,
        retry_index=1,
        requested_start_utc_ns=UtcNs(START),
    )
    retry_station = materialize_campaign_station(
        definition, base, retry, side="a", campaign_state_root=tmp_path
    )
    assert retry_station.state.state_root == (
        tmp_path / "units" / "u000_s001_r01" / "radio-a"
    )
    assert retry_station.state.state_root != station.state.state_root


def test_campaign_hardware_blocks_cap_post_release_priming_at_40ms(
    tmp_path: Path,
) -> None:
    definition, _ = _main_definition()
    base = load_v5_capture_station(BASE_A)

    for index, cell in enumerate(CAMPAIGN_CELLS):
        unit = build_campaign_unit(
            definition,
            success_index=index,
            slot_index=index,
            retry_index=0,
            requested_start_utc_ns=UtcNs(START + index),
        )
        station = materialize_campaign_station(
            definition, base, unit, side="a", campaign_state_root=tmp_path
        )
        expected_block_samples = (
            cell.sample_rate_hz * CAMPAIGN_HARDWARE_BLOCK_DURATION_MS // 1_000
        )

        assert station.plan.sample_count == cell.sample_count
        assert station.plan.hardware_block_samples == expected_block_samples
        assert (
            station.plan.sample_count // station.plan.hardware_block_samples
            == cell.duration_ms // CAMPAIGN_HARDWARE_BLOCK_DURATION_MS
        )


def test_main_materialization_rotates_same_and_opposite_edge_geometry(
    tmp_path: Path,
) -> None:
    definition, _ = _main_definition()
    bases = (load_v5_capture_station(BASE_A), load_v5_capture_station(BASE_B))

    observed = []
    for index in range(4):
        unit = build_campaign_unit(
            definition,
            success_index=index,
            slot_index=index,
            retry_index=0,
            requested_start_utc_ns=UtcNs(START + index),
        )
        stations = tuple(
            materialize_campaign_station(
                definition,
                base,
                unit,
                side=side,
                campaign_state_root=tmp_path,
            )
            for base, side in zip(bases, ("a", "b"), strict=True)
        )
        observed.append(tuple(station.plan.edge_order for station in stations))

    assert tuple(observed) == MAIN_GEOMETRY_SCHEDULE


def test_sqlite_journal_reopens_and_rejects_definition_mutation(tmp_path: Path) -> None:
    definition, _ = _main_definition()
    path = tmp_path / "campaign.sqlite3"
    journal = SQLiteCampaignJournal(path)
    initial = journal.initialize(definition)
    assert SQLiteCampaignJournal(path).load(definition) == initial
    with pytest.raises(RuntimeError, match="integrity|definition"):
        journal.load(replace(definition, maximum_start_lateness_ns=1))


def test_sqlite_journal_persists_exact_analysis_and_projection_receipt(
    tmp_path: Path,
) -> None:
    definition, receipt = _main_definition()
    path = tmp_path / "campaign.sqlite3"
    journal = SQLiteCampaignJournal(path)
    result = CampaignCoordinator(
        definition,
        journal,
        _Capture(),
        _Analysis(),
        _Capacity(),
        0,
        receipt,
    ).run_next(UtcNs(START - 15_000_000_000))

    reopened = SQLiteCampaignJournal(path).load(definition)

    assert result.status is CampaignRunStatus.UNIT_COMPLETE
    assert reopened.records[-1].analysis_receipt is not None
    assert reopened.records[-1].analysis_receipt == (
        result.state.records[-1].analysis_receipt
    )
    assert len(reopened.records[-1].analysis_receipt.successes) == 2


def test_campaign_codec_is_canonical_and_rejects_timing_mutation() -> None:
    definition, _ = _main_definition()
    encoded = encode_campaign_definition(definition)
    assert decode_campaign_definition(encoded) == definition

    mutated = json.loads(encoded)
    mutated["slot_period_denominator"] = 4
    with pytest.raises(ValueError, match="timing"):
        decode_campaign_definition(
            json.dumps(mutated, sort_keys=True, separators=(",", ":")).encode()
        )


def test_campaign_codec_preserves_private_analysis_phase_policy() -> None:
    definition, _ = _main_definition()
    deferred = replace(definition, analysis_after_each_capture=False)

    encoded = encode_campaign_definition(deferred)

    assert json.loads(encoded)["analysis_after_each_capture"] is False
    assert decode_campaign_definition(encoded) == deferred

    mutated = json.loads(encoded)
    mutated["analysis_after_each_capture"] = "false"
    with pytest.raises(ValueError, match="execution policy"):
        decode_campaign_definition(
            json.dumps(mutated, sort_keys=True, separators=(",", ":")).encode()
        )


def test_campaign_codec_retires_legacy_now_based_qualification_schedule() -> None:
    encoded = encode_campaign_definition(_qualification_definition())
    document = json.loads(encoded)
    assert document["schema"] == "org.leo-flow.gauss-v5-campaign/v2"
    assert document["unit_schedule"] == ("fixed_nine_cell_no_catch_up_grid")

    document["schema"] = "org.leo-flow.gauss-v5-campaign/v1"
    document.pop("unit_schedule")
    with pytest.raises(ValueError, match="fields|schema"):
        decode_campaign_definition(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        )


def test_one_shot_coordinator_rejects_deferred_analysis_definition() -> None:
    definition, receipt = _main_definition()

    with pytest.raises(ValueError, match="per-capture analysis"):
        CampaignCoordinator(
            replace(definition, analysis_after_each_capture=False),
            InMemoryCampaignJournal(),
            _Capture(),
            _Analysis(),
            _Capacity(),
            0,
            receipt,
        )


def test_one_shot_operator_rejects_deferred_definition_before_journal(
    tmp_path: Path,
) -> None:
    definition, receipt = _main_definition()
    definition = replace(definition, analysis_after_each_capture=False)
    definition_path = tmp_path / "deferred-main.json"
    receipt_path = tmp_path / "qualification.receipt.json"
    journal_path = tmp_path / "campaign.sqlite3"
    definition_path.write_bytes(encode_campaign_definition(definition))
    receipt_path.write_bytes(encode_qualification_receipt(receipt))

    stderr = StringIO()
    code = campaign_main(
        [
            "run-next",
            "--definition",
            str(definition_path),
            "--qualification-receipt",
            str(receipt_path),
            "--station-a",
            str(BASE_A),
            "--station-b",
            str(BASE_B),
            "--journal",
            str(journal_path),
            "--campaign-state-root",
            str(tmp_path / "state"),
            "--campaign-lock",
            str(tmp_path / "campaign.lock"),
            "--capacity-margin-bytes",
            "0",
            "--arm",
            "--confirm-definition-digest",
            str(definition.digest),
        ],
        stdout=StringIO(),
        stderr=stderr,
        capture_builder=lambda *_args: _Capture(),
        analysis_builder=lambda _definition: _Analysis(),
        capacity_builder=lambda _root: _Capacity(),
    )

    assert code == 3
    assert json.loads(stderr.getvalue()) == {"event": "campaign_arm_rejected"}
    assert not journal_path.exists()


def test_offline_validate_materializes_every_plan_without_creating_state(
    tmp_path: Path,
) -> None:
    definition, receipt = _main_definition()
    definition_path = tmp_path / "campaign.json"
    receipt_path = tmp_path / "receipt.json"
    definition_path.write_bytes(encode_campaign_definition(definition))
    receipt_path.write_bytes(encode_qualification_receipt(receipt))
    state_root = tmp_path / "must-not-be-created"
    stdout = StringIO()
    stderr = StringIO()

    code = campaign_main(
        [
            "validate",
            "--definition",
            str(definition_path),
            "--qualification-receipt",
            str(receipt_path),
            "--station-a",
            str(BASE_A),
            "--station-b",
            str(BASE_B),
            "--campaign-state-root",
            str(state_root),
        ],
        stdout=stdout,
        stderr=stderr,
    )

    assert code == 0
    assert json.loads(stdout.getvalue())["target_successes"] == 936
    assert stderr.getvalue() == ""
    assert not state_root.exists()


def test_cli_plans_fresh_qualification_exclusively(tmp_path: Path) -> None:
    output = tmp_path / "qualification.json"
    stdout = StringIO()
    args = [
        "plan-qualification",
        "--campaign-id",
        "qual_cli_20260816",
        "--start-utc-ns",
        str(START),
        "--maximum-start-lateness-ns",
        "5000000000",
        "--station-a",
        str(BASE_A),
        "--station-b",
        str(BASE_B),
        "--output",
        str(output),
    ]

    code = campaign_main(args, stdout=stdout, stderr=StringIO())
    replay_stderr = StringIO()
    replay = campaign_main(args, stdout=StringIO(), stderr=replay_stderr)

    planned = decode_campaign_definition(output.read_bytes())
    assert code == 0
    assert planned.qualification is True
    assert json.loads(stdout.getvalue())["definition_digest"] == str(planned.digest)
    assert replay == 2
    assert json.loads(replay_stderr.getvalue())["event"] == "campaign_plan_error"


def test_cli_refuses_to_plan_from_superseded_single_tx_station_policy(
    tmp_path: Path,
) -> None:
    paths: list[Path] = []
    for name, source in (("a", BASE_A), ("b", BASE_B)):
        document = json.loads(source.read_text(encoding="utf-8"))
        del document["radio"]["require_both_tx_muted"]
        path = tmp_path / f"legacy-{name}.station.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        paths.append(path)
    output = tmp_path / "must-not-exist.json"
    stderr = StringIO()

    code = campaign_main(
        [
            "plan-qualification",
            "--campaign-id",
            "legacy_policy_must_not_plan",
            "--start-utc-ns",
            str(START),
            "--maximum-start-lateness-ns",
            "5000000000",
            "--station-a",
            str(paths[0]),
            "--station-b",
            str(paths[1]),
            "--output",
            str(output),
        ],
        stdout=StringIO(),
        stderr=stderr,
    )

    assert code == 2
    assert json.loads(stderr.getvalue()) == {"event": "campaign_plan_error"}
    assert not output.exists()


def test_cli_plans_truthful_deferred_analysis_main(tmp_path: Path) -> None:
    qualification = _qualification_definition()
    receipt = replace(_receipt(), qualification_definition_digest=qualification.digest)
    qualification_path = tmp_path / "qualification.json"
    receipt_path = tmp_path / "qualification.receipt.json"
    output = tmp_path / "main.json"
    qualification_path.write_bytes(encode_campaign_definition(qualification))
    receipt_path.write_bytes(encode_qualification_receipt(receipt))

    code = campaign_main(
        [
            "plan-main",
            "--campaign-id",
            "main_deferred_20260816",
            "--start-utc-ns",
            str(START),
            "--maximum-start-lateness-ns",
            "100000000",
            "--station-a",
            str(BASE_A),
            "--station-b",
            str(BASE_B),
            "--qualification-definition",
            str(qualification_path),
            "--qualification-receipt",
            str(receipt_path),
            "--deferred-analysis",
            "--output",
            str(output),
        ],
        stdout=StringIO(),
        stderr=StringIO(),
    )

    planned = decode_campaign_definition(output.read_bytes())
    assert code == 0
    assert planned.analysis_after_each_capture is False


def test_run_next_holds_campaign_lock_and_requires_all_live_ports(
    tmp_path: Path,
) -> None:
    definition, receipt = _main_definition()
    definition_path = tmp_path / "campaign.json"
    receipt_path = tmp_path / "receipt.json"
    definition_path.write_bytes(encode_campaign_definition(definition))
    receipt_path.write_bytes(encode_qualification_receipt(receipt))
    journal_path = tmp_path / "campaign.sqlite3"
    state_root = tmp_path / "state"
    lock_path = tmp_path / "campaign.lock"
    capture = _Capture()
    analysis = _Analysis()
    lock_state = {"held": False, "released": False}

    class Lock:
        def acquire(self) -> None:
            lock_state["held"] = True

        def release(self) -> None:
            lock_state["held"] = False
            lock_state["released"] = True

    def capture_builder(*args: object) -> _Capture:
        assert lock_state["held"]
        return capture

    def analysis_builder(*args: object) -> _Analysis:
        assert lock_state["held"]
        return analysis

    stdout = StringIO()
    stderr = StringIO()
    common = [
        "--definition",
        str(definition_path),
        "--qualification-receipt",
        str(receipt_path),
        "--station-a",
        str(BASE_A),
        "--station-b",
        str(BASE_B),
        "--campaign-state-root",
        str(state_root),
    ]

    rejected = campaign_main(
        [
            "run-next",
            *common,
            "--journal",
            str(journal_path),
            "--campaign-lock",
            str(lock_path),
            "--capacity-margin-bytes",
            "0",
            "--confirm-definition-digest",
            str(definition.digest),
        ],
        stdout=stdout,
        stderr=stderr,
    )
    assert rejected == 3
    assert not journal_path.exists()

    stdout = StringIO()
    stderr = StringIO()
    code = campaign_main(
        [
            "run-next",
            *common,
            "--journal",
            str(journal_path),
            "--campaign-lock",
            str(lock_path),
            "--now-utc-ns",
            str(START - 15_000_000_000),
            "--capacity-margin-bytes",
            "0",
            "--arm",
            "--confirm-definition-digest",
            str(definition.digest),
        ],
        stdout=stdout,
        stderr=stderr,
        capture_builder=capture_builder,
        analysis_builder=analysis_builder,
        capacity_builder=lambda root: _Capacity(),
        lock_factory=lambda path: Lock(),
    )

    assert code == 0
    emitted = stdout.getvalue()
    assert emitted.count('"successful_counts":') == 1
    payload = json.loads(emitted)
    assert payload["status"] == "unit_complete"
    assert payload["successful_counts"] == [1, 0, 0, 0, 0, 0, 0, 0, 0]
    assert stderr.getvalue() == ""
    assert lock_state == {"held": False, "released": True}
    assert len(capture.calls) == len(analysis.calls) == 1


def test_persistent_run_completes_finite_qualification_under_one_lock(
    tmp_path: Path,
) -> None:
    definition = _qualification_definition()
    definition_path = tmp_path / "qualification.json"
    definition_path.write_bytes(encode_campaign_definition(definition))
    lock_state = {"acquired": 0, "released": 0}
    capture = _Capture()
    analysis = _Analysis()
    clock = START

    class Lock:
        def acquire(self) -> None:
            lock_state["acquired"] += 1

        def release(self) -> None:
            lock_state["released"] += 1

    stdout = StringIO()

    def now() -> int:
        return clock

    def delay(seconds: float) -> None:
        nonlocal clock
        clock += round(seconds * 1_000_000_000)

    code = campaign_main(
        [
            "run",
            "--definition",
            str(definition_path),
            "--station-a",
            str(BASE_A),
            "--station-b",
            str(BASE_B),
            "--journal",
            str(tmp_path / "qualification.sqlite3"),
            "--campaign-state-root",
            str(tmp_path / "state"),
            "--campaign-lock",
            str(tmp_path / "campaign.lock"),
            "--capacity-margin-bytes",
            "0",
            "--arm",
            "--confirm-definition-digest",
            str(definition.digest),
        ],
        stdout=stdout,
        stderr=StringIO(),
        capture_builder=lambda *args: capture,
        analysis_builder=lambda *args: analysis,
        capacity_builder=lambda root: _Capacity(),
        lock_factory=lambda path: Lock(),
        now_utc_ns=now,
        delay=delay,
    )

    events = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert code == 0
    assert [item["status"] for item in events] == ["unit_complete"] + [
        status for _index in range(8) for status in ("not_due", "unit_complete")
    ] + ["campaign_complete"]
    assert lock_state == {"acquired": 1, "released": 1}
    assert len(capture.calls) == len(analysis.calls) == 9


def test_operator_waits_until_qualification_preflight_then_releases_exact_start(
    tmp_path: Path,
) -> None:
    definition = replace(
        _qualification_definition(), start_utc_ns=UtcNs(START + 30_000_000_000)
    )
    definition_path = tmp_path / "qualification.json"
    definition_path.write_bytes(encode_campaign_definition(definition))
    capture = _Capture()
    clock = int(definition.start_utc_ns) - definition.preflight_lead_ns - 1_000_000_000
    delays: list[float] = []

    class Lock:
        def acquire(self) -> None:
            pass

        def release(self) -> None:
            pass

    def now() -> int:
        return clock

    def delay(seconds: float) -> None:
        nonlocal clock
        delays.append(seconds)
        clock += round(seconds * 1_000_000_000)

    code = campaign_main(
        [
            "run",
            "--definition",
            str(definition_path),
            "--station-a",
            str(BASE_A),
            "--station-b",
            str(BASE_B),
            "--journal",
            str(tmp_path / "qualification.sqlite3"),
            "--campaign-state-root",
            str(tmp_path / "state"),
            "--campaign-lock",
            str(tmp_path / "campaign.lock"),
            "--capacity-margin-bytes",
            "0",
            "--arm",
            "--confirm-definition-digest",
            str(definition.digest),
        ],
        stdout=StringIO(),
        stderr=StringIO(),
        capture_builder=lambda *_args: capture,
        analysis_builder=lambda *_args: _Analysis(),
        capacity_builder=lambda _root: _Capacity(),
        lock_factory=lambda _path: Lock(),
        now_utc_ns=now,
        delay=delay,
    )

    assert code == 0
    assert delays[0] == 1.0
    assert len(delays) == 9
    assert capture.calls[0][1] == definition.start_utc_ns
    assert all(int(call[1]) >= int(definition.start_utc_ns) for call in capture.calls)


def test_operator_terminalizes_late_qualification_before_capture(
    tmp_path: Path,
) -> None:
    definition = _qualification_definition()
    definition_path = tmp_path / "qualification.json"
    definition_path.write_bytes(encode_campaign_definition(definition))
    capture = _Capture()
    stdout = StringIO()

    class Lock:
        def acquire(self) -> None:
            pass

        def release(self) -> None:
            pass

    code = campaign_main(
        [
            "run-next",
            "--definition",
            str(definition_path),
            "--station-a",
            str(BASE_A),
            "--station-b",
            str(BASE_B),
            "--journal",
            str(tmp_path / "qualification.sqlite3"),
            "--campaign-state-root",
            str(tmp_path / "state"),
            "--campaign-lock",
            str(tmp_path / "campaign.lock"),
            "--now-utc-ns",
            str(
                int(definition.start_utc_ns) + definition.maximum_start_lateness_ns + 1
            ),
            "--capacity-margin-bytes",
            "0",
            "--arm",
            "--confirm-definition-digest",
            str(definition.digest),
        ],
        stdout=stdout,
        stderr=StringIO(),
        capture_builder=lambda *_args: capture,
        analysis_builder=lambda *_args: _Analysis(),
        capacity_builder=lambda _root: _Capacity(),
        lock_factory=lambda _path: Lock(),
    )

    assert code == 4
    assert json.loads(stdout.getvalue())["status"] == "missed_slot"
    assert capture.calls == []
