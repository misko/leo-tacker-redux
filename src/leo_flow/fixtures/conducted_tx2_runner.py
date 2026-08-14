"""Supervised one-shot runner for the conducted V5 TX2 fixture.

Dry-run is the default and never opens a radio.  A transmitting invocation
must consume the immutable receipt from a prior dry-run of the exact plan and
repeat both the radio identity and antenna-free topology confirmation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import struct
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, TextIO, cast

from leo_flow.capture.drivers.v5_preflight import ExpectedV5Radio, ExpectedV5Runtime

from .conducted_tx2 import (
    CONDUCTED_CONFIRMATION,
    ConductedFixtureAttestation,
    ConductedTx2Plan,
    FiniteTx2Waveform,
    Tx2CleanupError,
    Tx2Device,
    Tx2DeviceFactory,
    Tx2LadderStep,
    open_exact_pyadi_tx2,
    preflight_conducted_tx2,
    run_conducted_tx2_ladder,
    validate_conducted_tx2_plan,
)

CONFIG_SCHEMA = "leo-flow.conducted-tx2-runner-config/v1"
RECEIPT_SCHEMA = "leo-flow.conducted-tx2-runner-receipt/v1"
FIXTURE_ID = "lower-edge-inner-pilot-pair-unmodulated/v1"
FIXTURE_SAMPLE_COUNT = 4_096
FIXTURE_RMS_COUNTS = 16
FIXTURE_TX_ATTENUATION_DB = 80
INNER_PILOT_OFFSET_HZ = 117_187.5
QUALIFIED_URI = "ip:192.168.1.15"
QUALIFIED_RUNTIME = ExpectedV5Runtime(
    runtime_id="pluto-v5-libiio-0.25-spfmeta3",
    schema="leo-flow.v5-runtime/v1",
    iio_module_path="/usr/local/lib/python3.11/dist-packages/iio.py",
    iio_version=(0, 25, "c26258b"),
    iio_commit="c26258bfa33098c2b215e19cf85d448e89499b1a",
    native_libiio_prefix="/opt/leo-v5",
    required_backends=frozenset(("local", "ip", "usb")),
    pyadi_version="0.0.21",
    pyadi_module_path="/usr/local/lib/python3.11/dist-packages/adi/__init__.py",
    spf_module_path=(
        "/usr/local/lib/python3.11/dist-packages/spf/direct_radio/iio_metadata.py"
    ),
    spf_revision="c40ee4116546889effd72056115adaaa1bc3fd40",
    spf_import="spf.direct_radio.iio_metadata:IioMetadataRx",
    metadata_protocol="spf-radio-metadata-v3",
)
QUALIFIED_RADIO = ExpectedV5Radio(
    serial="104000b29905000e17000800065934759d",
    firmware_release="v0.38-plutoplus-spf-libiio-metadata-v5",
    firmware_commit="d7c87a9a28094ee6f0b23cb47df9ff737b5a69d8",
)


class ConductedTx2RunnerError(RuntimeError):
    """A runner configuration, arming, or receipt gate failed closed."""


@dataclass(frozen=True, slots=True)
class PreparedOneShot:
    plan: ConductedTx2Plan
    plan_sha256: str
    fixture_id: str


@dataclass(slots=True)
class _AttemptState:
    radio_contact_attempted: bool = False
    context_opened: bool = False
    transmission_attempted: bool = False


class _TracingTx2Device:
    """Transparent narrow-port wrapper used only for truthful receipts."""

    def __init__(self, device: Tx2Device, state: _AttemptState) -> None:
        self._device = device
        self._state = state

    def read_serial(self) -> str:
        return self._device.read_serial()

    def attest_qualified_v5(
        self,
        uri: str,
        expected_runtime: ExpectedV5Runtime,
        expected_radio: ExpectedV5Radio,
    ) -> None:
        self._device.attest_qualified_v5(uri, expected_runtime, expected_radio)

    def destroy_tx_buffer(self) -> None:
        self._device.destroy_tx_buffer()

    def disable_tx2_dds(self) -> None:
        self._device.disable_tx2_dds()

    def read_tx2_dds_scales(self) -> Mapping[str, float]:
        return self._device.read_tx2_dds_scales()

    def set_tx2_gain_db(self, value: float) -> None:
        self._device.set_tx2_gain_db(value)

    def read_tx2_gain_db(self) -> float:
        return self._device.read_tx2_gain_db()

    def set_tx2_lo_hz(self, value: int) -> None:
        self._device.set_tx2_lo_hz(value)

    def read_tx2_lo_hz(self) -> float:
        return self._device.read_tx2_lo_hz()

    def set_sample_rate_hz(self, value: int) -> None:
        self._device.set_sample_rate_hz(value)

    def read_sample_rate_hz(self) -> float:
        return self._device.read_sample_rate_hz()

    def transmit_tx2_finite_ci16(self, value: bytes) -> None:
        self._state.transmission_attempted = True
        self._device.transmit_tx2_finite_ci16(value)

    def close(self) -> None:
        self._device.close()


def deterministic_two_pilot_waveform(sample_rate_hz: int) -> FiniteTx2Waveform:
    """Return a bounded pair at the inner lower-edge pilot-bin offsets.

    This is deliberately only a spectral smoke-test fixture.  It does not
    claim the published pilot coding, framing, or complete Starlink waveform.
    """

    component_amplitude = FIXTURE_RMS_COUNTS / math.sqrt(2.0)
    output = bytearray()
    for sample_index in range(FIXTURE_SAMPLE_COUNT):
        phase = 2.0 * math.pi * INNER_PILOT_OFFSET_HZ * sample_index / sample_rate_hz
        # Two conjugate tones. Their sum is real and has complex RMS 16 counts.
        i_value = round(2.0 * component_amplitude * math.cos(phase))
        output.extend(struct.pack("<hh", i_value, 0))
    return FiniteTx2Waveform.from_ci16(
        bytes(output), declared_rms_counts=FIXTURE_RMS_COUNTS
    )


def load_one_shot_config(path: Path) -> PreparedOneShot:
    """Load one strict config and build its deterministic one-step plan."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        root = _mapping(document, "config")
        _exact_keys(
            root,
            {
                "schema",
                "topology",
                "tx_lo_hz",
                "sample_rate_hz",
            },
            "config",
        )
        if _string(root["schema"], "schema") != CONFIG_SCHEMA:
            raise ValueError("unsupported runner config schema")
        topology = _topology(_mapping(root["topology"], "topology"))
        sample_rate_hz = _integer(root["sample_rate_hz"], "sample_rate_hz")
        waveform = deterministic_two_pilot_waveform(sample_rate_hz)
        plan = ConductedTx2Plan(
            uri=QUALIFIED_URI,
            expected_radio_serial=QUALIFIED_RADIO.serial,
            armed_radio_serial=QUALIFIED_RADIO.serial,
            topology=topology,
            expected_runtime=QUALIFIED_RUNTIME,
            expected_radio=QUALIFIED_RADIO,
            tx_lo_hz=_integer(root["tx_lo_hz"], "tx_lo_hz"),
            sample_rate_hz=sample_rate_hz,
            steps=(Tx2LadderStep(FIXTURE_TX_ATTENUATION_DB, waveform),),
        )
        validate_conducted_tx2_plan(plan)
    except ConductedTx2RunnerError:
        raise
    except Exception as error:
        raise ConductedTx2RunnerError(
            f"invalid conducted TX2 config: {type(error).__name__}"
        ) from error
    plan_sha256 = hashlib.sha256(_canonical_json(_plan_document(plan))).hexdigest()
    return PreparedOneShot(plan, plan_sha256, FIXTURE_ID)


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    device_factory: Tx2DeviceFactory = open_exact_pyadi_tx2,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    """Run one dry-run, read-only preflight, or explicitly armed operation."""

    parser = _parser()
    args = parser.parse_args(argv)
    try:
        prepared = load_one_shot_config(args.config)
        _require_fresh_receipt(args.receipt)
        if args.arm:
            _require_arm(args, prepared)
            return _run_armed(prepared, args.receipt, device_factory, stdout)
        if args.preflight:
            evidence = preflight_conducted_tx2(prepared.plan, device_factory)
            receipt = _base_receipt(prepared, "read_only_preflight")
            receipt.update(
                {
                    "status": "pass",
                    "radio_contacted": True,
                    "transmission_attempted": False,
                    "radio_serial": evidence.radio_serial,
                    "cleanup": {"status": "context_closed"},
                }
            )
        else:
            receipt = _base_receipt(prepared, "dry_run")
            receipt.update(
                {
                    "status": "pass",
                    "radio_contacted": False,
                    "transmission_attempted": False,
                    "cleanup": {"status": "not_applicable"},
                }
            )
        _write_receipt(args.receipt, receipt)
        _emit(stdout, receipt)
        return 0
    except Exception as error:  # noqa: BLE001 - CLI emits bounded failure details
        _emit(
            stderr,
            {
                "schema": RECEIPT_SCHEMA,
                "status": "fail",
                "error_type": type(error).__name__,
            },
        )
        return 1


