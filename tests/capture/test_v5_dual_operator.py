from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from dataclasses import replace
from io import StringIO
from pathlib import Path

import pytest

from leo_flow.adapters.capture_batch_sqlite import SQLiteCaptureBatchStateStore
from leo_flow.application.capture_batches import CaptureBatchCoordinator
from leo_flow.capture.batch_serialization import (
    decode_batch_definition,
    decode_batch_snapshot,
)
from leo_flow.capture.dual import (
    CaptureAttemptControl,
    CaptureAttemptFailureReason,
    CaptureAttemptRunResult,
)
from leo_flow.capture.scan_plan import (
    StarlinkEdgeScanSpec,
    build_starlink_edge_scan_plan,
)
from leo_flow.capture.v5_station import V5CaptureStation
from leo_flow.contracts.capture import GainMode, GainSetting
from leo_flow.contracts.capture_batch import (
    CaptureAttemptOutcome,
    CaptureAttemptState,
    CaptureBatchDefinition,
    CaptureBatchMode,
    ExpectedCaptureAttempt,
)
from leo_flow.contracts.core import (
    CaptureAttemptId,
    CaptureBatchId,
    Digest,
    PlanId,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    UtcNs,
    canonical_digest,
)
from leo_flow.contracts.dashboard_batch import (
    CaptureBatchDashboardView,
    DashboardAnalysisState,
    DashboardCaptureState,
)
from leo_flow.contracts.storage import (
    ObjectRef,
    PublishedRecordingRef,
    RecordingObjectRef,
)
from leo_flow.deployments import v5_dual_capture_operator
from leo_flow.deployments.process_isolated_capture import IsolatedAttemptPhaseFailure
from leo_flow.deployments.v5_canary import V5PlanCyclePhase
from leo_flow.deployments.v5_dual_capture_operator import (
    CATALOG_CREDENTIAL_NAME,
    ExitCode,
    OneShotCycleAttemptRunner,
    _pair_digest,
    _parser,
    _write_new,
    main,
)
from leo_flow.deployments.v5_scan import DEVELOPMENT_STATION


def test_help_uses_installed_name_and_describes_each_offline_or_live_command() -> None:
    help_text = _parser().format_help()
    normalized_help = " ".join(help_text.split())

    assert help_text.startswith("usage: leo-v5-dual-capture")
    assert "leo-flow-v5-dual-capture" not in help_text
    for description in (
        "validate the exact pair and batch without credentials, DB, CAS, or radio",
        "print the exact batch without credentials, DB, CAS, or radio",
        "exclusively create one canonical independent or coordinated batch",
        "export one durable public terminal snapshot without radio contact",
        "explicitly arm exactly two process-isolated V5 attempts",
    ):
        assert description in normalized_help


def test_help_import_does_not_require_optional_postgres_dependency() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; sys.modules['psycopg'] = None; "
                "from leo_flow.deployments.v5_dual_capture_operator import _parser; "
                "print(_parser().prog)"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "leo-v5-dual-capture\n"


def _stations(tmp_path: Path) -> tuple[V5CaptureStation, V5CaptureStation]:
    mode_lock = tmp_path / "pipeline-mode.lock"
    first = replace(
        DEVELOPMENT_STATION,
        state=replace(DEVELOPMENT_STATION.state, mode_lock_path=mode_lock),
    )
    radio = replace(
        first.radio,
        uri="ip:192.0.2.22",
        expected_serial="abstract-test-radio-b",
        radio_id=RadioId("radio_dual_test_b"),
        receiver_chain_ids=(
            ReceiverChainId("rx_dual_test_b_1"),
            ReceiverChainId("rx_dual_test_b_2"),
        ),
    )
    provisional = replace(first.plan, plan_id=PlanId("plan_dual_test_b"))
    capture_plan = build_starlink_edge_scan_plan(
        StarlinkEdgeScanSpec(
            plan_id=provisional.plan_id,
            radio_id=radio.radio_id,
            receiver_chain_ids=radio.receiver_chain_ids,
            gain=GainSetting(GainMode.AGC),
            sample_rate_hz=provisional.sample_rate_hz,
            bandwidth_hz=provisional.bandwidth_hz,
            sample_count=provisional.sample_count,
            edge_order=provisional.edge_order,
            lnb_lo_hz=provisional.lnb_lo_hz,
            edge_order_draw_u32=provisional.edge_order_draw_u32,
            arm_name=provisional.arm_name,
            hardware_block_samples=provisional.hardware_block_samples,
        )
    )
    state_root = tmp_path / "radio-b"
    second = replace(
        first,
        radio=radio,
        plan=replace(provisional, plan_digest=canonical_digest(capture_plan)),
        state=replace(
            first.state,
            state_root=state_root,
            recording_root=state_root / "recordings",
            spool_database=state_root / "capture-spool.sqlite3",
            lock_path=tmp_path / "radio-b.lock",
        ),
    )
    return first, second


