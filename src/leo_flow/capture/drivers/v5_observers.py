"""Current-process host and selected-device observers for V5 preflight."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..errors import RadioConfigurationError
from .pluto import PlutoDevice
from .v5_preflight import (
    TX2_DDS_CHANNEL_IDS,
    ObservedV5Radio,
    ObservedV5Runtime,
)

ModuleLoader = Callable[[str], Any]
VersionReader = Callable[[str], str]
MapsReader = Callable[[], str]


def observe_current_v5_runtime(
    manifest_path: Path = Path("/opt/leo-v5/runtime-manifest.json"),
    *,
    module_loader: ModuleLoader = importlib.import_module,
    distribution_version: VersionReader = importlib.metadata.version,
    maps_reader: MapsReader | None = None,
) -> ObservedV5Runtime:
    """Verify and report the libraries loaded by this capture process.

    No verifier subprocess is used: module paths and ``/proc/self/maps`` refer
    to the process that will construct the radio adapter.
    """

    try:
        manifest = _load_manifest(manifest_path)
        libiio = _mapping(manifest["libiio"], "libiio")
        pyadi = _mapping(manifest["pyadi"], "pyadi")
        spf = _mapping(manifest["spf"], "spf")
        psycopg = _mapping(manifest["psycopg"], "psycopg")
        iio_module = module_loader("iio")
        adi_module = module_loader("adi")
        spf_package = module_loader("spf")
        module_name, separator, symbol = _string(spf["import"], "SPF import").partition(
            ":"
        )
        spf_module = module_loader(module_name)
        if not separator or not hasattr(spf_module, symbol):
            raise ValueError("pinned SPF integration symbol is absent")
        module_loader("psycopg")
        _verify_distribution(distribution_version, pyadi)
        _verify_distribution(distribution_version, _mapping(manifest["numpy"], "numpy"))
        _verify_distribution(distribution_version, psycopg)
        _verify_spf_files(spf_package, spf)
        version = _version_tuple(getattr(iio_module, "version", None))
        paths = _loaded_libiio_paths(
            maps_reader() if maps_reader is not None else _read_process_maps()
        )
        protocol_number = _mapping(manifest["firmware"], "firmware")[
            "metadata_protocol"
        ]
        if isinstance(protocol_number, bool) or not isinstance(protocol_number, int):
            raise TypeError("firmware metadata protocol must be an integer")
        return ObservedV5Runtime(
            runtime_id=_string(manifest["runtime_id"], "runtime_id"),
            schema=_string(manifest["schema"], "schema"),
            iio_module_path=_module_path(iio_module),
            iio_version=version,
            iio_commit=_string(libiio["commit"], "libiio commit"),
            metadata_buffer_present=hasattr(iio_module, "MetadataBuffer"),
            native_libiio_paths=paths,
            available_backends=frozenset(
                _backend_name(item)
                for item in _string_sequence(
                    getattr(iio_module, "backends", None), "libiio backends"
                )
            ),
            pyadi_version=distribution_version(
                _string(pyadi["distribution"], "pyadi distribution")
            ),
            pyadi_module_path=_module_path(adi_module),
            spf_module_path=_module_path(spf_module),
            spf_revision=_string(spf["commit"], "SPF revision"),
            spf_import=_string(spf["import"], "SPF import"),
            metadata_protocol=f"spf-radio-metadata-v{protocol_number}",
        )
    except RadioConfigurationError:
        raise
    except Exception as error:
        raise RadioConfigurationError(
            f"V5 runtime observation failed: {type(error).__name__}"
        ) from error


def observe_v5_radio(device: PlutoDevice) -> ObservedV5Radio:
    """Read radio identity and paired scan facts from the selected context."""

    try:
        serial = _first_value(device, ("serial", "hw_serial"))
        firmware = _first_value(device, ("fw_version", "firmware_version"))
        capability_value = _first_value(device, ("iio,buffer-metadata",))
        capability = (
            capability_value
            if "=" in capability_value
            else f"iio,buffer-metadata={capability_value}"
        )
        rx_device = device._rxadc  # type: ignore[attr-defined]
        channels = rx_device.channels
        if not isinstance(channels, Sequence):
            raise TypeError("RX scan channels are unavailable")
        scanned: list[tuple[int, int, str]] = []
        for channel in channels:
            if bool(getattr(channel, "output", False)) or not bool(
                getattr(channel, "scan_element", True)
            ):
                continue
            identifier = str(getattr(channel, "id", getattr(channel, "name", "")))
            component = _rx_component(identifier)
            if component is None:
                continue
            index = getattr(channel, "index", None)
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise ValueError("RX scan channel lacks a valid index")
            receiver, label = component
            scanned.append((index, receiver, label))
        if (
            not scanned
            or len({index for index, _, _ in scanned}) != len(scanned)
            or len({label for _, _, label in scanned}) != len(scanned)
        ):
            raise ValueError("paired RX scan layout is absent or ambiguous")
        scanned.sort()
        tx2_hardware_gain_db, tx2_dds_scales = _observe_tx2_mute_state(device)
        return ObservedV5Radio(
            serial=serial,
            firmware_release=firmware,
            metadata_capability=capability,
            enabled_scan_mask=sum(1 << index for index, _, _ in scanned),
            channel_count=len({receiver for _, receiver, _ in scanned}),
            component_layout=tuple(component for _, _, component in scanned),
            tx2_hardware_gain_db=tx2_hardware_gain_db,
            tx2_dds_scales=tx2_dds_scales,
        )
    except Exception as error:
        raise RadioConfigurationError(
            f"V5 radio observation failed: {type(error).__name__}"
        ) from error


def _observe_tx2_mute_state(
    device: PlutoDevice,
) -> tuple[float, tuple[tuple[str, float], ...]]:
    context = getattr(device, "_ctx", getattr(device, "ctx", None))
    find_device = getattr(context, "find_device", None)
    if not callable(find_device):
        raise TypeError("selected context cannot inspect TX2 state")
    phy = find_device("ad9361-phy")
    dds = find_device("cf-ad9361-dds-core-lpc")
    if phy is None or dds is None:
        raise ValueError("selected context lacks TX2 PHY or DDS")

    gain: float | None = None
    for channel in getattr(phy, "channels", ()):
        if (
            bool(getattr(channel, "output", False))
            and getattr(channel, "id", None) == "voltage1"
        ):
            gain = _numeric_channel_attribute(channel, "hardwaregain")
            break
    if gain is None:
        raise ValueError("selected context lacks TX2 hardware gain")

    scales: dict[str, float] = {}
    for channel in getattr(dds, "channels", ()):
        channel_id = str(getattr(channel, "id", ""))
        if bool(getattr(channel, "output", False)) and (
            channel_id in TX2_DDS_CHANNEL_IDS
        ):
            scales[channel_id] = _numeric_channel_attribute(channel, "scale")
    if set(scales) != set(TX2_DDS_CHANNEL_IDS):
        raise ValueError("selected context lacks the complete TX2 DDS layout")
    return gain, tuple(
        (channel_id, scales[channel_id]) for channel_id in TX2_DDS_CHANNEL_IDS
    )


def _numeric_channel_attribute(channel: object, name: str) -> float:
    attrs = getattr(channel, "attrs", None)
    if attrs is None:
        raise ValueError(f"TX2 channel lacks {name}")
    try:
        attribute = attrs[name]
    except (KeyError, TypeError) as error:
        raise ValueError(f"TX2 channel lacks {name}") from error
    value = getattr(attribute, "value", attribute)
    return float(str(value).split()[0])


def _rx_component(identifier: str) -> tuple[int, str] | None:
    named = re.fullmatch(r"voltage([01])_([iqIQ])", identifier)
    if named is not None:
        receiver = int(named.group(1))
        return receiver, f"{named.group(2).upper()}{receiver}"
    indexed = re.fullmatch(r"voltage([0-3])", identifier)
    if indexed is None:
        return None
    component_index = int(indexed.group(1))
    receiver = component_index // 2
    component = "I" if component_index % 2 == 0 else "Q"
    return receiver, f"{component}{receiver}"


def _load_manifest(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    manifest = _mapping(value, "runtime manifest")
    if manifest.get("schema") != "leo-flow.v5-runtime/v1":
        raise ValueError("unsupported V5 runtime manifest schema")
    for key in ("runtime_id", "firmware", "libiio", "pyadi", "numpy", "psycopg", "spf"):
        if key not in manifest:
            raise ValueError(f"V5 runtime manifest lacks {key}")
    return manifest


def _verify_distribution(reader: VersionReader, dependency: Mapping[str, Any]) -> None:
    name = _string(dependency["distribution"], "distribution")
    if reader(name) != _string(dependency["version"], f"{name} version"):
        raise ValueError(f"{name} version differs from the runtime manifest")


def _verify_spf_files(package: Any, spf: Mapping[str, Any]) -> None:
    roots = tuple(getattr(package, "__path__", ()))
    if len(roots) != 1:
        raise ValueError("SPF must resolve from exactly one installed package root")
    package_root = Path(str(roots[0])).resolve().parent
    files = _mapping(spf["files"], "SPF files")
    if not files:
        raise ValueError("SPF source digest table is empty")
    for relative, expected in files.items():
        source = package_root / _string(relative, "SPF relative path")
        if hashlib.sha256(source.read_bytes()).hexdigest() != _string(
            expected, "SPF source digest"
        ):
            raise ValueError(f"SPF source digest differs: {relative}")


def _first_value(device: PlutoDevice, names: tuple[str, ...]) -> str:
    for name in names:
        direct = getattr(device, name, None)
        if direct not in (None, ""):
            return str(direct)
    for owner_name in ("ctx", "_ctx", "_ctrl"):
        owner = getattr(device, owner_name, None)
        attrs = getattr(owner, "attrs", None)
        if attrs is None:
            continue
        for name in names:
            try:
                raw = attrs[name]
            except (KeyError, TypeError):
                continue
            value = getattr(raw, "value", raw)
            if value not in (None, ""):
                return str(value)
    raise ValueError(f"radio attribute is absent: {names[0]}")


def _read_process_maps() -> str:
    return Path("/proc/self/maps").read_text(encoding="utf-8")


def _loaded_libiio_paths(maps: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                str(Path(line.rsplit(maxsplit=1)[-1]).resolve())
                for line in maps.splitlines()
                if "/" in line and "libiio.so" in line.rsplit(maxsplit=1)[-1]
            }
        )
    )


def _module_path(module: Any) -> str:
    value = getattr(module, "__file__", None)
    if not isinstance(value, str):
        raise TypeError("loaded module lacks a source path")
    return str(Path(value).resolve())


def _version_tuple(value: object) -> tuple[int, int, str]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, str)
        or len(value) != 3
        or isinstance(value[0], bool)
        or not isinstance(value[0], int)
        or isinstance(value[1], bool)
        or not isinstance(value[1], int)
        or not isinstance(value[2], str)
    ):
        raise ValueError("libiio version is malformed")
    return value[0], value[1], value[2]


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _string_sequence(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise TypeError(f"{name} must be an array")
    return tuple(_string(item, name) for item in value)


def _backend_name(value: str) -> str:
    return "ip" if value == "network" else value
