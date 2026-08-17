from __future__ import annotations

import json
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest

from leo_flow.capture.drivers.v5_preflight import ObservedV5Runtime
from leo_flow.capture.v5_station import V5CaptureStation
from leo_flow.deployments.v5_capture_operator import (
    ExclusiveModeLock,
    ExitCode,
    _parser,
    main,
)
from leo_flow.deployments.v5_radio_firmware import VerifiedV5RadioFirmware
from leo_flow.deployments.v5_scan import DEVELOPMENT_STATION


class _ForbiddenCredentials:
    def resolve(self, _name: str) -> str:
        raise AssertionError("offline command must not read credentials")


class _FakeCredentials:
    def __init__(self, directory: Path) -> None:
        assert directory == Path("/run/credentials/test")

    def resolve(self, name: str) -> str:
        assert name == "catalog-dsn"
        return "secret-dsn-not-for-output"


class _Cycle:
    def __init__(self, *, progressed: bool = True) -> None:
        self.progressed = progressed
        self.calls: list[object] = []

    def preflight(self) -> None:
        self.calls.append("preflight")

    def capture_and_publish_once(self) -> bool:
        self.calls.append("capture")
        return self.progressed

    def close(self, timeout_s: float) -> None:
        self.calls.append(("close", timeout_s))


class _ModeLock:
    def __init__(
        self, path: Path, *, fail: bool = False, release_fail: bool = False
    ) -> None:
        assert path == DEVELOPMENT_STATION.state.mode_lock_path
        self.fail = fail
        self.release_fail = release_fail
        self.calls: list[str] = []

    def acquire(self) -> None:
        self.calls.append("acquire")
        if self.fail:
            raise RuntimeError("contended")

    def release(self) -> None:
        self.calls.append("release")
        if self.release_fail:
            raise RuntimeError("private lock cleanup failure")


class _DrainGate:
    def __init__(self, *, ready: bool = True, fail: bool = False) -> None:
        self.is_ready = ready
        self.fail = fail
        self.calls = 0

    def ready(self) -> bool:
        self.calls += 1
        if self.fail:
            raise RuntimeError("private database failure")
        return self.is_ready


def _forbidden_builder(_station: V5CaptureStation, _dsn: str) -> _Cycle:
    raise AssertionError("offline command or rejected arm must not build a cycle")


def _observed_runtime(station: V5CaptureStation) -> ObservedV5Runtime:
    expected = station.expected_runtime
    return ObservedV5Runtime(
        runtime_id=expected.runtime_id,
        schema=expected.schema,
        iio_module_path=expected.iio_module_path,
        iio_version=expected.iio_version,
        iio_commit=expected.iio_commit,
        metadata_buffer_present=True,
        native_libiio_paths=(f"{expected.native_libiio_prefix}/lib/libiio.so.0.25",),
        available_backends=expected.required_backends,
        pyadi_version=expected.pyadi_version,
        pyadi_module_path=expected.pyadi_module_path,
        spf_module_path=expected.spf_module_path,
        spf_revision=expected.spf_revision,
        spf_import=expected.spf_import,
        metadata_protocol=expected.metadata_protocol,
    )


def test_help_uses_the_installed_console_script_name() -> None:
    help_text = _parser().format_help()

    assert help_text.startswith("usage: leo-v5-capture")
    assert "leo-flow-v5-capture" not in help_text


def test_help_import_does_not_require_optional_postgres_dependency() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; sys.modules['psycopg'] = None; "
                "from leo_flow.deployments.v5_capture_operator import _parser; "
                "print(_parser().prog)"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "leo-v5-capture\n"


def test_validate_and_show_plan_are_offline_machine_readable_commands() -> None:
    for command in ("validate", "show-plan"):
        stdout = StringIO()
        stderr = StringIO()
        code = main(
            [command],
            stdout=stdout,
            stderr=stderr,
            cycle_builder=_forbidden_builder,
            credential_provider_factory=lambda _path: _ForbiddenCredentials(),
        )
        assert code == ExitCode.OK
        assert stderr.getvalue() == ""
        payload = json.loads(stdout.getvalue())
        assert payload["radio_uri"] == "ip:192.168.1.15"
        assert payload["radio_serial"] == DEVELOPMENT_STATION.radio.expected_serial
        assert payload["plan_digest"] == str(DEVELOPMENT_STATION.plan.plan_digest)
        assert payload["mode_lock_path"] == (
            "/home/mouse9911/.local/state/leo-flow/pipeline-mode.lock"
        )
        assert ("plan" in payload) is (command == "show-plan")