def _definition(
    stations: tuple[V5CaptureStation, V5CaptureStation],
) -> CaptureBatchDefinition:
    first, second = stations
    return CaptureBatchDefinition(
        SchemaRef(CaptureBatchDefinition.SCHEMA_ID),
        CaptureBatchId("cbatch_dual_operator_test"),
        CaptureBatchMode.INDEPENDENT,
        (
            ExpectedCaptureAttempt(
                CaptureAttemptId("cattempt_dual_operator_a"),
                first.radio.radio_id,
                first.plan.plan_id,
                UtcNs(1_000),
            ),
            ExpectedCaptureAttempt(
                CaptureAttemptId("cattempt_dual_operator_b"),
                second.radio.radio_id,
                second.plan.plan_id,
                UtcNs(2_000),
            ),
        ),
    )


def _recording(suffix: str) -> PublishedRecordingRef:
    data = Digest.sha256(f"{suffix}:data".encode())
    metadata = Digest.sha256(f"{suffix}:metadata".encode())
    return PublishedRecordingRef(
        RecordingObjectRef(
            RecordingId(f"rec_dual_{suffix}"),
            ObjectRef(
                data,
                64,
                "application/octet-stream",
                "recording-data-v1",
                f"cas:sha256:{data.value}",
            ),
            ObjectRef(
                metadata,
                128,
                "application/json",
                "recording-metadata-v1",
                f"cas:sha256:{metadata.value}",
            ),
            Digest.sha256(f"{suffix}:manifest".encode()),
        )
    )


class _Credentials:
    def __init__(self, _directory: Path) -> None:
        pass

    def resolve(self, name: str) -> str:
        assert name == CATALOG_CREDENTIAL_NAME
        return "private-test-dsn"


class _DrainGate:
    def __init__(self, *, ready: bool = True, fail: bool = False) -> None:
        self.is_ready = ready
        self.fail = fail
        self.calls = 0

    def ready(self) -> bool:
        self.calls += 1
        if self.fail:
            raise RuntimeError("postgres://private:password@database")
        return self.is_ready


class _ProjectionWriter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.views: list[CaptureBatchDashboardView] = []

    def publish(self, view: CaptureBatchDashboardView) -> int:
        self.views.append(view)
        if self.fail:
            raise RuntimeError("private projection failure")
        return len(self.views)


class _Runner:
    def __init__(self, *, fail: bool = False, secret: str = "") -> None:
        self.fail = fail
        self.secret = secret

    def run(
        self, attempt: ExpectedCaptureAttempt, control: CaptureAttemptControl
    ) -> CaptureAttemptRunResult:
        assert control.ready_and_wait_for_release()
        if self.fail:
            raise RuntimeError(self.secret or "injected runner failure")
        observed = int(attempt.requested_start_utc_ns) + 10
        return CaptureAttemptRunResult(
            SchemaRef(CaptureAttemptRunResult.SCHEMA_ID),
            CaptureBatchId("cbatch_dual_operator_test"),
            attempt.attempt_id,
            attempt.radio_id,
            attempt.plan_id,
            UtcNs(observed),
            UtcNs(observed + 100),
            _recording(str(attempt.radio_id)),
        )


def _loaders(
    stations: tuple[V5CaptureStation, V5CaptureStation],
    definition: CaptureBatchDefinition,
) -> tuple[object, object]:
    def station_loader(path: Path) -> V5CaptureStation:
        return stations[0] if path.name == "a.json" else stations[1]

    def batch_loader(_path: Path) -> CaptureBatchDefinition:
        return definition

    return station_loader, batch_loader


