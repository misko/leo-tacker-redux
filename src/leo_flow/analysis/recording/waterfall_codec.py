"""Canonical bounded codec for waterfall bundle v0.1."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, NoReturn

from leo_flow.contracts.core import (
    AnalysisRunId,
    Digest,
    DigestAlgorithm,
    Provenance,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SchemaVersion,
    SegmentId,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.waterfall import (
    WaterfallBundleV0_1,
    WaterfallProductId,
    WaterfallTileV0_1,
    WaterfallTimeBinV0_1,
)

MAX_WATERFALL_BUNDLE_BYTES = 4 * 1024 * 1024
WATERFALL_MEDIA_TYPE = "application/json"
WATERFALL_FORMAT_ID = "waterfall-bundle-v0.1"


class MalformedWaterfallError(ValueError):
    """Waterfall bytes are oversized, noncanonical, ambiguous, or invalid."""


def encode_waterfall_bundle(bundle: WaterfallBundleV0_1) -> bytes:
    payload = canonical_json_bytes(bundle)
    if len(payload) > MAX_WATERFALL_BUNDLE_BYTES:
        raise MalformedWaterfallError("waterfall bundle exceeds size limit")
    return payload


def decode_waterfall_bundle(data: bytes) -> WaterfallBundleV0_1:
    if len(data) > MAX_WATERFALL_BUNDLE_BYTES:
        raise MalformedWaterfallError("waterfall bundle exceeds size limit")
    try:
        document = json.loads(data, object_pairs_hook=_unique_object)
        if canonical_json_bytes(document) != data:
            _bad("waterfall bytes are not canonical JSON")
        root = _object(document, "root")
        _keys(
            root,
            {
                "schema",
                "product_id",
                "analysis_run_id",
                "recording_id",
                "input_recording_identity_digest",
                "provenance",
                "tiles",
                "warnings",
                "reason_codes",
            },
            "root",
        )
        schema = _schema(root["schema"], "schema")
        if schema != SchemaRef(WaterfallBundleV0_1.SCHEMA_ID):
            _bad("unsupported durable waterfall schema")
        return WaterfallBundleV0_1(
            schema=schema,
            product_id=WaterfallProductId(_string(root["product_id"], "product_id")),
            analysis_run_id=AnalysisRunId(
                _string(root["analysis_run_id"], "analysis_run_id")
            ),
            recording_id=RecordingId(_string(root["recording_id"], "recording_id")),
            input_recording_identity_digest=_digest(
                root["input_recording_identity_digest"], "input_recording_digest"
            ),
            provenance=_provenance(root["provenance"]),
            tiles=tuple(
                _tile(value, index)
                for index, value in enumerate(_array(root["tiles"], "tiles"))
            ),
            warnings=tuple(
                _string(value, "warning")
                for value in _array(root["warnings"], "warnings")
            ),
            reason_codes=tuple(
                _string(value, "reason_code")
                for value in _array(root["reason_codes"], "reason_codes")
            ),
        )
    except MalformedWaterfallError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise MalformedWaterfallError(str(error)) from error


def _tile(value: object, index: int) -> WaterfallTileV0_1:
    name = f"tiles[{index}]"
    item = _object(value, name)
    _keys(
        item,
        {
            "segment_id",
            "receiver_chain_id",
            "segment_start_utc_ns",
            "segment_sample_count",
            "center_frequency_hz",
            "sample_rate_hz",
            "fft_window_samples",
            "power_reference",
            "frequency_bin_offsets_hz",
            "time_bins",
        },
        name,
    )
    return WaterfallTileV0_1(
        segment_id=SegmentId(_string(item["segment_id"], f"{name}.segment_id")),
        receiver_chain_id=ReceiverChainId(
            _string(item["receiver_chain_id"], f"{name}.receiver_chain_id")
        ),
        segment_start_utc_ns=UtcNs(
            _integer(item["segment_start_utc_ns"], f"{name}.segment_start_utc_ns")
        ),
        segment_sample_count=_integer(
            item["segment_sample_count"], f"{name}.segment_sample_count"
        ),
        center_frequency_hz=_number(
            item["center_frequency_hz"], f"{name}.center_frequency_hz"
        ),
        sample_rate_hz=_number(item["sample_rate_hz"], f"{name}.sample_rate_hz"),
        fft_window_samples=_integer(
            item["fft_window_samples"], f"{name}.fft_window_samples"
        ),
        power_reference=_string(item["power_reference"], f"{name}.power_reference"),
        frequency_bin_offsets_hz=tuple(
            _number(entry, f"{name}.frequency_bin_offsets_hz")
            for entry in _array(
                item["frequency_bin_offsets_hz"],
                f"{name}.frequency_bin_offsets_hz",
            )
        ),
        time_bins=tuple(
            _time_bin(entry, name, row_index)
            for row_index, entry in enumerate(
                _array(item["time_bins"], f"{name}.time_bins")
            )
        ),
    )


def _time_bin(value: object, tile_name: str, index: int) -> WaterfallTimeBinV0_1:
    name = f"{tile_name}.time_bins[{index}]"
    item = _object(value, name)
    _keys(item, {"start_sample", "stop_sample", "midpoint_utc_ns", "power_db"}, name)
    return WaterfallTimeBinV0_1(
        start_sample=_integer(item["start_sample"], f"{name}.start_sample"),
        stop_sample=_integer(item["stop_sample"], f"{name}.stop_sample"),
        midpoint_utc_ns=UtcNs(
            _integer(item["midpoint_utc_ns"], f"{name}.midpoint_utc_ns")
        ),
        power_db=tuple(
            _number(entry, f"{name}.power_db")
            for entry in _array(item["power_db"], f"{name}.power_db")
        ),
    )


def _provenance(value: object) -> Provenance:
    item = _object(value, "provenance")
    _keys(
        item,
        {
            "producer_name",
            "producer_version",
            "git_commit",
            "environment_digest",
            "normalized_config_digest",
            "input_digests",
            "dependency_digests",
            "started_utc_ns",
            "completed_utc_ns",
            "host_class",
        },
        "provenance",
    )
    return Provenance(
        producer_name=_string(item["producer_name"], "producer_name"),
        producer_version=_string(item["producer_version"], "producer_version"),
        git_commit=_string(item["git_commit"], "git_commit"),
        environment_digest=_digest(item["environment_digest"], "environment_digest"),
        normalized_config_digest=_digest(
            item["normalized_config_digest"], "normalized_config_digest"
        ),
        input_digests=tuple(
            _digest(entry, "input_digest")
            for entry in _array(item["input_digests"], "input_digests")
        ),
        dependency_digests=tuple(
            _digest(entry, "dependency_digest")
            for entry in _array(item["dependency_digests"], "dependency_digests")
        ),
        started_utc_ns=UtcNs(_integer(item["started_utc_ns"], "started_utc_ns")),
        completed_utc_ns=UtcNs(_integer(item["completed_utc_ns"], "completed_utc_ns")),
        host_class=_string(item["host_class"], "host_class"),
    )


def _schema(value: object, name: str) -> SchemaRef:
    item = _object(value, name)
    _keys(item, {"schema_id", "version"}, name)
    version = _object(item["version"], f"{name}.version")
    _keys(version, {"major", "minor"}, f"{name}.version")
    return SchemaRef(
        _string(item["schema_id"], f"{name}.schema_id"),
        SchemaVersion(
            _integer(version["major"], f"{name}.version.major"),
            _integer(version["minor"], f"{name}.version.minor"),
        ),
    )


def _digest(value: object, name: str) -> Digest:
    item = _object(value, name)
    _keys(item, {"algorithm", "value"}, name)
    return Digest(
        DigestAlgorithm(_string(item["algorithm"], f"{name}.algorithm")),
        _string(item["value"], f"{name}.value"),
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _bad(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _object(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        _bad(f"{name} must be an object")
    return value


def _array(value: object, name: str) -> list[Any]:
    if not isinstance(value, list):
        _bad(f"{name} must be an array")
    return value


def _keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        _bad(f"{name} fields differ from the schema")


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        _bad(f"{name} must be a string")
    return value


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _bad(f"{name} must be an integer")
    return value


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _bad(f"{name} must be a number")
    return float(value)


def _bad(message: str) -> NoReturn:
    raise MalformedWaterfallError(message)