def _run_armed(
    prepared: PreparedOneShot,
    receipt_path: Path,
    device_factory: Tx2DeviceFactory,
    stdout: TextIO,
) -> int:
    state = _AttemptState()

    def traced_factory(uri: str) -> Tx2Device:
        state.radio_contact_attempted = True
        device = device_factory(uri)
        state.context_opened = True
        return _TracingTx2Device(device, state)

    receipt = _base_receipt(prepared, "armed_one_shot")
    receipt.update(
        {
            "transmission_authorized": True,
        }
    )
    try:
        evidence = run_conducted_tx2_ladder(prepared.plan, traced_factory)
    except Exception as error:  # noqa: BLE001 - retain an immutable failure receipt
        receipt.update(
            {
                "status": "fail",
                "error_type": type(error).__name__,
                "radio_contact_attempted": state.radio_contact_attempted,
                "context_opened": state.context_opened,
                "transmission_attempted": state.transmission_attempted,
                "cleanup": {
                    "status": (
                        "failed"
                        if isinstance(error, Tx2CleanupError)
                        else "adapter_completed_without_cleanup_error"
                    )
                },
            }
        )
        exit_code = 1
    else:
        receipt.update(
            {
                "status": "pass",
                "radio_contact_attempted": state.radio_contact_attempted,
                "context_opened": state.context_opened,
                "transmission_attempted": state.transmission_attempted,
                "radio_serial": evidence.radio_serial,
                "step_evidence": [asdict(step) for step in evidence.steps],
                "cleanup": {
                    "status": "verified_muted",
                    "tx_buffer_destroyed": True,
                    "tx2_dds_disabled": True,
                    "tx2_gain_db": -80.0,
                    "context_closed": True,
                },
            }
        )
        exit_code = 0
    _write_receipt(receipt_path, receipt)
    _emit(stdout, receipt)
    return exit_code


