"""Immutable station-local inputs for one V5 scan capture.

This is deployment configuration, not a public wire contract.  It deliberately
contains every identity and local path needed to bind one radio to one exact
plan.  Loading it performs no filesystem, database, or radio I/O beyond reading
the named JSON document.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leo_flow.contracts.capture import CapturePlan, GainMode, GainSetting
from leo_flow.contracts.continuity import ContinuityPolicy
from leo_flow.contracts.core import (
    Digest,
    DigestAlgorithm,
    HardwareSnapshotId,
    PlanId,
    RadioId,
    ReceiverChainId,
    StationId,
    canonical_digest,
    canonical_json_bytes,
)

from .drivers.pluto import (
    V5_FIRMWARE_COMMIT,
    V5_FIRMWARE_RELEASE,
    PlutoRadioConfig,
)
from .drivers.v5_preflight import ExpectedV5Runtime
from .engine import CaptureIdentity
from .scan_plan import StarlinkEdgeScanSpec, build_starlink_edge_scan_plan

STATION_SCHEMA = "org.leo-flow.v5-capture-station/v1"

_CANONICAL_USB_URI = re.compile(
    r"usb:(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
)

_RX_INTEGRITY_CANDIDATE_FIRMWARE_RELEASE = (
    "v0.38-plutoplus-spf-libiio-metadata-v5-rx-integrity-candidate1"
)
_RX_INTEGRITY_CANDIDATE_FIRMWARE_COMMIT = (
    "de830094a177daf4f577b60b9d3324b41f99ae58+libiio.patch.195bddceada230ef"
)
_RX_INTEGRITY_CANDIDATE_RUNTIME_ID = "gauss-pluto-v5-rx-integrity-close-barrier-1"
_RX_INTEGRITY_CANDIDATE_RUNTIME_DIGEST = Digest(
    DigestAlgorithm.SHA256,
    "0a9cf278bf836655afbf7a9a324a21c5dc41235d1b251386a0013eb0f299f123",
)
_RX_INTEGRITY_CANDIDATE_IIO_COMMIT = (
    "c26258bfa33098c2b215e19cf85d448e89499b1a+patch.195bddceada230ef"
)
_RX_INTEGRITY_CANDIDATE_SPF_REVISION = (
    "c40ee4116546889effd72056115adaaa1bc3fd40+patch.c9113a6d75466b4d"
)
_PERMITTED_V5_FIRMWARE_IDENTITIES = frozenset(
    {
        (V5_FIRMWARE_RELEASE, V5_FIRMWARE_COMMIT),
        (
            _RX_INTEGRITY_CANDIDATE_FIRMWARE_RELEASE,
            _RX_INTEGRITY_CANDIDATE_FIRMWARE_COMMIT,
        ),
    }
)


class V5StationConfigurationError(ValueError):
    """A station document cannot identify one exact safe capture."""


@dataclass(frozen=True, slots=True)
class V5ScanDefinition:
    plan_id: PlanId
    plan_digest: Digest
    sample_rate_hz: float
    bandwidth_hz: float
    sample_count: int
    edge_order: str
    edge_order_draw_u32: int
    arm_name: str
    lnb_lo_hz: float
    hardware_block_samples: int
    allow_clipped_pilot: bool = False


@dataclass(frozen=True, slots=True)
class V5RadioDefinition:
    uri: str
    expected_serial: str
    radio_id: RadioId
    receiver_chain_ids: tuple[ReceiverChainId, ReceiverChainId]
    firmware_release: str
    firmware_commit: str
    io_timeout_ms: int
    require_both_tx_muted: bool = False

    def __post_init__(self) -> None:
        prefix, separator, address = self.uri.partition(":")
        if not separator or prefix not in {"ip", "usb"}:
            raise V5StationConfigurationError(
                "station radio URI must select an explicit IP or USB context"
            )
        if prefix == "ip":
            try:
                parsed = ipaddress.ip_address(address)
            except ValueError as error:
                raise V5StationConfigurationError(
                    "station radio URI contains an invalid IP address"
                ) from error
            if parsed.version != 4 or str(parsed) != address:
                raise V5StationConfigurationError(
                    "station radio URI must contain a canonical IPv4 address"
                )
        elif _CANONICAL_USB_URI.fullmatch(self.uri) is None:
            raise V5StationConfigurationError(
                "station radio URI must contain a canonical USB bus.device.interface"
            )
        if not all((self.expected_serial, self.firmware_release, self.firmware_commit)):
            raise V5StationConfigurationError(
                "station radio identity and firmware cannot be empty"
            )
        if (
            self.firmware_release,
            self.firmware_commit,
        ) not in _PERMITTED_V5_FIRMWARE_IDENTITIES:
            raise V5StationConfigurationError(
                "station radio firmware is not an exact reviewed V5 build"
            )
        if len(set(self.receiver_chain_ids)) != 2:
            raise V5StationConfigurationError(
                "station radio requires two distinct receiver chains"
            )
        if self.io_timeout_ms <= 0:
            raise V5StationConfigurationError("station radio timeout must be positive")
        if not isinstance(self.require_both_tx_muted, bool):
            raise V5StationConfigurationError(
                "station both-TX mute requirement must be a boolean"
            )


@dataclass(frozen=True, slots=True)
class V5CaptureState:
    state_root: Path
    recording_root: Path
    spool_database: Path
    cas_root: Path
    lock_path: Path
    mode_lock_path: Path
    minimum_free_bytes: int
    require_cas_mount: bool

    def __post_init__(self) -> None:
        paths = (
            self.state_root,
            self.recording_root,
            self.spool_database,
            self.cas_root,
            self.lock_path,
            self.mode_lock_path,
        )
        if any(not path.is_absolute() for path in paths):
            raise V5StationConfigurationError(
                "station state and lock paths must be absolute"
            )
        if any(".." in path.parts for path in paths):
            raise V5StationConfigurationError(
                "station state and lock paths cannot traverse parents"
            )
        if self.recording_root.parent != self.state_root:
            raise V5StationConfigurationError(
                "station recording root must be directly beneath its state root"
            )
        if self.spool_database.parent != self.state_root:
            raise V5StationConfigurationError(
                "station spool database must be directly beneath its state root"
            )
        if self.lock_path == self.mode_lock_path:
            raise V5StationConfigurationError(
                "station radio lock and shared mode lock must be distinct"
            )
        if self.minimum_free_bytes <= 0:
            raise V5StationConfigurationError(
                "station minimum free bytes must be positive"
            )


@dataclass(frozen=True, slots=True)
class V5CaptureStation:
    station_id: StationId
    radio: V5RadioDefinition
    hardware_snapshot_id: HardwareSnapshotId
    clock_status: str
    capture_implementation: str
    runtime_manifest: Path
    runtime_manifest_digest: Digest
    expected_runtime: ExpectedV5Runtime
    plan: V5ScanDefinition
    state: V5CaptureState

    def __post_init__(self) -> None:
        if not self.clock_status or not self.capture_implementation:
            raise V5StationConfigurationError(
                "station clock and capture implementation cannot be empty"
            )
        if not self.runtime_manifest.is_absolute():
            raise V5StationConfigurationError(
                "station runtime manifest path must be absolute"
            )
        if self.runtime_manifest_digest.algorithm is not DigestAlgorithm.SHA256:
            raise V5StationConfigurationError(
                "station runtime manifest requires a SHA-256 digest"
            )
        runtime_paths = (
            self.expected_runtime.iio_module_path,
            self.expected_runtime.native_libiio_prefix,
            self.expected_runtime.pyadi_module_path,
            self.expected_runtime.spf_module_path,
        )
        if any(not Path(path).is_absolute() for path in runtime_paths):
            raise V5StationConfigurationError(
                "station expected runtime paths must be absolute"
            )
        self._require_candidate_runtime_binding()
        capture_plan = self.capture_plan()
        if canonical_digest(capture_plan) != self.plan.plan_digest:
            raise V5StationConfigurationError(
                "station plan differs from its declared immutable digest"
            )

    def _require_candidate_runtime_binding(self) -> None:
        firmware_identity = (
            self.radio.firmware_release,
            self.radio.firmware_commit,
        )
        candidate_identity = (
            _RX_INTEGRITY_CANDIDATE_FIRMWARE_RELEASE,
            _RX_INTEGRITY_CANDIDATE_FIRMWARE_COMMIT,
        )
        if firmware_identity != candidate_identity:
            return
        if (
            self.runtime_manifest_digest != _RX_INTEGRITY_CANDIDATE_RUNTIME_DIGEST
            or self.expected_runtime.runtime_id != _RX_INTEGRITY_CANDIDATE_RUNTIME_ID
            or self.expected_runtime.iio_commit != _RX_INTEGRITY_CANDIDATE_IIO_COMMIT
            or self.expected_runtime.spf_revision
            != _RX_INTEGRITY_CANDIDATE_SPF_REVISION
        ):
            raise V5StationConfigurationError(
                "candidate radio firmware requires its exact candidate host runtime"
            )

    def capture_plan(self) -> CapturePlan:
        return build_starlink_edge_scan_plan(
            StarlinkEdgeScanSpec(
                plan_id=self.plan.plan_id,
                radio_id=self.radio.radio_id,
                receiver_chain_ids=self.radio.receiver_chain_ids,
                gain=GainSetting(GainMode.AGC),
                sample_rate_hz=self.plan.sample_rate_hz,
                bandwidth_hz=self.plan.bandwidth_hz,
                sample_count=self.plan.sample_count,
                edge_order=self.plan.edge_order,
                lnb_lo_hz=self.plan.lnb_lo_hz,
                edge_order_draw_u32=self.plan.edge_order_draw_u32,
                arm_name=self.plan.arm_name,
                hardware_block_samples=self.plan.hardware_block_samples,
                allow_clipped_pilot=self.plan.allow_clipped_pilot,
            )
        )

    def capture_identity(self) -> CaptureIdentity:
        return CaptureIdentity(
            self.station_id,
            self.radio.expected_serial,
            self.clock_status,
            self.hardware_snapshot_id,
            self.capture_implementation,
        )

    def radio_config(self) -> PlutoRadioConfig:
        return PlutoRadioConfig(
            uri=self.radio.uri,
            expected_serial=self.radio.expected_serial,
            radio_id=self.radio.radio_id,
            receiver_chain_ids=self.radio.receiver_chain_ids,
            block_samples=self.plan.hardware_block_samples,
            frequency_tolerance_hz=2.0,
            continuity_policy=ContinuityPolicy.REQUIRE_CONTIGUOUS,
            firmware_release=self.radio.firmware_release,
            firmware_commit=self.radio.firmware_commit,
            io_timeout_ms=self.radio.io_timeout_ms,
        )

    def document(self) -> dict[str, object]:
        """Return the normalized secret-free station document."""

        return {
            "schema": STATION_SCHEMA,
            "station_id": str(self.station_id),
            "hardware_snapshot_id": str(self.hardware_snapshot_id),
            "clock_status": self.clock_status,
            "capture_implementation": self.capture_implementation,
            "runtime_manifest": str(self.runtime_manifest),
            "runtime_manifest_digest": str(self.runtime_manifest_digest),
            "expected_runtime": {
                "runtime_id": self.expected_runtime.runtime_id,
                "schema": self.expected_runtime.schema,
                "iio_module_path": self.expected_runtime.iio_module_path,
                "iio_version": list(self.expected_runtime.iio_version),
                "iio_commit": self.expected_runtime.iio_commit,
                "native_libiio_prefix": self.expected_runtime.native_libiio_prefix,
                "required_backends": sorted(self.expected_runtime.required_backends),
                "pyadi_version": self.expected_runtime.pyadi_version,
                "pyadi_module_path": self.expected_runtime.pyadi_module_path,
                "spf_module_path": self.expected_runtime.spf_module_path,
                "spf_revision": self.expected_runtime.spf_revision,
                "spf_import": self.expected_runtime.spf_import,
                "metadata_protocol": self.expected_runtime.metadata_protocol,
            },
            "radio": {
                "uri": self.radio.uri,
                "expected_serial": self.radio.expected_serial,
                "radio_id": str(self.radio.radio_id),
                "receiver_chain_ids": [
                    str(item) for item in self.radio.receiver_chain_ids
                ],
                "firmware_release": self.radio.firmware_release,
                "firmware_commit": self.radio.firmware_commit,
                "io_timeout_ms": self.radio.io_timeout_ms,
                **(
                    {"require_both_tx_muted": True}
                    if self.radio.require_both_tx_muted
                    else {}
                ),
            },
            "plan": {
                "plan_id": str(self.plan.plan_id),
                "plan_digest": str(self.plan.plan_digest),
                "sample_rate_hz": self.plan.sample_rate_hz,
                "bandwidth_hz": self.plan.bandwidth_hz,
                "sample_count": self.plan.sample_count,
                "edge_order": self.plan.edge_order,
                "edge_order_draw_u32": self.plan.edge_order_draw_u32,
                "arm_name": self.plan.arm_name,
                "lnb_lo_hz": self.plan.lnb_lo_hz,
                "hardware_block_samples": self.plan.hardware_block_samples,
                **(
                    {"allow_clipped_pilot": True}
                    if self.plan.allow_clipped_pilot
                    else {}
                ),
            },
            "state": {
                "state_root": str(self.state.state_root),
                "recording_root": str(self.state.recording_root),
                "spool_database": str(self.state.spool_database),
                "cas_root": str(self.state.cas_root),
                "lock_path": str(self.state.lock_path),
                "mode_lock_path": str(self.state.mode_lock_path),
                "minimum_free_bytes": self.state.minimum_free_bytes,
                "require_cas_mount": self.state.require_cas_mount,
            },
        }

    @property
    def specification_digest(self) -> Digest:
        return Digest.sha256(canonical_json_bytes(self.document()))


def load_v5_capture_station(path: Path) -> V5CaptureStation:
    """Load one exact station document without consulting ambient settings."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        root = _object(value, "station document")
        _exact_keys(
            root,
            {
                "schema",
                "station_id",
                "hardware_snapshot_id",
                "clock_status",
                "capture_implementation",
                "runtime_manifest",
                "runtime_manifest_digest",
                "expected_runtime",
                "radio",
                "plan",
                "state",
            },
            "station document",
        )
        if _string(root["schema"], "schema") != STATION_SCHEMA:
            raise V5StationConfigurationError("unsupported station schema")
        radio = _radio_definition(_object(root["radio"], "radio"))
        plan = _plan_definition(_object(root["plan"], "plan"))
        state = _state_definition(_object(root["state"], "state"))
        return V5CaptureStation(
            station_id=StationId(_string(root["station_id"], "station_id")),
            radio=radio,
            hardware_snapshot_id=HardwareSnapshotId(
                _string(root["hardware_snapshot_id"], "hardware_snapshot_id")
            ),
            clock_status=_string(root["clock_status"], "clock_status"),
            capture_implementation=_string(
                root["capture_implementation"], "capture_implementation"
            ),
            runtime_manifest=Path(
                _string(root["runtime_manifest"], "runtime_manifest")
            ),
            runtime_manifest_digest=_digest(
                root["runtime_manifest_digest"], "runtime_manifest_digest"
            ),
            expected_runtime=_expected_runtime(
                _object(root["expected_runtime"], "expected_runtime")
            ),
            plan=plan,
            state=state,
        )
    except V5StationConfigurationError:
        raise
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise V5StationConfigurationError(
            f"station document is invalid: {type(error).__name__}"
        ) from error


