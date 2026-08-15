from __future__ import annotations

from pathlib import Path

import pytest

import leo_flow.deployments.v5_scan_e2e as harness
from leo_flow.deployments.v5_scan_e2e import (
    EXPECTED_SERIAL,
    V5ScanE2EError,
    _require_empty_output_root,
    _require_live_confirmation,
    _verify_tx2_muted,
)


class _Attr:
    def __init__(self, value: str) -> None:
        self.value = value


class _Channel:
    def __init__(self, channel_id: str, **attrs: str) -> None:
        self.id = channel_id
        self.output = True
        self.attrs = {name: _Attr(value) for name, value in attrs.items()}


class _Device:
    def __init__(self, channels: list[_Channel]) -> None:
        self.channels = channels


class _Context:
    def __init__(self, *, tx2_gain: str = "-80 dB", tx2_scale: str = "0") -> None:
        self.attrs = {"hw_serial": EXPECTED_SERIAL}
        self.destroyed = False
        self.timeout_calls: list[int] = []
        self._devices = {
            "ad9361-phy": _Device([_Channel("voltage1", hardwaregain=tx2_gain)]),
            "cf-ad9361-dds-core-lpc": _Device(
                [
                    _Channel(channel_id, scale=tx2_scale)
                    for channel_id in (
                        "altvoltage4",
                        "altvoltage5",
                        "altvoltage6",
                        "altvoltage7",
                    )
                ]
            ),
        }

    def find_device(self, name: str) -> _Device | None:
        return self._devices.get(name)

    def set_timeout(self, value: int) -> None:
        self.timeout_calls.append(value)

    def destroy(self) -> None:
        self.destroyed = True


class _Iio:
    def __init__(self, context: _Context) -> None:
        self._context = context

    def Context(self, uri: str) -> _Context:
        assert uri == harness.EXPECTED_URI
        return self._context


def test_live_confirmation_requires_exact_radio_serial() -> None:
    _require_live_confirmation(EXPECTED_SERIAL)
    with pytest.raises(V5ScanE2EError, match="exact expected"):
        _require_live_confirmation("104000-wrong")


def test_output_root_must_be_new_or_empty(tmp_path) -> None:
    root = tmp_path / "result"
    assert _require_empty_output_root(root) == root.resolve()
    (root / "existing").write_text("do not overwrite")
    with pytest.raises(V5ScanE2EError, match="absent or empty"):
        _require_empty_output_root(root)


def test_output_root_must_be_absolute(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(V5ScanE2EError, match="must be absolute"):
        _require_empty_output_root(Path("relative"))


def test_output_root_must_be_a_directory(tmp_path) -> None:
    root = tmp_path / "result"
    root.write_text("not a directory")
    with pytest.raises(V5ScanE2EError, match="must be a directory"):
        _require_empty_output_root(root)


def test_tx2_mute_check_is_read_only_and_closes_context(monkeypatch) -> None:
    context = _Context()
    monkeypatch.setattr(
        "leo_flow.deployments.v5_live_safety.importlib.import_module",
        lambda name: _Iio(context),
    )

    evidence = _verify_tx2_muted()

    assert evidence == {
        "radio_serial": EXPECTED_SERIAL,
        "tx2_hardware_gain_db": -80.0,
        "tx2_dds_scales": {
            "altvoltage4": 0.0,
            "altvoltage5": 0.0,
            "altvoltage6": 0.0,
            "altvoltage7": 0.0,
        },
        "read_only_check": True,
    }
    assert context.timeout_calls == [5_000]
    assert context.destroyed


def test_tx2_mute_check_rejects_nonzero_dds_and_closes_context(monkeypatch) -> None:
    context = _Context(tx2_scale="0.25")
    monkeypatch.setattr(
        "leo_flow.deployments.v5_live_safety.importlib.import_module",
        lambda name: _Iio(context),
    )

    with pytest.raises(V5ScanE2EError, match="DDS scales are not all zero"):
        _verify_tx2_muted()

    assert context.destroyed