def _require_arm(args: argparse.Namespace, prepared: PreparedOneShot) -> None:
    if args.confirm_radio_serial != prepared.plan.expected_radio_serial:
        raise ConductedTx2RunnerError(
            "armed run requires the exact expected radio serial confirmation"
        )
    if args.confirm_conducted_topology != CONDUCTED_CONFIRMATION:
        raise ConductedTx2RunnerError(
            "armed run requires the exact current antenna-free topology confirmation"
        )
    if args.arm_from_dry_run is None:
        raise ConductedTx2RunnerError("armed run requires a prior dry-run receipt")
    try:
        prior = _mapping(
            json.loads(args.arm_from_dry_run.read_text(encoding="utf-8")),
            "dry-run receipt",
        )
    except Exception as error:
        raise ConductedTx2RunnerError("cannot read prior dry-run receipt") from error
    required = {
        "schema": RECEIPT_SCHEMA,
        "mode": "dry_run",
        "status": "pass",
        "plan_sha256": prepared.plan_sha256,
        "radio_contacted": False,
        "transmission_attempted": False,
    }
    if any(prior.get(key) != value for key, value in required.items()):
        raise ConductedTx2RunnerError(
            "prior dry-run receipt does not authorize this exact immutable plan"
        )


def _base_receipt(prepared: PreparedOneShot, mode: str) -> dict[str, Any]:
    waveform = prepared.plan.steps[0].waveform
    return {
        "schema": RECEIPT_SCHEMA,
        "mode": mode,
        "plan_sha256": prepared.plan_sha256,
        "uri": prepared.plan.uri,
        "expected_radio_serial": prepared.plan.expected_radio_serial,
        "fixture": {
            "fixture_id": prepared.fixture_id,
            "scope": "two unmodulated tones at inner lower-edge pilot-bin offsets",
            "sample_count": FIXTURE_SAMPLE_COUNT,
            "sample_rate_hz": prepared.plan.sample_rate_hz,
            "duration_ns": round(
                FIXTURE_SAMPLE_COUNT * 1_000_000_000 / prepared.plan.sample_rate_hz
            ),
            "declared_rms_counts": waveform.declared_rms_counts,
            "ci16_sha256": waveform.sha256,
            "tx_attenuation_db": FIXTURE_TX_ATTENUATION_DB,
            "pilot_offsets_hz": [-INNER_PILOT_OFFSET_HZ, INNER_PILOT_OFFSET_HZ],
        },
    }