def test_verify_firmware_is_offline_machine_readable_and_sanitizes_failure() -> None:
    stdout = StringIO()
    code = main(
        [
            "verify-firmware",
            "--receipt",
            "/tmp/candidate.json",
            "--confirm-receipt-sha256",
            "d" * 64,
        ],
        stdout=stdout,
        stderr=StringIO(),
        cycle_builder=_forbidden_builder,
        credential_provider_factory=lambda _path: _ForbiddenCredentials(),
        firmware_verifier=lambda path, digest: (
            VerifiedV5RadioFirmware(
                "candidate-1",
                "release-1",
                "d" * 64,
                "a" * 64,
                "b" * 64,
                "c" * 64,
            )
            if (path, digest) == (Path("/tmp/candidate.json"), "d" * 64)
            else (_ for _ in ()).throw(AssertionError("unexpected receipt"))
        ),
    )
    assert code == ExitCode.OK
    assert json.loads(stdout.getvalue()) == {
        "event": "firmware_valid",
        "candidate_id": "candidate-1",
        "release_identity": "release-1",
        "receipt_sha256": "d" * 64,
        "itb_sha256": "a" * 64,
        "dfu_sha256": "b" * 64,
        "frm_sha256": "c" * 64,
    }

    stderr = StringIO()
    code = main(
        [
            "verify-firmware",
            "--receipt",
            "/tmp/private.json",
            "--confirm-receipt-sha256",
            "0" * 64,
        ],
        stdout=StringIO(),
        stderr=stderr,
        firmware_verifier=lambda _path, _digest: (_ for _ in ()).throw(
            RuntimeError("private artifact path")
        ),
    )
    assert code == ExitCode.FIRMWARE_INVALID
    assert json.loads(stderr.getvalue()) == {"event": "firmware_invalid"}
    assert "private artifact path" not in stderr.getvalue()


def test_exclusive_mode_lock_rejects_contention_and_can_be_reacquired(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "pipeline-mode.lock").resolve()
    first = ExclusiveModeLock(path)
    second = ExclusiveModeLock(path)
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="mode is already owned"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_validate_runtime_is_explicit_machine_readable_and_radio_free() -> None:
    stdout = StringIO()
    seen: list[V5CaptureStation] = []

    def verify(station: V5CaptureStation) -> ObservedV5Runtime:
        seen.append(station)
        return _observed_runtime(station)

    code = main(
        ["validate-runtime"],
        stdout=stdout,
        stderr=StringIO(),
        cycle_builder=_forbidden_builder,
        credential_provider_factory=lambda _path: _ForbiddenCredentials(),
        runtime_verifier=verify,
    )
    assert code == ExitCode.OK
    assert seen == [DEVELOPMENT_STATION]
    payload = json.loads(stdout.getvalue())
    assert payload["event"] == "runtime_valid"
    assert payload["runtime_id"] == DEVELOPMENT_STATION.expected_runtime.runtime_id
    assert payload["runtime_manifest_digest"] == str(
        DEVELOPMENT_STATION.runtime_manifest_digest
    )


def test_validate_runtime_sanitizes_failure_without_credentials_or_cycle() -> None:
    stderr = StringIO()

    def fail(_station: V5CaptureStation) -> ObservedV5Runtime:
        raise RuntimeError("private runtime detail")

    code = main(
        ["validate-runtime"],
        stdout=StringIO(),
        stderr=stderr,
        cycle_builder=_forbidden_builder,
        credential_provider_factory=lambda _path: _ForbiddenCredentials(),
        runtime_verifier=fail,
    )
    assert code == ExitCode.RUNTIME_INVALID
    assert json.loads(stderr.getvalue())["event"] == "runtime_invalid"
    assert "private runtime detail" not in stderr.getvalue()


