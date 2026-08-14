from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from leo_flow.capture.drivers.v5_observers import (
    observe_current_v5_runtime,
    observe_v5_radio,
)
from leo_flow.capture.errors import RadioConfigurationError


def _runtime_fixture(tmp_path):  # type: ignore[no-untyped-def]
    site = tmp_path / "site"
    iio_path = site / "iio.py"
    adi_path = site / "adi" / "__init__.py"
    spf_root = site / "spf"
    spf_module_path = spf_root / "direct_radio" / "iio_metadata.py"
    psycopg_path = site / "psycopg" / "__init__.py"
    for path in (
        iio_path,
        adi_path,
        spf_root / "__init__.py",
        spf_module_path,
        psycopg_path,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {path.name}\n", encoding="utf-8")
    spf_files = {
        str(path.relative_to(site)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (spf_root / "__init__.py", spf_module_path)
    }
    manifest = {
        "schema": "leo-flow.v5-runtime/v1",
        "runtime_id": "runtime-test",
        "firmware": {"metadata_protocol": 3},
        "libiio": {"commit": "iio-commit"},
        "pyadi": {"distribution": "pyadi-iio", "version": "0.0.21"},
        "numpy": {"distribution": "numpy", "version": "2.4.6"},
        "psycopg": {"distribution": "psycopg", "version": "3.3.4"},
        "spf": {
            "commit": "spf-commit",
            "import": "spf.direct_radio.iio_metadata:IioMetadataRx",
            "files": spf_files,
        },
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    modules = {
        "iio": SimpleNamespace(
            __file__=str(iio_path),
            version=(0, 25, "c26258b"),
            MetadataBuffer=object,
            backends=("local", "network", "usb"),
        ),
        "adi": SimpleNamespace(__file__=str(adi_path)),
        "spf": SimpleNamespace(__path__=(str(spf_root),)),
        "spf.direct_radio.iio_metadata": SimpleNamespace(
            __file__=str(spf_module_path), IioMetadataRx=object
        ),
        "psycopg": SimpleNamespace(__file__=str(psycopg_path)),
    }
    versions = {"pyadi-iio": "0.0.21", "numpy": "2.4.6", "psycopg": "3.3.4"}
    return manifest_path, modules, versions, spf_module_path


def test_runtime_observer_reports_modules_loaded_in_this_process(tmp_path) -> None:
    manifest, modules, versions, spf_module_path = _runtime_fixture(tmp_path)
    loaded: list[str] = []

    def load(name: str):  # type: ignore[no-untyped-def]
        loaded.append(name)
        return modules[name]

    observed = observe_current_v5_runtime(
        manifest,
        module_loader=load,
        distribution_version=versions.__getitem__,
        maps_reader=lambda: "7f-8f r-xp 0000 00:00 0 /opt/leo-v5/lib/libiio.so.0\n",
    )
    assert observed.runtime_id == "runtime-test"
    assert observed.iio_version == (0, 25, "c26258b")
    assert observed.available_backends == frozenset(("local", "ip", "usb"))
    assert observed.native_libiio_paths == ("/opt/leo-v5/lib/libiio.so.0",)
    assert observed.spf_module_path == str(spf_module_path.resolve())
    assert observed.metadata_protocol == "spf-radio-metadata-v3"
    assert loaded == [
        "iio",
        "adi",
        "spf",
        "spf.direct_radio.iio_metadata",
        "psycopg",
    ]


def test_runtime_observer_rejects_tampered_spf_and_missing_psycopg(tmp_path) -> None:
    manifest, modules, versions, spf_module_path = _runtime_fixture(tmp_path)
    spf_module_path.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(RadioConfigurationError, match="runtime observation failed"):
        observe_current_v5_runtime(
            manifest,
            module_loader=modules.__getitem__,
            distribution_version=versions.__getitem__,
            maps_reader=lambda: "x /opt/leo-v5/lib/libiio.so.0\n",
        )

    _, modules, versions, _ = _runtime_fixture(tmp_path / "fresh")

    def missing_psycopg(name: str):  # type: ignore[no-untyped-def]
        if name == "psycopg":
            raise ImportError("absent")
        return modules[name]

    with pytest.raises(RadioConfigurationError, match="ImportError"):
        observe_current_v5_runtime(
            tmp_path / "fresh" / "manifest.json",
            module_loader=missing_psycopg,
            distribution_version=versions.__getitem__,
            maps_reader=lambda: "x /opt/leo-v5/lib/libiio.so.0\n",
        )


class _Attribute:
    def __init__(self, value: str) -> None:
        self.value = value


def test_radio_observer_reads_selected_context_and_scan_layout() -> None:
    attrs = {
        "hw_serial": _Attribute("serial-v5"),
        "fw_version": _Attribute("firmware-v5"),
        "iio,buffer-metadata": _Attribute("1"),
    }
    channels = tuple(
        SimpleNamespace(id=name, index=index, output=False, scan_element=True)
        for index, name in enumerate(
            ("voltage0_i", "voltage0_q", "voltage1_i", "voltage1_q")
        )
    )
    device = SimpleNamespace(
        _ctx=SimpleNamespace(attrs=attrs),
        _rxadc=SimpleNamespace(channels=channels),
    )
    observed = observe_v5_radio(device)
    assert observed.serial == "serial-v5"
    assert observed.firmware_release == "firmware-v5"
    assert observed.metadata_capability == "iio,buffer-metadata=1"
    assert observed.enabled_scan_mask == 0x0F
    assert observed.channel_count == 2
    assert observed.component_layout == ("I0", "Q0", "I1", "Q1")


def test_radio_observer_rejects_unindexed_scan_claim() -> None:
    device = SimpleNamespace(
        serial="serial-v5",
        fw_version="firmware-v5",
        _ctx=SimpleNamespace(attrs={"iio,buffer-metadata": _Attribute("1")}),
        _rxadc=SimpleNamespace(
            channels=(SimpleNamespace(id="voltage0_i", output=False),)
        ),
    )
    with pytest.raises(RadioConfigurationError, match="radio observation failed"):
        observe_v5_radio(device)