def _capture_args(
    tmp_path: Path,
    stations: tuple[V5CaptureStation, V5CaptureStation],
    definition: CaptureBatchDefinition,
) -> list[str]:
    batch_digest = str(canonical_digest(definition))
    pair_digest = str(_pair_digest(definition, stations))
    return [
        "capture",
        "--station-a",
        "a.json",
        "--station-b",
        "b.json",
        "--batch",
        "batch.json",
        "--arm",
        "--confirm-analysis-stopped",
        "--confirm-radio-a-serial",
        stations[0].radio.expected_serial,
        "--confirm-radio-b-serial",
        stations[1].radio.expected_serial,
        "--confirm-batch-digest",
        batch_digest,
        "--confirm-pair-digest",
        pair_digest,
        "--credential-directory",
        str(tmp_path / "credentials"),
        "--batch-database",
        str(tmp_path / "batch.sqlite3"),
    ]


def _invoke(
    args: list[str],
    stations: tuple[V5CaptureStation, V5CaptureStation],
    definition: CaptureBatchDefinition,
    **dependencies: object,
) -> tuple[int, StringIO, StringIO]:
    stdout, stderr = StringIO(), StringIO()
    station_loader, batch_loader = _loaders(stations, definition)
    dependencies.setdefault("publisher_builder", lambda _dsn: _ProjectionWriter())
    dependencies.setdefault("drain_gate_builder", lambda _dsn: _DrainGate())
    code = main(
        args,
        stdout=stdout,
        stderr=stderr,
        station_loader=station_loader,  # type: ignore[arg-type]
        batch_loader=batch_loader,  # type: ignore[arg-type]
        **dependencies,  # type: ignore[arg-type]
    )
    return code, stdout, stderr


def test_validate_and_show_batch_are_offline_and_machine_readable(
    tmp_path: Path,
) -> None:
    stations = _stations(tmp_path)
    definition = _definition(stations)
    forbidden = lambda _value: (_ for _ in ()).throw(AssertionError("unexpected I/O"))
    for command in ("validate", "show-batch"):
        code, stdout, stderr = _invoke(
            [
                command,
                "--station-a",
                "a.json",
                "--station-b",
                "b.json",
                "--batch",
                "batch.json",
            ],
            stations,
            definition,
            store_factory=forbidden,
            credential_factory=forbidden,
            mode_lock_factory=forbidden,
        )
        assert code == ExitCode.OK
        assert stderr.getvalue() == ""
        payload = json.loads(stdout.getvalue())
        assert payload["event"] == "dual_configuration_valid"
        assert ("batch" in payload) is (command == "show-batch")


def test_arm_rejection_precedes_mode_lock_credentials_store_and_runner(
    tmp_path: Path,
) -> None:
    stations = _stations(tmp_path)
    definition = _definition(stations)
    args = _capture_args(tmp_path, stations, definition)
    args[args.index("--confirm-pair-digest") + 1] = "sha256:" + "0" * 64
    forbidden = lambda *_values: (_ for _ in ()).throw(AssertionError("unexpected I/O"))

    code, _, stderr = _invoke(
        args,
        stations,
        definition,
        store_factory=forbidden,
        credential_factory=forbidden,
        mode_lock_factory=forbidden,
        runner_builder=forbidden,
    )
    assert code == ExitCode.ARM_REJECTED
    assert json.loads(stderr.getvalue()) == {"event": "dual_capture_arm_rejected"}


def test_both_success_reaches_terminal_state_and_releases_mode_lock(
    tmp_path: Path,
) -> None:
    stations = _stations(tmp_path)
    definition = _definition(stations)
    writer = _ProjectionWriter()
    code, stdout, stderr = _invoke(
        _capture_args(tmp_path, stations, definition),
        stations,
        definition,
        credential_factory=_Credentials,
        runner_builder=lambda *_args: _Runner(),
        publisher_builder=lambda _dsn: writer,
    )

    assert code == ExitCode.OK
    assert stderr.getvalue() == ""
    payload = json.loads(stdout.getvalue())
    assert payload["event"] == "dual_capture_terminal"
    assert payload["replay"] is False
    assert [item["state"] for item in payload["snapshot"]["outcomes"]] == [
        "succeeded",
        "succeeded",
    ]
    assert len(writer.views) == 1
    assert all(
        item.capture_state is DashboardCaptureState.SUCCEEDED
        and item.analysis_state is DashboardAnalysisState.PENDING
        for item in writer.views[0].attempts
    )
    _assert_lock_available(stations[0].state.mode_lock_path)