def test_capture_rejects_wrong_serial_before_credentials_or_radio() -> None:
    stderr = StringIO()
    code = main(
        [
            "capture",
            "--arm",
            "--confirm-radio-serial",
            "wrong-radio",
            "--confirm-plan-digest",
            str(DEVELOPMENT_STATION.plan.plan_digest),
            "--credential-directory",
            "/run/credentials/test",
        ],
        stdout=StringIO(),
        stderr=stderr,
        cycle_builder=_forbidden_builder,
        credential_provider_factory=lambda _path: _ForbiddenCredentials(),
    )
    assert code == ExitCode.ARM_REJECTED
    assert json.loads(stderr.getvalue())["event"] == "capture_arm_rejected"


def test_explicitly_armed_capture_reads_named_credential_and_closes_cycle() -> None:
    cycle = _Cycle()
    mode_lock = _ModeLock(DEVELOPMENT_STATION.state.mode_lock_path)
    drain_gate = _DrainGate()

    def build(station: V5CaptureStation, dsn: str) -> _Cycle:
        assert station is DEVELOPMENT_STATION
        assert dsn == "secret-dsn-not-for-output"
        return cycle

    stdout = StringIO()
    code = main(
        [
            "capture",
            "--arm",
            "--confirm-radio-serial",
            DEVELOPMENT_STATION.radio.expected_serial,
            "--confirm-plan-digest",
            str(DEVELOPMENT_STATION.plan.plan_digest),
            "--credential-directory",
            "/run/credentials/test",
        ],
        stdout=stdout,
        stderr=StringIO(),
        cycle_builder=build,
        credential_provider_factory=_FakeCredentials,
        mode_lock_factory=lambda _path: mode_lock,
        drain_gate_builder=lambda dsn: (
            drain_gate
            if dsn == "secret-dsn-not-for-output"
            else (_ for _ in ()).throw(AssertionError("unexpected DSN"))
        ),
    )
    assert code == ExitCode.OK
    assert cycle.calls == ["preflight", "capture", ("close", 10.0)]
    assert mode_lock.calls == ["acquire", "release"]
    assert drain_gate.calls == 1
    assert json.loads(stdout.getvalue())["forward_progress"] is True
    assert "secret-dsn-not-for-output" not in stdout.getvalue()


def test_durable_replay_is_reported_without_requesting_recapture() -> None:
    cycle = _Cycle(progressed=False)
    mode_lock = _ModeLock(DEVELOPMENT_STATION.state.mode_lock_path)
    stdout = StringIO()
    code = main(
        [
            "capture",
            "--arm",
            "--confirm-radio-serial",
            DEVELOPMENT_STATION.radio.expected_serial,
            "--confirm-plan-digest",
            str(DEVELOPMENT_STATION.plan.plan_digest),
            "--credential-directory",
            "/run/credentials/test",
        ],
        stdout=stdout,
        stderr=StringIO(),
        cycle_builder=lambda _station, _dsn: cycle,
        credential_provider_factory=_FakeCredentials,
        mode_lock_factory=lambda _path: mode_lock,
        drain_gate_builder=lambda _dsn: _DrainGate(),
    )
    assert code == ExitCode.OK
    assert cycle.calls == ["preflight", "capture", ("close", 10.0)]
    assert mode_lock.calls == ["acquire", "release"]
    assert json.loads(stdout.getvalue())["forward_progress"] is False


def test_mode_lock_contention_rejects_before_credentials_or_cycle() -> None:
    mode_lock = _ModeLock(DEVELOPMENT_STATION.state.mode_lock_path, fail=True)
    stderr = StringIO()
    code = main(
        [
            "capture",
            "--arm",
            "--confirm-radio-serial",
            DEVELOPMENT_STATION.radio.expected_serial,
            "--confirm-plan-digest",
            str(DEVELOPMENT_STATION.plan.plan_digest),
            "--credential-directory",
            "/run/credentials/test",
        ],
        stdout=StringIO(),
        stderr=stderr,
        cycle_builder=_forbidden_builder,
        credential_provider_factory=lambda _path: _ForbiddenCredentials(),
        mode_lock_factory=lambda _path: mode_lock,
    )
    assert code == ExitCode.CAPTURE_FAILED
    assert mode_lock.calls == ["acquire", "release"]
    assert json.loads(stderr.getvalue())["event"] == "capture_failed"


