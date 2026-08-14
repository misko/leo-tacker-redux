"""Strict canonical codec for hardware metadata snapshots."""

from __future__ import annotations

import json
from typing import Any, NoReturn, cast

from leo_flow.contracts.core import (
    HardwareSnapshotId,
    RadioId,
    ReceiverChainId,
    SchemaRef,
    StationId,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.hardware import HardwareMetadataSnapshot, ReceiverChainMetadata

MAX_HARDWARE_SNAPSHOT_BYTES = 4 * 1024 * 1024
HARDWARE_SNAPSHOT_MEDIA_TYPE = "application/json"
HARDWARE_SNAPSHOT_FORMAT_ID = "hardware-metadata-snapshot-v0.1"


class MalformedHardwareSnapshotError(ValueError):
    pass


def encode_hardware_snapshot(snapshot: HardwareMetadataSnapshot) -> bytes:
    return canonical_json_bytes(
        {
            "schema": snapshot.SCHEMA_ID,
            "version": "0.1",
            "snapshot_id": str(snapshot.snapshot_id),
            "station_id": str(snapshot.station_id),
            "radio_ids": [str(value) for value in snapshot.radio_ids],
            "receiver_chains": [
                {
                    "receiver_chain_id": str(chain.receiver_chain_id),
                    "radio_id": str(chain.radio_id),
                    "radio_channel": chain.radio_channel,
                    "lnb_id": chain.lnb_id,
                    "polarization": chain.polarization,
                    "cable_id": chain.cable_id,
                    "valid_from_utc_ns": chain.valid_from_utc_ns,
                    "valid_until_utc_ns": chain.valid_until_utc_ns,
                }
                for chain in snapshot.receiver_chains
            ],
        }
    )


def decode_hardware_snapshot(data: bytes) -> HardwareMetadataSnapshot:
    if len(data) > MAX_HARDWARE_SNAPSHOT_BYTES:
        raise MalformedHardwareSnapshotError("hardware snapshot exceeds size limit")
    try:
        document = json.loads(data, object_pairs_hook=_unique)
        if canonical_json_bytes(document) != data:
            _bad("hardware snapshot bytes are not canonical JSON")
        root = _object(document, "root")
        _keys(
            root,
            {
                "schema",
                "version",
                "snapshot_id",
                "station_id",
                "radio_ids",
                "receiver_chains",
            },
            "root",
        )
        if (
            root["schema"] != HardwareMetadataSnapshot.SCHEMA_ID
            or root["version"] != "0.1"
        ):
            _bad("unsupported hardware snapshot schema")
        return HardwareMetadataSnapshot(
            SchemaRef(HardwareMetadataSnapshot.SCHEMA_ID),
            HardwareSnapshotId(_string(root["snapshot_id"], "snapshot_id")),
            StationId(_string(root["station_id"], "station_id")),
            tuple(
                RadioId(_string(value, "radio_id"))
                for value in _array(root["radio_ids"], "radio_ids")
            ),
            tuple(
                _chain(value, index)
                for index, value in enumerate(
                    _array(root["receiver_chains"], "receiver_chains")
                )
            ),
        )
    except MalformedHardwareSnapshotError:
        raise
    except (TypeError, ValueError) as error:
        raise MalformedHardwareSnapshotError(str(error)) from error


def _chain(value: object, index: int) -> ReceiverChainMetadata:
    name = f"receiver_chains[{index}]"
    item = _object(value, name)
    _keys(
        item,
        {
            "receiver_chain_id",
            "radio_id",
            "radio_channel",
            "lnb_id",
            "polarization",
            "cable_id",
            "valid_from_utc_ns",
            "valid_until_utc_ns",
        },
        name,
    )
    until = item["valid_until_utc_ns"]
    return ReceiverChainMetadata(
        ReceiverChainId(_string(item["receiver_chain_id"], "receiver_chain_id")),
        RadioId(_string(item["radio_id"], "radio_id")),
        _integer(item["radio_channel"], "radio_channel"),
        _string(item["lnb_id"], "lnb_id"),
        _optional_string(item["polarization"], "polarization"),
        _optional_string(item["cable_id"], "cable_id"),
        UtcNs(_integer(item["valid_from_utc_ns"], "valid_from_utc_ns")),
        None if until is None else UtcNs(_integer(until, "valid_until_utc_ns")),
    )


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _bad(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        _bad(f"{name} must be an object")
    return cast(dict[str, object], value)


def _array(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        _bad(f"{name} must be an array")
    return value


def _keys(value: dict[str, object], expected: set[str], name: str) -> None:
    if set(value) != expected:
        _bad(f"{name} fields differ from the schema")


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        _bad(f"{name} must be a string")
    return value


def _optional_string(value: object, name: str) -> str | None:
    return None if value is None else _string(value, name)


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _bad(f"{name} must be an integer")
    return value


def _bad(message: str) -> NoReturn:
    raise MalformedHardwareSnapshotError(message)