def test_undrained_analysis_blocks_both_radio_runners(tmp_path: Path) -> None:
    stations = _stations(tmp_path)
    definition = _definition(stations)
    drain_gate = _DrainGate(ready=False)
    forbidden_runner = lambda *_values: (_ for _ in ()).throw(
        AssertionError("undrained analysis must block runner construction")
    )

    code, stdout, stderr = _invoke(
        _capture_args(tmp_path, stations, definition),
        stations,
        definition,
        credential_factory=_Credentials,
        drain_gate_builder=lambda dsn: (
            drain_gate
            if dsn == "private-test-dsn"
            else (_ for _ in ()).throw(AssertionError("unexpected DSN"))
        ),
        process_supervisor_factory=forbidden_runner,
    )
    assert code == ExitCode.CAPTURE_FAILED
    assert stdout.getvalue() == ""
    assert json.loads(stderr.getvalue()) == {"event": "dual_capture_admission_blocked"}
    assert drain_gate.calls == 1
    _assert_lock_available(stations[0].state.mode_lock_path)


def test_dual_admission_database_error_fails_closed_and_releases_lock(
    tmp_path: Path,
) -> None:
    stations = _stations(tmp_path)
    definition = _definition(stations)
    forbidden_runner = lambda *_values: (_ for _ in ()).throw(
        AssertionError("admission error must block runner construction")
    )
    code, stdout, stderr = _invoke(
        _capture_args(tmp_path, stations, definition),
        stations,
        definition,
        credential_factory=_Credentials,
        drain_gate_builder=lambda _dsn: _DrainGate(fail=True),
        process_supervisor_factory=forbidden_runner,
    )
    assert code == ExitCode.CAPTURE_FAILED
    assert stdout.getvalue() == ""
    assert json.loads(stderr.getvalue()) == {"event": "dual_capture_admission_blocked"}
    assert "password" not in stderr.getvalue()
    _assert_lock_available(stations[0].state.mode_lock_path)


def test_peer_failure_keeps_success_terminal_and_sanitizes_runner_error(
    tmp_path: Path,
) -> None:
    stations = _stations(tmp_path)
    definition = _definition(stations)

    def builder(
        station: V5CaptureStation, _dsn: str, _batch: CaptureBatchId
    ) -> _Runner:
        return _Runner(
            fail=station is stations[1], secret="postgres://user:password@private"
        )

    writer = _ProjectionWriter()
    code, stdout, stderr = _invoke(
        _capture_args(tmp_path, stations, definition),
        stations,
        definition,
        credential_factory=_Credentials,
        runner_builder=builder,
        publisher_builder=lambda _dsn: writer,
    )
    assert code == ExitCode.CAPTURE_FAILED
    assert stderr.getvalue() == ""
    assert "password" not in stdout.getvalue()
    outcomes = json.loads(stdout.getvalue())["snapshot"]["outcomes"]
    assert [item["state"] for item in outcomes] == ["succeeded", "failed"]
    assert outcomes[1]["failure_reason"] == "capture_runner_failed"
    assert [item.capture_state for item in writer.views[0].attempts] == [
        DashboardCaptureState.SUCCEEDED,
        DashboardCaptureState.FAILED,
    ]
    _assert_lock_available(stations[0].state.mode_lock_path)


def test_terminal_replay_republishes_without_runners_or_cas_scan(
    tmp_path: Path,
) -> None:
    stations = _stations(tmp_path)
    definition = _definition(stations)
    args = _capture_args(tmp_path, stations, definition)
    first, _, _ = _invoke(
        args,
        stations,
        definition,
        credential_factory=_Credentials,
        runner_builder=lambda *_args: _Runner(),
    )
    assert first == ExitCode.OK
    forbidden = lambda *_values: (_ for _ in ()).throw(AssertionError("unexpected"))

    code, stdout, stderr = _invoke(
        args,
        stations,
        definition,
        credential_factory=_Credentials,
        process_supervisor_factory=forbidden,
    )
    assert code == ExitCode.OK
    assert stderr.getvalue() == ""
    assert json.loads(stdout.getvalue())["replay"] is True