def require_disjoint_station_pair(
    first: V5CaptureStation, second: V5CaptureStation
) -> None:
    """Reject identities or private state that could alias in a dual run.

    The content-addressed object root is intentionally shareable.  Every radio,
    plan, mutable staging root, spool database, and process lock is not.
    """

    identities = (
        ("radio URI", first.radio.uri, second.radio.uri),
        (
            "radio serial",
            first.radio.expected_serial,
            second.radio.expected_serial,
        ),
        ("radio ID", first.radio.radio_id, second.radio.radio_id),
        ("plan ID", first.plan.plan_id, second.plan.plan_id),
        ("state root", first.state.state_root, second.state.state_root),
        (
            "recording root",
            first.state.recording_root,
            second.state.recording_root,
        ),
        (
            "spool database",
            first.state.spool_database,
            second.state.spool_database,
        ),
        ("lock path", first.state.lock_path, second.state.lock_path),
    )
    collisions = tuple(name for name, left, right in identities if left == right)
    if set(first.radio.receiver_chain_ids) & set(second.radio.receiver_chain_ids):
        collisions += ("receiver chain IDs",)
    if _paths_overlap(first.state.state_root, second.state.state_root):
        collisions += ("state root ancestry",)
    if first.state.cas_root != second.state.cas_root:
        collisions += ("CAS root divergence",)
    if first.state.mode_lock_path != second.state.mode_lock_path:
        collisions += ("mode lock divergence",)
    if collisions:
        raise V5StationConfigurationError(
            "dual station specifications collide: " + ", ".join(collisions)
        )


