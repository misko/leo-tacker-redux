from __future__ import annotations

import json
import time
from io import StringIO
from pathlib import Path

from leo_flow.capture.drivers.v5_preflight import ExpectedV5Radio, ExpectedV5Runtime
from leo_flow.fixtures.conducted_tx2 import (
    CONDUCTED_CONFIRMATION,
    CONDUCTED_TOPOLOGY,
    TX_AUTHORIZATION,
)
from leo_flow.fixtures.conducted_tx2_runner import (
    CONFIG_SCHEMA,
    FIXTURE_SAMPLE_COUNT,
    RECEIPT_SCHEMA,
    deterministic_two_pilot_waveform,
    load_one_shot_config,
    run_cli,
)

SERIAL = "104000b29905000e17000800065934759d"
URI = "ip:192.168.1.15"


def config_document() -> dict[str, object]:
    now = time.time_ns()
    return {
        "schema": CONFIG_SCHEMA,
        "tx_operator_id": "operator-a",
        "authorization_issued_utc_ns": now - 1_000_000_000,
        "authorization_expires_utc_ns": now + 14 * 60 * 1_000_000_000,
        "topology": {
            "radio_serial": SERIAL,
            "topology": CONDUCTED_TOPOLOGY,
            "splitter_id": "tee-bench-01",
            "path_evidence": [
                {
                    "receiver_path": "RX1",
                    "attenuator_ids": ["att-rx1-30db"],
                    "attenuation_db": 30.0,
                    "verified_by": "reviewer-a",
                    "verification_method": "calibrated_vna",
                    "verified_utc_ns": now,
                    "evidence_sha256": "1" * 64,
                },
                {
                    "receiver_path": "RX2",
                    "attenuator_ids": ["att-rx2-30db"],
                    "attenuation_db": 30.0,
                    "verified_by": "reviewer-b",
                    "verification_method": "calibrated_signal_generator_power_meter",
                    "verified_utc_ns": now,
                    "evidence_sha256": "2" * 64,
                },
            ],
            "confirmation": CONDUCTED_CONFIRMATION,
        },
        "tx_lo_hz": 1_709_687_500,
        "sample_rate_hz": 1_250_000,
    }


def write_config(path: Path, document: dict[str, object] | None = None) -> Path:
    path.write_text(json.dumps(document or config_document()), encoding="utf-8")
    return path


class FakeTx2:
    def __init__(self, *, serial: str = SERIAL, fail_transmit: bool = False) -> None:
        self.serial = serial
        self.fail_transmit = fail_transmit
        self.events: list[str] = []
        self.dds = dict.fromkeys(
            ("altvoltage4", "altvoltage5", "altvoltage6", "altvoltage7"), 0.0
        )
        self.gain = -80.0
        self.lo = 0.0
        self.rate = 0.0
        self.transmissions = 0

    def attest_qualified_v5(
        self,
        uri: str,
        expected_runtime: ExpectedV5Runtime,
        expected_radio: ExpectedV5Radio,
    ) -> None:
        del expected_runtime, expected_radio
        assert uri == URI
        self.events.append("attest")

    def read_serial(self) -> str:
        self.events.append("read_serial")
        return self.serial

    def destroy_tx_buffer(self) -> None:
        self.events.append("destroy")

    def disable_tx2_dds(self) -> None:
        self.events.append("disable_dds")
        self.dds = dict.fromkeys(self.dds, 0.0)

    def read_tx2_dds_scales(self) -> dict[str, float]:
        self.events.append("read_dds")
        return dict(self.dds)

    def set_tx2_gain_db(self, value: float) -> None:
        self.events.append("set_gain")
        self.gain = value

    def read_tx2_gain_db(self) -> float:
        self.events.append("read_gain")
        return self.gain

    def set_tx2_lo_hz(self, value: int) -> None:
        self.events.append("set_lo")
        self.lo = float(value)

    def read_tx2_lo_hz(self) -> float:
        self.events.append("read_lo")
        return self.lo

    def set_sample_rate_hz(self, value: int) -> None:
        self.events.append("set_rate")
        self.rate = float(value)

    def read_sample_rate_hz(self) -> float:
        self.events.append("read_rate")
        return self.rate

    def transmit_tx2_finite_ci16(self, value: bytes) -> None:
        assert len(value) == FIXTURE_SAMPLE_COUNT * 4
        self.events.append("transmit")
        self.transmissions += 1
        if self.fail_transmit:
            raise OSError("injected transmit failure")

    def close(self) -> None:
        self.events.append("close")