def test_publication_outage_preserves_terminal_sqlite_and_replay_retries_it(
    tmp_path: Path,
) -> None:
    stations = _stations(tmp_path)
    definition = _definition(stations)
    args = _capture_args(tmp_path, stations, definition)
    failed_writer = _ProjectionWriter(fail=True)

    code, stdout, stderr = _invoke(
        args,
        stations,
        definition,
        credential_factory=_Credentials,
        runner_builder=lambda *_args: _Runner(),
        publisher_builder=lambda _dsn: failed_writer,
    )
    assert code == ExitCode.PUBLICATION_FAILED
    assert stdout.getvalue() == ""
    failure = json.loads(stderr.getvalue())
    assert failure["event"] == "dual_capture_publication_failed"
    assert len(failure["snapshot"]["outcomes"]) == 2
    persisted = SQLiteCaptureBatchStateStore(tmp_path / "batch.sqlite3").get(
        definition.batch_id
    )
    assert persisted is not None and persisted.terminal

    writer = _ProjectionWriter()
    forbidden_runner = lambda *_values: (_ for _ in ()).throw(
        AssertionError("terminal replay must not build a radio/CAS runner")
    )
    code, stdout, stderr = _invoke(
        args,
        stations,
        definition,
        credential_factory=_Credentials,
        runner_builder=forbidden_runner,
        publisher_builder=lambda _dsn: writer,
    )
    assert code == ExitCode.OK
    assert stderr.getvalue() == ""
    assert json.loads(stdout.getvalue())["replay"] is True
    assert len(writer.views) == 1


def test_partial_batch_refuses_recapture_before_credentials_or_runners(
    tmp_path: Path,
) -> None:
    stations = _stations(tmp_path)
    definition = _definition(stations)
    coordinator = CaptureBatchCoordinator(
        SQLiteCaptureBatchStateStore(tmp_path / "batch.sqlite3")
    )
    coordinator.register(definition)
    first_attempt = definition.expected_attempts[0]
    coordinator.record(
        CaptureAttemptOutcome(
            SchemaRef(CaptureAttemptOutcome.SCHEMA_ID),
            definition.batch_id,
            first_attempt.attempt_id,
            first_attempt.radio_id,
            first_attempt.plan_id,
            CaptureAttemptState.FAILED,
            UtcNs(3_000),
            failure_reason="test_failure",
        )
    )
    forbidden = lambda *_values: (_ for _ in ()).throw(AssertionError("unexpected"))

    code, stdout, stderr = _invoke(
        _capture_args(tmp_path, stations, definition),
        stations,
        definition,
        credential_factory=forbidden,
        runner_builder=forbidden,
    )
    assert code == ExitCode.CAPTURE_FAILED
    assert stdout.getvalue() == ""
    assert json.loads(stderr.getvalue()) == {"event": "dual_capture_failed"}
    _assert_lock_available(stations[0].state.mode_lock_path)


def test_real_mode_lock_contention_fails_before_store_credentials_or_runner(
    tmp_path: Path,
) -> None:
    stations = _stations(tmp_path)
    definition = _definition(stations)
    path = stations[0].state.mode_lock_path
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    forbidden = lambda *_values: (_ for _ in ()).throw(AssertionError("unexpected"))
    try:
        code, stdout, stderr = _invoke(
            _capture_args(tmp_path, stations, definition),
            stations,
            definition,
            store_factory=forbidden,
            credential_factory=forbidden,
            runner_builder=forbidden,
        )
    finally:
        os.close(descriptor)
    assert code == ExitCode.CAPTURE_FAILED
    assert stdout.getvalue() == ""
    assert json.loads(stderr.getvalue()) == {"event": "dual_capture_failed"}


def test_plan_batch_writes_canonical_round_trippable_bytes_without_overwrite(
    tmp_path: Path,
) -> None:
    stations = _stations(tmp_path)
    definition = _definition(stations)
    output = tmp_path / "planned-batch.json"
    args = [
        "plan-batch",
        "--station-a",
        "a.json",
        "--station-b",
        "b.json",
        "--mode",
        "independent",
        "--batch-id",
        str(definition.batch_id),
        "--attempt-a-id",
        str(definition.expected_attempts[0].attempt_id),
        "--attempt-b-id",
        str(definition.expected_attempts[1].attempt_id),
        "--requested-start-a-utc-ns",
        "1000",
        "--requested-start-b-utc-ns",
        "2000",
        "--output",
        str(output),
    ]
    forbidden = lambda *_values: (_ for _ in ()).throw(AssertionError("unexpected"))

    code, stdout, stderr = _invoke(
        args,
        stations,
        definition,
        store_factory=forbidden,
        credential_factory=forbidden,
        runner_builder=forbidden,
        publisher_builder=forbidden,
        mode_lock_factory=forbidden,
    )
    assert code == ExitCode.OK
    assert stderr.getvalue() == ""
    assert decode_batch_definition(output.read_bytes()) == definition
    payload = json.loads(stdout.getvalue())
    assert payload["batch_digest"] == str(canonical_digest(definition))
    assert payload["pair_digest"] == str(_pair_digest(definition, stations))
    original = output.read_bytes()

    code, stdout, stderr = _invoke(
        args,
        stations,
        definition,
        store_factory=forbidden,
        credential_factory=forbidden,
        runner_builder=forbidden,
        publisher_builder=forbidden,
        mode_lock_factory=forbidden,
    )
    assert code == ExitCode.USAGE_OR_CONFIG
    assert stdout.getvalue() == ""
    assert json.loads(stderr.getvalue()) == {"event": "dual_batch_plan_error"}
    assert output.read_bytes() == original