def require_passive_both_tx_station_pair(
    first: V5CaptureStation, second: V5CaptureStation
) -> None:
    """Admit only the current science policy before any live port is built."""

    if not first.radio.require_both_tx_muted or not second.radio.require_both_tx_muted:
        raise V5StationConfigurationError(
            "science station pair must require both TX outputs muted"
        )


def _paths_overlap(first: Path, second: Path) -> bool:
    return first.is_relative_to(second) or second.is_relative_to(first)


def _radio_definition(value: Mapping[str, Any]) -> V5RadioDefinition:
    required = {
        "uri",
        "expected_serial",
        "radio_id",
        "receiver_chain_ids",
        "firmware_release",
        "firmware_commit",
        "io_timeout_ms",
    }
    if set(value) not in (required, required | {"require_both_tx_muted"}):
        raise V5StationConfigurationError("radio fields are not exact")
    chains = value["receiver_chain_ids"]
    if not isinstance(chains, list) or len(chains) != 2:
        raise V5StationConfigurationError(
            "receiver_chain_ids must contain exactly two entries"
        )
    return V5RadioDefinition(
        uri=_string(value["uri"], "radio.uri"),
        expected_serial=_string(value["expected_serial"], "radio.expected_serial"),
        radio_id=RadioId(_string(value["radio_id"], "radio.radio_id")),
        receiver_chain_ids=(
            ReceiverChainId(_string(chains[0], "receiver_chain_ids[0]")),
            ReceiverChainId(_string(chains[1], "receiver_chain_ids[1]")),
        ),
        firmware_release=_string(value["firmware_release"], "radio.firmware_release"),
        firmware_commit=_string(value["firmware_commit"], "radio.firmware_commit"),
        io_timeout_ms=_integer(value["io_timeout_ms"], "radio.io_timeout_ms"),
        require_both_tx_muted=(
            _boolean(value["require_both_tx_muted"], "radio.require_both_tx_muted")
            if "require_both_tx_muted" in value
            else False
        ),
    )