def receipt(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_fixture_and_plan_identity_are_deterministic(tmp_path: Path) -> None:
    first = deterministic_two_pilot_waveform(1_250_000)
    second = deterministic_two_pilot_waveform(1_250_000)
    assert first == second
    assert len(first.ci16_le) == FIXTURE_SAMPLE_COUNT * 4

    config = write_config(tmp_path / "config.json")
    prepared_a = load_one_shot_config(config)
    prepared_b = load_one_shot_config(config)
    assert prepared_a.plan_sha256 == prepared_b.plan_sha256
    assert prepared_a.plan.steps[0].waveform == first
    assert prepared_a.plan.uri == URI
    assert prepared_a.plan.expected_radio_serial == SERIAL


def test_default_is_offline_dry_run_and_writes_immutable_receipt(
    tmp_path: Path,
) -> None:
    config = write_config(tmp_path / "config.json")
    output = tmp_path / "dry.json"
    opened = False

    def factory(uri: str) -> FakeTx2:
        del uri
        nonlocal opened
        opened = True
        return FakeTx2()

    stdout = StringIO()
    assert (
        run_cli(
            ["--config", str(config), "--receipt", str(output)],
            device_factory=factory,
            stdout=stdout,
        )
        == 0
    )
    assert not opened
    result = receipt(output)
    assert result["schema"] == RECEIPT_SCHEMA
    assert result["mode"] == "dry_run"
    assert result["radio_contacted"] is False
    assert result["transmission_attempted"] is False
    assert json.loads(stdout.getvalue()) == result

    assert (
        run_cli(
            ["--config", str(config), "--receipt", str(output)],
            device_factory=factory,
            stderr=StringIO(),
        )
        == 1
    )
    assert not opened


def test_read_only_preflight_never_calls_a_mutating_port(tmp_path: Path) -> None:
    config = write_config(tmp_path / "config.json")
    output = tmp_path / "preflight.json"
    device = FakeTx2()

    assert (
        run_cli(
            [
                "--config",
                str(config),
                "--receipt",
                str(output),
                "--preflight",
            ],
            device_factory=lambda uri: device,
        )
        == 0
    )
    assert device.events == ["attest", "read_serial", "close"]
    result = receipt(output)
    assert result["mode"] == "read_only_preflight"
    assert result["transmission_attempted"] is False
    assert result["cleanup"] == {"status": "context_closed"}


def test_arm_requires_prior_exact_dry_run_and_both_live_confirmations(
    tmp_path: Path,
) -> None:
    config = write_config(tmp_path / "config.json")
    opened = False

    def factory(uri: str) -> FakeTx2:
        del uri
        nonlocal opened
        opened = True
        return FakeTx2()

    for extra in (
        [],
        ["--confirm-radio-serial", SERIAL],
        [
            "--confirm-radio-serial",
            SERIAL,
            "--confirm-conducted-topology",
            CONDUCTED_CONFIRMATION,
        ],
    ):
        output = tmp_path / f"result-{len(extra)}.json"
        assert (
            run_cli(
                [
                    "--config",
                    str(config),
                    "--receipt",
                    str(output),
                    "--arm",
                    *extra,
                ],
                device_factory=factory,
                stderr=StringIO(),
            )
            == 1
        )
        assert not output.exists()
    assert not opened


def test_exact_dry_receipt_arms_one_finite_send_and_cleanup_receipt(
    tmp_path: Path,
) -> None:
    config = write_config(tmp_path / "config.json")
    dry = tmp_path / "dry.json"
    result = tmp_path / "result.json"
    assert run_cli(["--config", str(config), "--receipt", str(dry)]) == 0
    device = FakeTx2()

    assert (
        run_cli(
            [
                "--config",
                str(config),
                "--receipt",
                str(result),
                "--arm",
                "--arm-from-dry-run",
                str(dry),
                "--confirm-radio-serial",
                SERIAL,
                "--confirm-conducted-topology",
                CONDUCTED_CONFIRMATION,
                "--confirm-operator-id",
                "OPERATOR-A",
                "--authorize-tx",
                TX_AUTHORIZATION,
            ],
            device_factory=lambda uri: device,
        )
        == 0
    )
    assert device.transmissions == 1
    assert device.events[-1] == "close"
    assert device.gain == -80.0
    assert set(device.dds.values()) == {0.0}
    observed = receipt(result)
    assert observed["mode"] == "armed_one_shot"
    assert observed["status"] == "pass"
    assert observed["cleanup"] == {
        "status": "verified_muted",
        "initial_mute": {
            "tx_buffer_destroyed": True,
            "tx_gain_readback_db": -80.0,
            "tx2_dds_scale_readbacks": [
                ["altvoltage4", 0.0],
                ["altvoltage5", 0.0],
                ["altvoltage6", 0.0],
                ["altvoltage7", 0.0],
            ],
        },
        "final_mute": {
            "tx_buffer_destroyed": True,
            "tx_gain_readback_db": -80.0,
            "tx2_dds_scale_readbacks": [
                ["altvoltage4", 0.0],
                ["altvoltage5", 0.0],
                ["altvoltage6", 0.0],
                ["altvoltage7", 0.0],
            ],
        },
        "context_closed": True,
    }


def test_changed_plan_rejects_prior_dry_receipt_before_radio_contact(
    tmp_path: Path,
) -> None:
    config = write_config(tmp_path / "config.json")
    dry = tmp_path / "dry.json"
    assert run_cli(["--config", str(config), "--receipt", str(dry)]) == 0
    changed = config_document()
    changed["tx_lo_hz"] = 1_709_700_000
    changed_config = write_config(tmp_path / "changed.json", changed)
    opened = False

    def factory(uri: str) -> FakeTx2:
        del uri
        nonlocal opened
        opened = True
        return FakeTx2()

    assert (
        run_cli(
            [
                "--config",
                str(changed_config),
                "--receipt",
                str(tmp_path / "result.json"),
                "--arm",
                "--arm-from-dry-run",
                str(dry),
                "--confirm-radio-serial",
                SERIAL,
                "--confirm-conducted-topology",
                CONDUCTED_CONFIRMATION,
                "--confirm-operator-id",
                "OPERATOR-A",
                "--authorize-tx",
                TX_AUTHORIZATION,
            ],
            device_factory=factory,
            stderr=StringIO(),
        )
        == 1
    )
    assert not opened


def test_transmit_failure_still_writes_cleanup_outcome(tmp_path: Path) -> None:
    config = write_config(tmp_path / "config.json")
    dry = tmp_path / "dry.json"
    result = tmp_path / "result.json"
    assert run_cli(["--config", str(config), "--receipt", str(dry)]) == 0
    device = FakeTx2(fail_transmit=True)

    assert (
        run_cli(
            [
                "--config",
                str(config),
                "--receipt",
                str(result),
                "--arm",
                "--arm-from-dry-run",
                str(dry),
                "--confirm-radio-serial",
                SERIAL,
                "--confirm-conducted-topology",
                CONDUCTED_CONFIRMATION,
                "--confirm-operator-id",
                "OPERATOR-A",
                "--authorize-tx",
                TX_AUTHORIZATION,
            ],
            device_factory=lambda uri: device,
        )
        == 1
    )
    observed = receipt(result)
    assert observed["status"] == "fail"
    assert observed["cleanup"] == {"status": "adapter_completed_without_cleanup_error"}
    assert device.gain == -80.0
    assert set(device.dds.values()) == {0.0}
    assert device.events[-1] == "close"


def test_strict_config_rejects_unknown_field(tmp_path: Path) -> None:
    document = config_document()
    document["ignored_typo"] = True
    config = write_config(tmp_path / "config.json", document)
    assert (
        run_cli(
            ["--config", str(config), "--receipt", str(tmp_path / "out.json")],
            stderr=StringIO(),
        )
        == 1
    )