def _plan_document(plan: ConductedTx2Plan) -> dict[str, Any]:
    runtime = asdict(plan.expected_runtime)
    runtime["required_backends"] = sorted(plan.expected_runtime.required_backends)
    radio = asdict(plan.expected_radio)
    return {
        "uri": plan.uri,
        "expected_radio_serial": plan.expected_radio_serial,
        "topology": asdict(plan.topology),
        "expected_runtime": runtime,
        "expected_radio": radio,
        "tx_lo_hz": plan.tx_lo_hz,
        "sample_rate_hz": plan.sample_rate_hz,
        "steps": [
            {
                "tx_attenuation_db": step.tx_attenuation_db,
                "waveform_sha256": step.waveform.sha256,
                "declared_rms_counts": step.waveform.declared_rms_counts,
                "sample_count": len(step.waveform.ci16_le) // 4,
            }
            for step in plan.steps
        ],
    }


def _topology(value: Mapping[str, object]) -> ConductedFixtureAttestation:
    keys = {
        "radio_serial",
        "topology",
        "splitter_id",
        "tx2_to_rx1_attenuator_ids",
        "tx2_to_rx2_attenuator_ids",
        "tx2_to_rx1_attenuation_db",
        "tx2_to_rx2_attenuation_db",
        "confirmation",
    }
    _exact_keys(value, keys, "topology")
    rx1_ids = _sequence(value["tx2_to_rx1_attenuator_ids"], "RX1 attenuators")
    rx2_ids = _sequence(value["tx2_to_rx2_attenuator_ids"], "RX2 attenuators")
    return ConductedFixtureAttestation(
        radio_serial=_string(value["radio_serial"], "topology radio serial"),
        topology=_string(value["topology"], "topology"),
        splitter_id=_string(value["splitter_id"], "splitter_id"),
        tx2_to_rx1_attenuator_ids=tuple(
            _string(item, "RX1 attenuator") for item in rx1_ids
        ),
        tx2_to_rx2_attenuator_ids=tuple(
            _string(item, "RX2 attenuator") for item in rx2_ids
        ),
        tx2_to_rx1_attenuation_db=_number(
            value["tx2_to_rx1_attenuation_db"], "RX1 attenuation"
        ),
        tx2_to_rx2_attenuation_db=_number(
            value["tx2_to_rx2_attenuation_db"], "RX2 attenuation"
        ),
        confirmation=_string(value["confirmation"], "topology confirmation"),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dry-run or supervise one bounded conducted V5 TX2 operation"
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--receipt",
        type=Path,
        required=True,
        help="new immutable JSON receipt path; an existing path is rejected",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--preflight", action="store_true", help="read-only live V5 attestation"
    )
    mode.add_argument("--arm", action="store_true", help="arm one finite TX2 send")
    parser.add_argument("--arm-from-dry-run", type=Path)
    parser.add_argument("--confirm-radio-serial")
    parser.add_argument("--confirm-conducted-topology")
    return parser


def _require_fresh_receipt(path: Path) -> None:
    if not path.is_absolute():
        raise ConductedTx2RunnerError("receipt path must be absolute")
    if path.exists():
        raise ConductedTx2RunnerError("receipt path already exists")
    if not path.parent.is_dir():
        raise ConductedTx2RunnerError("receipt parent directory does not exist")


def _write_receipt(path: Path, value: Mapping[str, object]) -> None:
    payload = _canonical_json(value) + b"\n"
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _emit(stream: TextIO, value: Mapping[str, object]) -> None:
    stream.write(_canonical_json(value).decode("utf-8") + "\n")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object with string keys")
    return cast(Mapping[str, object], value)


def _sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise TypeError(f"{label} must be an array")
    return value


def _exact_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{label} fields differ from the strict schema")


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