def _plan_definition(value: Mapping[str, Any]) -> V5ScanDefinition:
    required = {
        "plan_id",
        "plan_digest",
        "sample_rate_hz",
        "bandwidth_hz",
        "sample_count",
        "edge_order",
        "edge_order_draw_u32",
        "arm_name",
        "lnb_lo_hz",
        "hardware_block_samples",
    }
    if set(value) not in (required, required | {"allow_clipped_pilot"}):
        raise V5StationConfigurationError("plan fields are not exact")
    return V5ScanDefinition(
        plan_id=PlanId(_string(value["plan_id"], "plan.plan_id")),
        plan_digest=_digest(value["plan_digest"]),
        sample_rate_hz=_number(value["sample_rate_hz"], "plan.sample_rate_hz"),
        bandwidth_hz=_number(value["bandwidth_hz"], "plan.bandwidth_hz"),
        sample_count=_integer(value["sample_count"], "plan.sample_count"),
        edge_order=_string(value["edge_order"], "plan.edge_order"),
        edge_order_draw_u32=_integer(
            value["edge_order_draw_u32"], "plan.edge_order_draw_u32"
        ),
        arm_name=_string(value["arm_name"], "plan.arm_name"),
        lnb_lo_hz=_number(value["lnb_lo_hz"], "plan.lnb_lo_hz"),
        hardware_block_samples=_integer(
            value["hardware_block_samples"], "plan.hardware_block_samples"
        ),
        allow_clipped_pilot=(
            _boolean(value["allow_clipped_pilot"], "plan.allow_clipped_pilot")
            if "allow_clipped_pilot" in value
            else False
        ),
    )


