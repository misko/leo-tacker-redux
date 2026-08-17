"""Machine-readable operator CLI for one exact V5 station scan."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from enum import IntEnum
from pathlib import Path
from typing import Protocol, TextIO

from leo_flow.adapters.systemd_credentials import SystemdCredentialProvider
from leo_flow.application.capture_admission import CaptureAdmissionGate
from leo_flow.capture.drivers.v5_observers import observe_current_v5_runtime
from leo_flow.capture.drivers.v5_preflight import (
    ObservedV5Runtime,
    attest_v5_host,
)
from leo_flow.capture.serialization import encode_plan
from leo_flow.capture.v5_station import (
    V5CaptureStation,
    V5StationConfigurationError,
    load_v5_capture_station,
)
from leo_flow.deployments.process_mode_lock import ExclusiveModeLock
from leo_flow.deployments.v5_radio_firmware import (
    VerifiedV5RadioFirmware,
    verify_v5_radio_firmware_receipt,
)
from leo_flow.deployments.v5_scan import (
    DEVELOPMENT_STATION,
    build_station_capture_cycle,
)

CATALOG_CREDENTIAL_NAME = "catalog-dsn"


class ExitCode(IntEnum):
    OK = 0
    USAGE_OR_CONFIG = 2
    ARM_REJECTED = 3
    CAPTURE_FAILED = 4
    RUNTIME_INVALID = 5
    FIRMWARE_INVALID = 6


class _CaptureCycle(Protocol):
    def preflight(self) -> None: ...

    def capture_and_publish_once(self) -> bool: ...

    def close(self, timeout_s: float) -> None: ...


class _CredentialProvider(Protocol):
    def resolve(self, name: str) -> str: ...


class _ModeLock(Protocol):
    def acquire(self) -> None: ...

    def release(self) -> None: ...


CycleBuilder = Callable[[V5CaptureStation, str], _CaptureCycle]
CredentialProviderFactory = Callable[[Path], _CredentialProvider]
RuntimeVerifier = Callable[[V5CaptureStation], ObservedV5Runtime]
ModeLockFactory = Callable[[Path], _ModeLock]
DrainGateBuilder = Callable[[str], CaptureAdmissionGate]
FirmwareVerifier = Callable[[Path, str], VerifiedV5RadioFirmware]


class _CaptureAdmissionBlocked(RuntimeError):
    pass


def _postgres_drain_gate(dsn: str) -> CaptureAdmissionGate:
    """Load the optional PostgreSQL adapter only for an armed live capture."""

    from leo_flow.adapters.capture_analysis_drain_postgres import (
        PostgresCaptureAnalysisDrainGate,
    )

    return PostgresCaptureAnalysisDrainGate(dsn)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="leo-v5-capture",
        description="Inspect or run one immutable station-bound V5 scan.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (
        ("validate", "validate a station and its exact plan without radio contact"),
        (
            "validate-runtime",
            "attest manifest bytes and this process runtime without radio contact",
        ),
        ("show-plan", "print the exact plan without radio contact"),
    ):
        child = subparsers.add_parser(command, help=help_text)
        child.add_argument(
            "--station",
            type=Path,
            help="explicit station JSON (default: checked development radio .15)",
        )

    firmware = subparsers.add_parser(
        "verify-firmware",
        help="verify a radio firmware receipt and exact files without radio contact",
    )
    firmware.add_argument("--receipt", type=Path, required=True)
    firmware.add_argument("--confirm-receipt-sha256", required=True)

    capture = subparsers.add_parser(
        "capture", help="explicitly arm and execute one restart-safe capture cycle"
    )
    capture.add_argument(
        "--station",
        type=Path,
        help="explicit station JSON (default: checked development radio .15)",
    )
    capture.add_argument(
        "--arm",
        action="store_true",
        help="required acknowledgement that this command can contact a radio",
    )
    capture.add_argument("--confirm-radio-serial", required=True)
    capture.add_argument("--confirm-plan-digest", required=True)
    capture.add_argument(
        "--credential-directory",
        type=Path,
        required=True,
        help="directory containing the catalog-dsn credential file",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    cycle_builder: CycleBuilder = build_station_capture_cycle,
    credential_provider_factory: CredentialProviderFactory = SystemdCredentialProvider,
    runtime_verifier: RuntimeVerifier | None = None,
    mode_lock_factory: ModeLockFactory = ExclusiveModeLock,
    drain_gate_builder: DrainGateBuilder = _postgres_drain_gate,
    firmware_verifier: FirmwareVerifier = verify_v5_radio_firmware_receipt,
) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "verify-firmware":
        try:
            verified = firmware_verifier(
                arguments.receipt, arguments.confirm_receipt_sha256
            )
        except Exception:  # noqa: BLE001 - sanitize local artifact paths
            _emit(stderr, {"event": "firmware_invalid"})
            return ExitCode.FIRMWARE_INVALID
        _emit(
            stdout,
            {
                "event": "firmware_valid",
                "candidate_id": verified.candidate_id,
                "release_identity": verified.release_identity,
                "receipt_sha256": verified.receipt_sha256,
                "itb_sha256": verified.itb_sha256,
                "dfu_sha256": verified.dfu_sha256,
                "frm_sha256": verified.frm_sha256,
            },
        )
        return ExitCode.OK
    try:
        station = (
            DEVELOPMENT_STATION
            if arguments.station is None
            else load_v5_capture_station(arguments.station)
        )
    except V5StationConfigurationError:
        _emit(stderr, {"event": "station_configuration_error"})
        return ExitCode.USAGE_OR_CONFIG

    if arguments.command == "validate":
        _emit(stdout, _station_summary(station, event="station_valid"))
        return ExitCode.OK
    if arguments.command == "validate-runtime":
        try:
            observed = (runtime_verifier or _verify_station_runtime)(station)
        except Exception:  # noqa: BLE001 - sanitize exact host paths at CLI boundary
            _emit(
                stderr,
                {
                    "event": "runtime_invalid",
                    "runtime_manifest_digest": str(station.runtime_manifest_digest),
                },
            )
            return ExitCode.RUNTIME_INVALID
        payload = _station_summary(station, event="runtime_valid")
        payload.update(
            {
                "runtime_id": observed.runtime_id,
                "iio_module_path": observed.iio_module_path,
                "native_libiio_paths": list(observed.native_libiio_paths),
                "pyadi_module_path": observed.pyadi_module_path,
                "spf_module_path": observed.spf_module_path,
            }
        )
        _emit(stdout, payload)
        return ExitCode.OK
    if arguments.command == "show-plan":
        payload = _station_summary(station, event="plan_valid")
        payload["plan"] = json.loads(encode_plan(station.capture_plan()))
        _emit(stdout, payload)
        return ExitCode.OK

    if arguments.command != "capture":  # pragma: no cover - argparse owns choices
        _emit(stderr, {"event": "usage_error"})
        return ExitCode.USAGE_OR_CONFIG
    if (
        not arguments.arm
        or arguments.confirm_radio_serial != station.radio.expected_serial
        or arguments.confirm_plan_digest != str(station.plan.plan_digest)
    ):
        _emit(
            stderr,
            {
                "event": "capture_arm_rejected",
                "radio_id": str(station.radio.radio_id),
                "plan_id": str(station.plan.plan_id),
            },
        )
        return ExitCode.ARM_REJECTED

    cycle: _CaptureCycle | None = None
    mode_lock: _ModeLock | None = None
    progressed = False
    failed = False
    admission_blocked = False
    try:
        mode_lock = mode_lock_factory(station.state.mode_lock_path)
        mode_lock.acquire()
        credential = credential_provider_factory(
            arguments.credential_directory
        ).resolve(CATALOG_CREDENTIAL_NAME)
        try:
            admitted = drain_gate_builder(credential).ready()
        except Exception as error:
            raise _CaptureAdmissionBlocked from error
        if not admitted:
            raise _CaptureAdmissionBlocked
        cycle = cycle_builder(station, credential)
        cycle.preflight()
        progressed = cycle.capture_and_publish_once()
    except _CaptureAdmissionBlocked:
        admission_blocked = True
    except Exception:  # noqa: BLE001 - sanitize the operator process boundary
        failed = True
    finally:
        if cycle is not None:
            try:
                cycle.close(10.0)
            except Exception:  # noqa: BLE001 - report a sanitized close failure below
                failed = True
        if mode_lock is not None:
            try:
                mode_lock.release()
            except Exception:  # noqa: BLE001 - report sanitized lock cleanup below
                failed = True

    if failed:
        _emit(
            stderr,
            {
                "event": "capture_failed",
                "radio_id": str(station.radio.radio_id),
                "plan_id": str(station.plan.plan_id),
            },
        )
        return ExitCode.CAPTURE_FAILED
    if admission_blocked:
        _emit(
            stderr,
            {
                "event": "capture_admission_blocked",
                "radio_id": str(station.radio.radio_id),
                "plan_id": str(station.plan.plan_id),
            },
        )
        return ExitCode.CAPTURE_FAILED

    payload = _station_summary(station, event="capture_cycle_complete")
    payload["forward_progress"] = progressed
    _emit(stdout, payload)
    return ExitCode.OK


def _station_summary(station: V5CaptureStation, *, event: str) -> dict[str, object]:
    return {
        "event": event,
        "station_spec_digest": str(station.specification_digest),
        "station_id": str(station.station_id),
        "radio_uri": station.radio.uri,
        "radio_serial": station.radio.expected_serial,
        "radio_id": str(station.radio.radio_id),
        "receiver_chain_ids": [str(item) for item in station.radio.receiver_chain_ids],
        "hardware_snapshot_id": str(station.hardware_snapshot_id),
        "plan_id": str(station.plan.plan_id),
        "plan_digest": str(station.plan.plan_digest),
        "state_root": str(station.state.state_root),
        "spool_database": str(station.state.spool_database),
        "lock_path": str(station.state.lock_path),
        "mode_lock_path": str(station.state.mode_lock_path),
        "runtime_manifest": str(station.runtime_manifest),
        "runtime_manifest_digest": str(station.runtime_manifest_digest),
    }


def _verify_station_runtime(station: V5CaptureStation) -> ObservedV5Runtime:
    observed = observe_current_v5_runtime(
        station.runtime_manifest,
        expected_manifest_digest=station.runtime_manifest_digest,
    )
    attest_v5_host(
        uri=station.radio.uri,
        expected_runtime=station.expected_runtime,
        observed_runtime=observed,
    )
    return observed


def _emit(stream: TextIO, payload: dict[str, object]) -> None:
    stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    stream.flush()


if __name__ == "__main__":  # pragma: no cover - exercised through main
    raise SystemExit(main())
