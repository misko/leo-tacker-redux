from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from io import StringIO
from pathlib import Path

import pytest

from leo_flow.adapters.continuous_collection_sqlite import (
    SQLiteContinuousCollectionJournal,
)
from leo_flow.application.deferred_analysis import (
    DeferredAnalysisWindowRunV1,
    DeferredAnalysisWindowStatus,
    OnlineAnalysisWindowRunV1,
    OnlineAnalysisWindowStatus,
)
from leo_flow.capture.campaign import CampaignDefinition
from leo_flow.capture.campaign_codec import (
    encode_campaign_definition,
    encode_qualification_receipt,
)
from leo_flow.capture.continuous import (
    DeferredCampaignCoordinator,
)
from leo_flow.contracts.core import UtcNs
from leo_flow.contracts.deferred_analysis import DeferredAnalysisStage
from leo_flow.deployments.v5_continuous_operator import main
from tests.capture.test_campaign import (
    BASE_A,
    BASE_B,
    START,
    _Analysis,
    _Capacity,
    _Capture,
    _main_definition,
)


class _Gate:
    def __init__(self, ready: bool) -> None:
        self.is_ready = ready
        self.calls = 0

    def ready(self) -> bool:
        self.calls += 1
        return self.is_ready


class _Lock:
    acquired = 0
    released = 0

    def __init__(self, _path: Path) -> None:
        pass

    def acquire(self) -> None:
        type(self).acquired += 1

    def release(self) -> None:
        type(self).released += 1


def _files(tmp_path: Path) -> tuple[Path, Path]:
    definition, receipt = _main_definition()
    definition = replace(definition, analysis_after_each_capture=False)
    definition_path = tmp_path / "main.definition.json"
    receipt_path = tmp_path / "qualification.receipt.json"
    definition_path.write_bytes(encode_campaign_definition(definition))
    receipt_path.write_bytes(encode_qualification_receipt(receipt))
    return definition_path, receipt_path


def _args(tmp_path: Path, command: str) -> list[str]:
    definition, receipt = _files(tmp_path)
    decoded, _ = _main_definition()
    decoded = replace(decoded, analysis_after_each_capture=False)
    values = [
        command,
        "--definition",
        str(definition),
        "--qualification-receipt",
        str(receipt),
        "--station-a",
        str(BASE_A),
        "--station-b",
        str(BASE_B),
        "--journal",
        str(tmp_path / "continuous.sqlite3"),
        "--campaign-state-root",
        str(tmp_path / "state"),
        "--campaign-lock",
        str(tmp_path / "campaign.lock"),
        "--capacity-margin-bytes",
        "0",
        "--arm",
        "--confirm-definition-digest",
        str(decoded.digest),
    ]
    if command == "capture-next":
        values.extend(("--now-utc-ns", str(START)))
    elif command == "analyze-next":
        values.extend(("--analysis-deadline-seconds", "30"))
    return values


def _invoke(
    args: list[str],
    capture: _Capture,
    analysis: _Analysis,
    gate: _Gate,
) -> tuple[int, StringIO, StringIO]:
    stdout, stderr = StringIO(), StringIO()
    code = main(
        args,
        stdout=stdout,
        stderr=stderr,
        capture_builder=lambda *_args: capture,
        analysis_builder=lambda _definition: analysis,
        capacity_builder=lambda _root: _Capacity(),
        start_admission=gate,
        lock_factory=_Lock,
        now_utc_ns=lambda: START + 1_000,
    )
    return code, stdout, stderr


def test_full_drain_is_required_only_before_first_persisted_transition(
    tmp_path: Path,
) -> None:
    capture = _Capture()
    analysis = _Analysis()
    blocked = _Gate(False)

    code, _, stderr = _invoke(
        _args(tmp_path, "capture-next"), capture, analysis, blocked
    )
    assert code == 4
    assert json.loads(stderr.getvalue()) == {"event": "continuous_transition_failed"}
    assert capture.calls == []
    assert blocked.calls == 1

    admitted = _Gate(True)
    code, stdout, stderr = _invoke(
        _args(tmp_path, "capture-next"), capture, analysis, admitted
    )
    assert code == 0
    assert stderr.getvalue() == ""
    assert json.loads(stdout.getvalue())["status"] == "captured"
    assert admitted.calls == 1

    second_gate = _Gate(False)
    second_start = START + 400_000_000_000 // 13
    args = _args(tmp_path, "capture-next")
    args[args.index("--now-utc-ns") + 1] = str(second_start - 15_000_000_000)
    code, stdout, stderr = _invoke(args, capture, analysis, second_gate)
    assert code == 0
    assert stderr.getvalue() == ""
    assert json.loads(stdout.getvalue())["captured_count"] == 2
    assert second_gate.calls == 0
    assert analysis.calls == []