def _state_definition(value: Mapping[str, Any]) -> V5CaptureState:
    _exact_keys(
        value,
        {
            "state_root",
            "recording_root",
            "spool_database",
            "cas_root",
            "lock_path",
            "mode_lock_path",
            "minimum_free_bytes",
            "require_cas_mount",
        },
        "state",
    )
    return V5CaptureState(
        state_root=Path(_string(value["state_root"], "state.state_root")),
        recording_root=Path(_string(value["recording_root"], "state.recording_root")),
        spool_database=Path(_string(value["spool_database"], "state.spool_database")),
        cas_root=Path(_string(value["cas_root"], "state.cas_root")),
        lock_path=Path(_string(value["lock_path"], "state.lock_path")),
        mode_lock_path=Path(_string(value["mode_lock_path"], "state.mode_lock_path")),
        minimum_free_bytes=_integer(
            value["minimum_free_bytes"], "state.minimum_free_bytes"
        ),
        require_cas_mount=_boolean(
            value["require_cas_mount"], "state.require_cas_mount"
        ),
    )


def _expected_runtime(value: Mapping[str, Any]) -> ExpectedV5Runtime:
    _exact_keys(
        value,
        {
            "runtime_id",
            "schema",
            "iio_module_path",
            "iio_version",
            "iio_commit",
            "native_libiio_prefix",
            "required_backends",
            "pyadi_version",
            "pyadi_module_path",
            "spf_module_path",
            "spf_revision",
            "spf_import",
            "metadata_protocol",
        },
        "expected_runtime",
    )
    version = value["iio_version"]
    if (
        not isinstance(version, list)
        or len(version) != 3
        or isinstance(version[0], bool)
        or not isinstance(version[0], int)
        or isinstance(version[1], bool)
        or not isinstance(version[1], int)
    ):
        raise V5StationConfigurationError(
            "expected_runtime.iio_version must be [major, minor, revision]"
        )
    backends = value["required_backends"]
    if not isinstance(backends, list):
        raise V5StationConfigurationError(
            "expected_runtime.required_backends must be an array"
        )
    return ExpectedV5Runtime(
        runtime_id=_string(value["runtime_id"], "expected_runtime.runtime_id"),
        schema=_string(value["schema"], "expected_runtime.schema"),
        iio_module_path=_string(
            value["iio_module_path"], "expected_runtime.iio_module_path"
        ),
        iio_version=(
            version[0],
            version[1],
            _string(version[2], "expected_runtime.iio_version[2]"),
        ),
        iio_commit=_string(value["iio_commit"], "expected_runtime.iio_commit"),
        native_libiio_prefix=_string(
            value["native_libiio_prefix"],
            "expected_runtime.native_libiio_prefix",
        ),
        required_backends=frozenset(
            _string(item, "expected_runtime.required_backends") for item in backends
        ),
        pyadi_version=_string(value["pyadi_version"], "expected_runtime.pyadi_version"),
        pyadi_module_path=_string(
            value["pyadi_module_path"], "expected_runtime.pyadi_module_path"
        ),
        spf_module_path=_string(
            value["spf_module_path"], "expected_runtime.spf_module_path"
        ),
        spf_revision=_string(value["spf_revision"], "expected_runtime.spf_revision"),
        spf_import=_string(value["spf_import"], "expected_runtime.spf_import"),
        metadata_protocol=_string(
            value["metadata_protocol"], "expected_runtime.metadata_protocol"
        ),
    )


def _object(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise V5StationConfigurationError(f"{name} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise V5StationConfigurationError(f"{name} fields are not exact")


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise V5StationConfigurationError(f"{name} must be a non-empty string")
    return value


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise V5StationConfigurationError(f"{name} must be an integer")
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise V5StationConfigurationError(f"{name} must be a boolean")
    return value


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise V5StationConfigurationError(f"{name} must be a number")
    return float(value)


def _digest(value: object, name: str = "plan.plan_digest") -> Digest:
    text = _string(value, name)
    algorithm, separator, digest = text.partition(":")
    if not separator:
        raise V5StationConfigurationError(f"{name} must include its algorithm")
    try:
        return Digest(DigestAlgorithm(algorithm), digest)
    except ValueError as error:
        raise V5StationConfigurationError(f"{name} is invalid") from error