def test_mode_lock_is_released_when_cycle_preflight_fails() -> None:
    mode_lock = _ModeLock(DEVELOPMENT_STATION.state.mode_lock_path)
    cycle = _Cycle()

    def fail_preflight() -> None:
        cycle.calls.append("preflight")
        raise RuntimeError("failure")

    cycle.preflight = fail_preflight  # type: ignore[method-assign]
    code = main(
        [
            "capture",
            "--arm",
            "--confirm-radio-serial",
            DEVELOPMENT_STATION.radio.expected_serial,
            "--confirm-plan-digest",
            str(DEVELOPMENT_STATION.plan.plan_digest),
            "--credential-directory",
            "/run/credentials/test",
        ],
        stdout=StringIO(),
        stderr=StringIO(),
        cycle_builder=lambda _station, _dsn: cycle,
        credential_provider_factory=_FakeCredentials,
        mode_lock_factory=lambda _path: mode_lock,
        drain_gate_builder=lambda _dsn: _DrainGate(),
    )
    assert code == ExitCode.CAPTURE_FAILED
    assert cycle.calls == ["preflight", ("close", 10.0)]
    assert mode_lock.calls == ["acquire", "release"]


def test_undrained_analysis_blocks_single_radio_construction() -> None:
    mode_lock = _ModeLock(DEVELOPMENT_STATION.state.mode_lock_path)
    drain_gate = _DrainGate(ready=False)
    stderr = StringIO()
    code = main(
        [
            "capture",
            "--arm",
            "--confirm-radio-serial",
            DEVELOPMENT_STATION.radio.expected_serial,
            "--confirm-plan-digest",
            str(DEVELOPMENT_STATION.plan.plan_digest),
            "--credential-directory",
            "/run/credentials/test",
        ],
        stdout=StringIO(),
        stderr=stderr,
        cycle_builder=_forbidden_builder,
        credential_provider_factory=_FakeCredentials,
        mode_lock_factory=lambda _path: mode_lock,
        drain_gate_builder=lambda _dsn: drain_gate,
    )
    assert code == ExitCode.CAPTURE_FAILED
    assert json.loads(stderr.getvalue())["event"] == "capture_admission_blocked"
    assert drain_gate.calls == 1
    assert mode_lock.calls == ["acquire", "release"]


def test_admission_database_error_fails_closed_without_radio_or_secret() -> None:
    mode_lock = _ModeLock(DEVELOPMENT_STATION.state.mode_lock_path)
    stderr = StringIO()
    code = main(
        [
            "capture",
            "--arm",
            "--confirm-radio-serial",
            DEVELOPMENT_STATION.radio.expected_serial,
            "--confirm-plan-digest",
            str(DEVELOPMENT_STATION.plan.plan_digest),
            "--credential-directory",
            "/run/credentials/test",
        ],
        stdout=StringIO(),
        stderr=stderr,
        cycle_builder=_forbidden_builder,
        credential_provider_factory=_FakeCredentials,
        mode_lock_factory=lambda _path: mode_lock,
        drain_gate_builder=lambda _dsn: _DrainGate(fail=True),
    )
    assert code == ExitCode.CAPTURE_FAILED
    assert json.loads(stderr.getvalue())["event"] == "capture_admission_blocked"
    assert "private database failure" not in stderr.getvalue()
    assert "secret-dsn-not-for-output" not in stderr.getvalue()
    assert mode_lock.calls == ["acquire", "release"]


def test_lock_cleanup_failure_takes_precedence_over_admission_classification() -> None:
    mode_lock = _ModeLock(DEVELOPMENT_STATION.state.mode_lock_path, release_fail=True)
    stderr = StringIO()
    code = main(
        [
            "capture",
            "--arm",
            "--confirm-radio-serial",
            DEVELOPMENT_STATION.radio.expected_serial,
            "--confirm-plan-digest",
            str(DEVELOPMENT_STATION.plan.plan_digest),
            "--credential-directory",
            "/run/credentials/test",
        ],
        stdout=StringIO(),
        stderr=stderr,
        cycle_builder=_forbidden_builder,
        credential_provider_factory=_FakeCredentials,
        mode_lock_factory=lambda _path: mode_lock,
        drain_gate_builder=lambda _dsn: _DrainGate(ready=False),
    )
    assert code == ExitCode.CAPTURE_FAILED
    assert json.loads(stderr.getvalue())["event"] == "capture_failed"
    assert "private lock cleanup failure" not in stderr.getvalue()