def test_close_then_analyze_is_separate_and_status_survives_restart(
    tmp_path: Path,
) -> None:
    capture = _Capture()
    analysis = _Analysis()
    gate = _Gate(True)
    assert _invoke(_args(tmp_path, "capture-next"), capture, analysis, gate)[0] == 0
    assert _invoke(_args(tmp_path, "close"), capture, analysis, gate)[0] == 0

    code, stdout, stderr = _invoke(
        _args(tmp_path, "analyze-next"), capture, analysis, gate
    )
    assert code == 0
    assert stderr.getvalue() == ""
    assert json.loads(stdout.getvalue())["status"] == "analyzed"
    assert len(capture.calls) == len(analysis.calls) == 1

    definition, _ = _files(tmp_path)
    status_out, status_error = StringIO(), StringIO()
    code = main(
        [
            "status",
            "--definition",
            str(definition),
            "--journal",
            str(tmp_path / "continuous.sqlite3"),
        ],
        stdout=status_out,
        stderr=status_error,
    )
    assert code == 0
    assert status_error.getvalue() == ""
    payload = json.loads(status_out.getvalue())
    assert payload["phase"] == "analyzing"
    assert payload["captured_count"] == payload["analyzed_count"] == 1


def test_capture_run_closes_capture_without_invoking_analysis(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("leo_flow.capture.continuous.CAMPAIGN_SUCCESS_TARGET", 1)
    capture = _Capture()
    analysis = _Analysis()
    args = _args(tmp_path, "capture-run")
    args.extend(
        (
            "--maximum-transitions",
            "1873",
            "--maximum-runtime-seconds",
            "60",
        )
    )

    code, stdout, stderr = _invoke(args, capture, analysis, _Gate(True))

    assert code == 0
    assert stderr.getvalue() == ""
    events = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [item["status"] for item in events] == [
        "captured",
        "capture_phase_closed",
    ]
    assert events[-1]["phase"] == "analyzing"
    assert len(capture.calls) == 1
    assert analysis.calls == []


def test_capture_service_bound_counts_not_due_and_closes_without_restart(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("leo_flow.capture.continuous.CAMPAIGN_SUCCESS_TARGET", 2)
    monkeypatch.setattr(
        CampaignDefinition, "capture_run_transition_limit", property(lambda _self: 5)
    )
    capture = _Capture()
    analysis = _Analysis()
    args = _args(tmp_path, "capture-run")
    args.extend(
        (
            "--maximum-transitions",
            "5",
            "--maximum-runtime-seconds",
            "32400",
        )
    )
    clock_ns = START - 16_000_000_000
    elapsed_s = 0.0

    def now_utc_ns() -> int:
        return clock_ns

    def monotonic() -> float:
        return elapsed_s

    def delay(seconds: float) -> None:
        nonlocal clock_ns, elapsed_s
        clock_ns += round(seconds * 1_000_000_000)
        elapsed_s += seconds

    stdout, stderr = StringIO(), StringIO()
    code = main(
        args,
        stdout=stdout,
        stderr=stderr,
        capture_builder=lambda *_args: capture,
        analysis_builder=lambda _definition: analysis,
        capacity_builder=lambda _root: _Capacity(),
        start_admission=_Gate(True),
        lock_factory=_Lock,
        now_utc_ns=now_utc_ns,
        monotonic=monotonic,
        delay=delay,
    )

    assert code == 0
    assert stderr.getvalue() == ""
    events = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert len(events) == 5
    assert sum(item["status"] == "not_due" for item in events) == 2
    assert sum(item["status"] == "captured" for item in events) == 2
    assert events[-1]["status"] == "capture_phase_closed"
    assert events[-1]["phase"] == "analyzing"
    assert events[-1]["captured_count"] == 2
    assert len(capture.calls) == 2
    assert analysis.calls == []
    assert elapsed_s < 32_400


def test_analysis_drain_resumes_same_journal_until_complete(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("leo_flow.capture.continuous.CAMPAIGN_SUCCESS_TARGET", 1)
    capture = _Capture()
    analysis = _Analysis()
    capture_args = _args(tmp_path, "capture-run")
    capture_args.extend(
        (
            "--maximum-transitions",
            "1873",
            "--maximum-runtime-seconds",
            "30",
        )
    )
    assert _invoke(capture_args, capture, analysis, _Gate(True))[0] == 0

    drain_args = _args(tmp_path, "drain-analysis")
    drain_args.extend(
        (
            "--analysis-deadline-seconds",
            "30",
            "--maximum-transitions",
            "937",
            "--maximum-runtime-seconds",
            "60",
        )
    )
    code, stdout, stderr = _invoke(drain_args, capture, analysis, _Gate(False))
    assert code == 0
    assert stderr.getvalue() == ""
    assert json.loads(stdout.getvalue().splitlines()[-1])["phase"] == "complete"
    assert len(capture.calls) == len(analysis.calls) == 1


def test_analysis_drain_fails_closed_while_capture_phase_is_open(
    tmp_path: Path,
) -> None:
    capture = _Capture()
    analysis = _Analysis()
    args = _args(tmp_path, "drain-analysis")
    args.extend(
        (
            "--analysis-deadline-seconds",
            "30",
            "--maximum-transitions",
            "937",
            "--maximum-runtime-seconds",
            "30",
        )
    )

    code, stdout, stderr = _invoke(args, capture, analysis, _Gate(True))

    assert code == 4
    assert stderr.getvalue() == ""
    assert json.loads(stdout.getvalue())["event"] == (
        "continuous_analysis_phase_not_open"
    )
    assert capture.calls == analysis.calls == []


def _seed_staged_journal(
    tmp_path: Path, capture: _Capture, analysis: _Analysis
) -> None:
    definition, receipt = _main_definition()
    definition = replace(definition, analysis_after_each_capture=False)
    journal = SQLiteContinuousCollectionJournal(tmp_path / "continuous.sqlite3")
    coordinator = DeferredCampaignCoordinator(
        definition, journal, capture, analysis, _Capacity(), 0, receipt
    )
    for index in range(36):
        requested = START + index * 400_000_000_000 // 13
        coordinator.capture_next(UtcNs(requested))
    coordinator.close_capture()


def test_staged_drain_reconciles_window_then_closes_under_two_locks(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        CampaignDefinition, "target_successes", property(lambda _self: 36)
    )
    capture, analysis = _Capture(), _Analysis()
    _seed_staged_journal(tmp_path, capture, analysis)
    args = _args(tmp_path, "drain-analysis-staged")
    args.extend(
        (
            "--analysis-deadline-seconds",
            "30",
            "--window-batches",
            "36",
            "--compute-workers",
            "8",
            "--projection-workers",
            "4",
            "--maximum-transitions",
            "2",
            "--maximum-runtime-seconds",
            "60",
        )
    )

    class Staged:
        def __init__(self, coordinator):
            self.coordinator = coordinator

        def advance_window(self, state, *, deadline_utc_ns):
            first = state.analyzed_count
            for _ in range(36):
                result = self.coordinator.analyze_next(deadline_utc_ns=deadline_utc_ns)
                assert result.status.value == "analyzed"
            return DeferredAnalysisWindowRunV1(
                DeferredAnalysisWindowStatus.ADVANCED, first, 36
            )

    _Lock.acquired = _Lock.released = 0
    stdout, stderr = StringIO(), StringIO()
    code = main(
        args,
        stdout=stdout,
        stderr=stderr,
        capture_builder=lambda *_args: capture,
        analysis_builder=lambda _definition: analysis,
        staged_analysis_builder=lambda _definition, coordinator, _compute, _projection: (
            Staged(coordinator)
        ),
        capacity_builder=lambda _root: _Capacity(),
        start_admission=_Gate(False),
        lock_factory=_Lock,
        now_utc_ns=lambda: START + 10_000,
    )

    assert code == 0
    assert stderr.getvalue() == ""
    assert json.loads(stdout.getvalue().splitlines()[-1])["phase"] == "complete"
    assert _Lock.acquired == _Lock.released == 2


def test_staged_park_durably_halts_and_does_not_restart_as_pending(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        CampaignDefinition, "target_successes", property(lambda _self: 36)
    )
    capture, analysis = _Capture(), _Analysis()
    _seed_staged_journal(tmp_path, capture, analysis)
    args = _args(tmp_path, "drain-analysis-staged")
    args.extend(
        (
            "--analysis-deadline-seconds",
            "30",
            "--window-batches",
            "36",
            "--compute-workers",
            "8",
            "--projection-workers",
            "4",
            "--maximum-transitions",
            "2",
            "--maximum-runtime-seconds",
            "60",
        )
    )

    class Parked:
        def advance_window(self, state, *, deadline_utc_ns):
            del state, deadline_utc_ns
            return DeferredAnalysisWindowRunV1(
                DeferredAnalysisWindowStatus.PARKED,
                0,
                terminal_stage=DeferredAnalysisStage.STARLINK_SUITE_COMPUTE,
                parked_ids=("job_parked",),
            )

    stdout, stderr = StringIO(), StringIO()
    code = main(
        args,
        stdout=stdout,
        stderr=stderr,
        capture_builder=lambda *_args: capture,
        analysis_builder=lambda _definition: analysis,
        staged_analysis_builder=lambda *_args: Parked(),
        capacity_builder=lambda _root: _Capacity(),
        start_admission=_Gate(False),
        lock_factory=_Lock,
        now_utc_ns=lambda: START + 10_000,
    )

    assert code == 4
    assert stderr.getvalue() == ""
    assert json.loads(stdout.getvalue().splitlines()[-1])["phase"] == "halted"


def test_staged_retryable_window_exits_75_and_preserves_analyzing_state(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        CampaignDefinition, "target_successes", property(lambda _self: 36)
    )
    capture, analysis = _Capture(), _Analysis()
    _seed_staged_journal(tmp_path, capture, analysis)
    args = _args(tmp_path, "drain-analysis-staged")
    args.extend(
        (
            "--analysis-deadline-seconds",
            "30",
            "--window-batches",
            "36",
            "--compute-workers",
            "8",
            "--projection-workers",
            "4",
            "--maximum-transitions",
            "2",
            "--maximum-runtime-seconds",
            "60",
        )
    )

    class Pending:
        def advance_window(self, state, *, deadline_utc_ns):
            del state, deadline_utc_ns
            return DeferredAnalysisWindowRunV1(
                DeferredAnalysisWindowStatus.PENDING,
                0,
                terminal_stage=DeferredAnalysisStage.STARLINK_SUITE_COMPUTE,
            )

    stdout, stderr = StringIO(), StringIO()
    code = main(
        args,
        stdout=stdout,
        stderr=stderr,
        capture_builder=lambda *_args: capture,
        analysis_builder=lambda _definition: analysis,
        staged_analysis_builder=lambda *_args: Pending(),
        capacity_builder=lambda _root: _Capacity(),
        start_admission=_Gate(False),
        lock_factory=_Lock,
        now_utc_ns=lambda: START + 10_000,
    )

    events = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert code == 75
    assert stderr.getvalue() == ""
    assert events[-1]["status"] == "pending"
    definition, _ = _main_definition()
    definition = replace(definition, analysis_after_each_capture=False)
    assert (
        SQLiteContinuousCollectionJournal(tmp_path / "continuous.sqlite3")
        .load(definition)
        .phase.value
        == "analyzing"
    )


def test_lock_release_failure_suppresses_success_and_fails_closed(
    tmp_path: Path,
) -> None:
    class ReleaseFailure(_Lock):
        def release(self) -> None:
            raise RuntimeError("private lock failure")

    stdout, stderr = StringIO(), StringIO()
    capture = _Capture()
    analysis = _Analysis()
    code = main(
        _args(tmp_path, "capture-next"),
        stdout=stdout,
        stderr=stderr,
        capture_builder=lambda *_args: capture,
        analysis_builder=lambda _definition: analysis,
        capacity_builder=lambda _root: _Capacity(),
        start_admission=_Gate(True),
        lock_factory=ReleaseFailure,
    )

    assert code == 4
    assert stdout.getvalue() == ""
    assert json.loads(stderr.getvalue()) == {"event": "continuous_transition_failed"}


def test_continuous_operator_rejects_per_capture_definition_before_journal(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path, "capture-next")
    definition, _ = _main_definition()
    definition_path = Path(args[args.index("--definition") + 1])
    definition_path.write_bytes(encode_campaign_definition(definition))
    args[args.index("--confirm-definition-digest") + 1] = str(definition.digest)

    code, stdout, stderr = _invoke(args, _Capture(), _Analysis(), _Gate(True))

    assert code == 3
    assert stdout.getvalue() == ""
    assert json.loads(stderr.getvalue()) == {"event": "continuous_arm_rejected"}
    assert not (tmp_path / "continuous.sqlite3").exists()


def test_gauss_help_imports_without_optional_postgres() -> None:
    script = """
import sys
sys.modules['psycopg'] = None
from leo_flow.deployments.gauss_v5_continuous_operator import main
try:
    main(['--help'])
except SystemExit as error:
    raise SystemExit(error.code)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0
    assert completed.stdout.startswith("usage: leo-v5-continuous")
    assert "capture-next" in completed.stdout
    assert "analyze-next" in completed.stdout
    assert "capture-run" in completed.stdout
    assert "drain-analysis" in completed.stdout
    assert completed.stderr == ""


def test_gauss_two_phase_commands_require_runtime_composition() -> None:
    from leo_flow.deployments.gauss_v5_continuous_operator import main as gauss_main

    for command in ("capture-run", "drain-analysis"):
        errors = StringIO()
        assert gauss_main([command], stdout=StringIO(), stderr=errors) == 2
        assert json.loads(errors.getvalue()) == {
            "event": "continuous_runtime_configuration_error"
        }


def test_online_analysis_uses_independent_lock_and_never_enters_capture_path(
    tmp_path: Path,
) -> None:
    definition, receipt = _main_definition()
    definition = replace(definition, analysis_after_each_capture=False)
    journal = SQLiteContinuousCollectionJournal(tmp_path / "continuous.sqlite3")
    coordinator = DeferredCampaignCoordinator(
        definition, journal, _Capture(), _Analysis(), _Capacity(), 0, receipt
    )
    for index in range(36):
        coordinator.capture_next(UtcNs(START + index * 400_000_000_000 // 13))
    before = journal.load(definition)

    args = _args(tmp_path, "drain-analysis-online")
    online_lock = tmp_path / "online-analysis.lock"
    args.extend(
        (
            "--analysis-deadline-seconds",
            "30",
            "--window-batches",
            "36",
            "--compute-workers",
            "8",
            "--projection-workers",
            "4",
            "--online-analysis-lock",
            str(online_lock),
            "--maximum-transitions",
            str(definition.staged_analysis_drain_transition_limit),
            "--maximum-runtime-seconds",
            "60",
        )
    )

    class Online:
        def advance_available(self, state, *, deadline_utc_ns):
            assert len(state.records) == 36
            assert deadline_utc_ns == START + 30_000_001_000
            return OnlineAnalysisWindowRunV1(OnlineAnalysisWindowStatus.CAUGHT_UP, None)

    paths = []

    class PathLock(_Lock):
        def __init__(self, path):
            paths.append(path)

    gate = _Gate(False)
    stdout, stderr = StringIO(), StringIO()

    def forbidden_capture(*_args):
        raise AssertionError("online analysis constructed the RF capture path")

    code = main(
        args,
        stdout=stdout,
        stderr=stderr,
        capture_builder=forbidden_capture,
        analysis_builder=lambda _definition: _Analysis(),
        staged_analysis_builder=lambda *_args: None,
        online_analysis_builder=lambda *_args: Online(),
        capacity_builder=lambda _root: _Capacity(),
        start_admission=gate,
        lock_factory=PathLock,
        now_utc_ns=lambda: START + 1_000,
        monotonic=iter((0.0, 0.0)).__next__,
    )

    assert code == 0
    assert stderr.getvalue() == ""
    assert paths == [online_lock]
    assert gate.calls == 0
    assert journal.load(definition) == before
    assert json.loads(stdout.getvalue())["status"] == "caught_up"


def test_gauss_capture_composes_registered_terminal_analysis_gate_v2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leo_flow.adapters.campaign_online_analysis_postgres import (
        PostgresRegisteredAnalysisSafetyGateV2,
    )
    from leo_flow.adapters.systemd_credentials import SystemdCredentialProvider
    from leo_flow.capture.v5_station import load_v5_capture_station
    from leo_flow.deployments import gauss_campaign_runtime, v5_continuous_operator
    from leo_flow.deployments.gauss_v5_continuous_operator import main as gauss_main

    runtime_path = Path(
        "deploy/gauss-campaign-r20-r21-postreboot-v1/runtime.json"
    ).resolve()
    first = load_v5_capture_station(
        Path("deploy/v5-scan/gauss-r20-science-postreboot-passive-v1.station.json")
    )
    second = load_v5_capture_station(
        Path("deploy/v5-scan/gauss-r21-science-postreboot-passive-v1.station.json")
    )
    definition, _ = _main_definition()
    definition = replace(
        definition,
        radio_a_id=first.radio.radio_id,
        radio_b_id=second.radio.radio_id,
        station_a_digest=first.specification_digest,
        station_b_digest=second.specification_digest,
        analysis_after_each_capture=False,
    )
    observed = False

    class CapturePort:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            nonlocal observed
            builder = kwargs["admission_builder"]
            assert callable(builder)
            gate = builder("postgresql://capture.invalid/catalog")
            assert isinstance(gate, PostgresRegisteredAnalysisSafetyGateV2)
            assert gate._capture_definition_digest == definition.digest
            observed = True

    def component_main(_arguments: list[str], **kwargs: object) -> int:
        builder = kwargs["capture_builder"]
        assert callable(builder)
        builder(definition, first, second, tmp_path / "state")
        return 91

    monkeypatch.setattr(SystemdCredentialProvider, "resolve", lambda *_args: "dsn")
    monkeypatch.setattr(
        gauss_campaign_runtime, "ProcessIsolatedCampaignCapture", CapturePort
    )
    monkeypatch.setattr(v5_continuous_operator, "main", component_main)

    assert (
        gauss_main(
            ["--runtime-config", str(runtime_path), "capture-run"],
            stdout=StringIO(),
            stderr=StringIO(),
        )
        == 91
    )
    assert observed


@pytest.mark.parametrize(
    "runtime_ips",
    [
        ["192.168.1.21", "192.168.1.20"],
        ["192.168.1.20", "192.168.1.19"],
    ],
)
def test_gauss_continuous_rejects_swapped_or_drifted_runtime_endpoints_before_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime_ips: list[str]
) -> None:
    from leo_flow.adapters.systemd_credentials import SystemdCredentialProvider
    from leo_flow.capture.v5_station import load_v5_capture_station
    from leo_flow.deployments import gauss_campaign_runtime, v5_continuous_operator
    from leo_flow.deployments.gauss_v5_continuous_operator import main as gauss_main

    runtime = json.loads(
        Path("deploy/gauss-campaign-r20-r21-postreboot-v1/runtime.json").read_text(
            encoding="utf-8"
        )
    )
    runtime["radio_ips"] = runtime_ips
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(json.dumps(runtime), encoding="utf-8")
    first = load_v5_capture_station(
        Path("deploy/v5-scan/gauss-r20-science-postreboot-passive-v1.station.json")
    )
    second = load_v5_capture_station(
        Path("deploy/v5-scan/gauss-r21-science-postreboot-passive-v1.station.json")
    )
    definition, _ = _main_definition()
    definition = replace(
        definition,
        radio_a_id=first.radio.radio_id,
        radio_b_id=second.radio.radio_id,
        station_a_digest=first.specification_digest,
        station_b_digest=second.specification_digest,
        analysis_after_each_capture=False,
    )
    constructed = False

    class ForbiddenCapturePort:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            nonlocal constructed
            constructed = True

    def component_main(_arguments: list[str], **kwargs: object) -> int:
        builder = kwargs["capture_builder"]
        assert callable(builder)
        with pytest.raises(ValueError, match="endpoints differ"):
            builder(definition, first, second, tmp_path / "state")
        return 91

    monkeypatch.setattr(SystemdCredentialProvider, "resolve", lambda *_args: "dsn")
    monkeypatch.setattr(
        gauss_campaign_runtime, "ProcessIsolatedCampaignCapture", ForbiddenCapturePort
    )
    monkeypatch.setattr(v5_continuous_operator, "main", component_main)

    assert (
        gauss_main(
            ["--runtime-config", str(runtime_path), "capture-run"],
            stdout=StringIO(),
            stderr=StringIO(),
        )
        == 91
    )
    assert constructed is False