def test_plan_coordinated_batch_uses_one_common_requested_start(
    tmp_path: Path,
) -> None:
    stations = _stations(tmp_path)
    definition = _definition(stations)
    args = [
        "create-batch",
        "--station-a",
        "a.json",
        "--station-b",
        "b.json",
        "--mode",
        "coordinated",
        "--batch-id",
        "cbatch_planned_coordinated",
        "--attempt-a-id",
        "cattempt_planned_a",
        "--attempt-b-id",
        "cattempt_planned_b",
        "--common-requested-start-utc-ns",
        "5000",
        "--maximum-observed-start-skew-ns",
        "250000",
    ]

    code, stdout, stderr = _invoke(args, stations, definition)
    assert code == ExitCode.OK
    assert stderr.getvalue() == ""
    planned = decode_batch_definition(
        json.dumps(
            json.loads(stdout.getvalue())["batch"],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    assert planned.mode is CaptureBatchMode.COORDINATED
    assert {int(item.requested_start_utc_ns) for item in planned.expected_attempts} == {
        5_000
    }
    assert planned.maximum_observed_start_skew_ns == 250_000


def test_plan_batch_exclusive_writer_rejects_existing_symlink(tmp_path: Path) -> None:
    target = tmp_path / "existing.json"
    target.write_bytes(b"preserve-me")
    output = tmp_path / "planned.json"
    output.symlink_to(target)

    with pytest.raises(FileExistsError):
        _write_new(output, b'{"must":"not-write"}')
    assert target.read_bytes() == b"preserve-me"


def test_plan_batch_exclusive_writer_closes_file_and_directory_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open = os.open
    real_close = os.close
    open_descriptors: set[int] = set()

    def tracked_open(*args: object, **kwargs: object) -> int:
        descriptor = real_open(*args, **kwargs)  # type: ignore[arg-type]
        open_descriptors.add(descriptor)
        return descriptor

    def tracked_close(descriptor: int) -> None:
        open_descriptors.remove(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "close", tracked_close)
    output = tmp_path / "planned.json"

    _write_new(output, b'{"batch":"exact"}')

    assert output.read_bytes() == b'{"batch":"exact"}'
    assert open_descriptors == set()


def test_show_state_emits_direct_canonical_snapshot_without_station_or_radio(
    tmp_path: Path,
) -> None:
    stations = _stations(tmp_path)
    definition = _definition(stations)
    store = SQLiteCaptureBatchStateStore(tmp_path / "batch.sqlite3")
    coordinator = CaptureBatchCoordinator(store)
    expected = coordinator.register(definition)
    forbidden = lambda *_values: (_ for _ in ()).throw(AssertionError("unexpected"))
    stdout, stderr = StringIO(), StringIO()

    code = main(
        [
            "show-state",
            "--batch-database",
            str(tmp_path / "batch.sqlite3"),
            "--batch-id",
            str(definition.batch_id),
        ],
        stdout=stdout,
        stderr=stderr,
        station_loader=forbidden,
        credential_factory=forbidden,
        runner_builder=forbidden,
        publisher_builder=forbidden,
    )
    assert code == ExitCode.OK
    assert stderr.getvalue() == ""
    assert decode_batch_snapshot(stdout.getvalue().encode()) == expected


def test_show_state_missing_batch_fails_sanitized(tmp_path: Path) -> None:
    stdout, stderr = StringIO(), StringIO()
    database = tmp_path / "batch.sqlite3"
    code = main(
        [
            "show-state",
            "--batch-database",
            str(database),
            "--batch-id",
            "cbatch_missing_private_detail",
        ],
        stdout=stdout,
        stderr=stderr,
    )
    assert code == ExitCode.USAGE_OR_CONFIG
    assert stdout.getvalue() == ""
    assert json.loads(stderr.getvalue()) == {"event": "dual_state_error"}
    assert not database.exists()


class _Cycle:
    def __init__(self, *, close_fails: bool = False) -> None:
        self.close_fails = close_fails
        self.calls: list[object] = []

    def preflight(self, phase_observer=None) -> None:
        self.calls.append("preflight")
        if phase_observer is not None:
            phase_observer(V5PlanCyclePhase.CYCLE_PREFLIGHT)

    def prepare_first_segment(self) -> None:
        self.calls.append("prepare_first_segment")

    def capture_and_publish_once(self, phase_observer=None) -> bool:
        self.calls.append("capture")
        if phase_observer is not None:
            phase_observer(V5PlanCyclePhase.CAPTURE_ENGINE)
        return True

    def close(self, timeout_s: float) -> None:
        self.calls.append(("close", timeout_s))
        if self.close_fails:
            raise RuntimeError("private close failure")


class _Control:
    cancelled = False

    def ready_and_wait_for_release(self) -> bool:
        return True


class _Resolver:
    def __init__(self, result: CaptureAttemptRunResult | None) -> None:
        self.result = result

    def preflight(self, phase_observer) -> None:
        phase_observer(V5PlanCyclePhase.HOST_SPOOL_PREFLIGHT)
        phase_observer(V5PlanCyclePhase.CATALOG_PREFLIGHT)

    def resolve(self, _attempt: ExpectedCaptureAttempt) -> CaptureAttemptRunResult:
        if self.result is None:
            raise RuntimeError("recording continuity is not verified")
        return self.result


def test_isolated_station_work_prepares_first_segment_before_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    station = _stations(tmp_path)[0]
    definition = _definition(_stations(tmp_path))
    cycle = _Cycle()
    resolver_builds: list[_Resolver] = []
    monkeypatch.setattr(
        v5_dual_capture_operator,
        "build_station_capture_cycle",
        lambda _station, _credential: cycle,
    )

    def build_resolver(*_args) -> _Resolver:
        resolver = _Resolver(None)
        resolver_builds.append(resolver)
        return resolver

    monkeypatch.setattr(
        v5_dual_capture_operator,
        "_PublishedResolver",
        build_resolver,
    )

    work = v5_dual_capture_operator._StationAttemptWork(
        station, "private-catalog-credential", definition.batch_id
    )
    assert resolver_builds == []
    work.preflight()

    assert cycle.calls == ["preflight", "prepare_first_segment"]
    assert len(resolver_builds) == 1


class _FailingPhasedCycle(_Cycle):
    def __init__(self, phase: V5PlanCyclePhase | str) -> None:
        super().__init__()
        self.phase = phase

    def preflight(self, phase_observer=None) -> None:
        if isinstance(self.phase, V5PlanCyclePhase) and self.phase in {
            V5PlanCyclePhase.CYCLE_PREFLIGHT,
            V5PlanCyclePhase.HOST_SPOOL_PREFLIGHT,
            V5PlanCyclePhase.CATALOG_PREFLIGHT,
            V5PlanCyclePhase.RADIO_ATTESTATION,
        }:
            assert phase_observer is not None
            phase_observer(self.phase)
            raise RuntimeError("private preflight detail")
        super().preflight(phase_observer)

    def prepare_first_segment(self) -> None:
        if self.phase == "first_segment":
            raise RuntimeError("private configuration value")
        super().prepare_first_segment()

    def capture_and_publish_once(self, phase_observer=None) -> bool:
        if isinstance(self.phase, V5PlanCyclePhase) and self.phase in {
            V5PlanCyclePhase.CAPTURE_ENGINE,
            V5PlanCyclePhase.RECORDING_PUBLICATION,
        }:
            assert phase_observer is not None
            phase_observer(self.phase)
            raise RuntimeError("private capture detail")
        return super().capture_and_publish_once(phase_observer)


@pytest.mark.parametrize(
    ("phase", "expected"),
    (
        (
            V5PlanCyclePhase.CYCLE_PREFLIGHT,
            CaptureAttemptFailureReason.CYCLE_PREFLIGHT,
        ),
        (
            V5PlanCyclePhase.HOST_SPOOL_PREFLIGHT,
            CaptureAttemptFailureReason.HOST_SPOOL_PREFLIGHT,
        ),
        (
            V5PlanCyclePhase.CATALOG_PREFLIGHT,
            CaptureAttemptFailureReason.CATALOG_PREFLIGHT,
        ),
        (
            V5PlanCyclePhase.RADIO_ATTESTATION,
            CaptureAttemptFailureReason.RADIO_ATTESTATION,
        ),
        (
            "first_segment",
            CaptureAttemptFailureReason.FIRST_SEGMENT_CONFIGURATION,
        ),
    ),
)
def test_isolated_station_work_sanitizes_exact_pre_ready_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: V5PlanCyclePhase | str,
    expected: CaptureAttemptFailureReason,
) -> None:
    station = _stations(tmp_path)[0]
    definition = _definition(_stations(tmp_path))
    monkeypatch.setattr(
        v5_dual_capture_operator,
        "build_station_capture_cycle",
        lambda _station, _credential: _FailingPhasedCycle(phase),
    )
    monkeypatch.setattr(
        v5_dual_capture_operator,
        "_PublishedResolver",
        lambda _station, _credential, _batch_id: _Resolver(None),
    )
    work = v5_dual_capture_operator._StationAttemptWork(
        station, "private-catalog-credential", definition.batch_id
    )

    with pytest.raises(IsolatedAttemptPhaseFailure) as raised:
        work.preflight()

    assert raised.value.reason is expected
    assert str(raised.value) == "isolated capture phase failed"


@pytest.mark.parametrize(
    ("phase", "expected"),
    (
        (
            V5PlanCyclePhase.CAPTURE_ENGINE,
            CaptureAttemptFailureReason.CAPTURE_ENGINE,
        ),
        (
            V5PlanCyclePhase.RECORDING_PUBLICATION,
            CaptureAttemptFailureReason.RECORDING_PUBLICATION,
        ),
        ("resolution", CaptureAttemptFailureReason.RECORDING_RESOLUTION),
    ),
)
def test_isolated_station_work_sanitizes_exact_post_release_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: V5PlanCyclePhase | str,
    expected: CaptureAttemptFailureReason,
) -> None:
    station = _stations(tmp_path)[0]
    definition = _definition(_stations(tmp_path))
    attempt = definition.expected_attempts[0]
    cycle = _FailingPhasedCycle(phase)
    monkeypatch.setattr(
        v5_dual_capture_operator,
        "build_station_capture_cycle",
        lambda _station, _credential: cycle,
    )
    monkeypatch.setattr(
        v5_dual_capture_operator,
        "_PublishedResolver",
        lambda _station, _credential, _batch_id: _Resolver(None),
    )
    work = v5_dual_capture_operator._StationAttemptWork(
        station, "private-catalog-credential", definition.batch_id
    )

    with pytest.raises(IsolatedAttemptPhaseFailure) as raised:
        work.capture(attempt)

    assert raised.value.reason is expected
    assert str(raised.value) == "isolated capture phase failed"


@pytest.mark.parametrize(
    ("close_fails", "continuity_missing"), ((False, True), (True, False))
)
def test_attempt_runner_closes_on_missing_continuity_and_close_failure(
    tmp_path: Path, close_fails: bool, continuity_missing: bool
) -> None:
    stations = _stations(tmp_path)
    definition = _definition(stations)
    attempt = definition.expected_attempts[0]
    cycle = _Cycle(close_fails=close_fails)
    observed = int(attempt.requested_start_utc_ns) + 10
    resolved = CaptureAttemptRunResult(
        SchemaRef(CaptureAttemptRunResult.SCHEMA_ID),
        definition.batch_id,
        attempt.attempt_id,
        attempt.radio_id,
        attempt.plan_id,
        UtcNs(observed),
        UtcNs(observed + 100),
        _recording("direct-runner"),
    )
    runner = OneShotCycleAttemptRunner(
        cycle, _Resolver(None if continuity_missing else resolved)
    )

    with pytest.raises(RuntimeError, match="station capture attempt failed") as raised:
        runner.run(attempt, _Control())
    assert "continuity" not in str(raised.value)
    assert cycle.calls == ["preflight", "capture", ("close", 10.0)]


def _assert_lock_available(path: Path) -> None:
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(descriptor)
