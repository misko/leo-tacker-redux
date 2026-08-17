"""Strict canonical codecs for the isolated 36-slot canary authority."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from leo_flow.contracts.core import (
    Digest,
    DigestAlgorithm,
    RadioId,
    UtcNs,
    canonical_json_bytes,
)

from .supercycle_canary import (
    CANARY_RECEIPT_SCHEMA,
    CANARY_SCHEMA,
    CanaryStageBenchmark,
    SupercycleCanaryDefinition,
    SupercycleCanaryReceipt,
)


def encode_canary_definition(value: SupercycleCanaryDefinition) -> bytes:
    return canonical_json_bytes(value.document())


def decode_canary_definition(encoded: bytes) -> SupercycleCanaryDefinition:
    root = _mapping(json.loads(encoded), "canary definition")
    if set(root) != set(_DEFINITION_FIELDS) or root.get("schema") != CANARY_SCHEMA:
        raise ValueError("canary definition schema or fields differ")
    radios = _pair(root["radios"], "canary radios")
    stations = _pair(root["station_digests"], "canary stations")
    result = SupercycleCanaryDefinition(
        _string(root["canary_id"]),
        UtcNs(_integer(root["start_utc_ns"])),
        RadioId(_string(radios[0])),
        RadioId(_string(radios[1])),
        _digest(stations[0]),
        _digest(stations[1]),
        _integer(root["maximum_start_lateness_ns"]),
        _digest(root["qualification_receipt_digest"]),
    )
    if encode_canary_definition(result) != encoded:
        raise ValueError("canary definition is not canonical or policy-exact")
    return result


def encode_canary_receipt(value: SupercycleCanaryReceipt) -> bytes:
    return canonical_json_bytes(value.document())


def decode_canary_receipt(encoded: bytes) -> SupercycleCanaryReceipt:
    root = _mapping(json.loads(encoded), "canary receipt")
    if set(root) != set(_RECEIPT_FIELDS) or root.get("schema") != CANARY_RECEIPT_SCHEMA:
        raise ValueError("canary receipt schema or fields differ")
    closure = _mapping(root["closure"], "canary closure")
    if set(closure) != {
        "feature_set_count",
        "waterfall_count",
        "starlink_suite_terminal_count",
        "dashboard_recording_count",
    }:
        raise ValueError("canary closure fields differ")
    benchmarks = _list(root["benchmarks"], "canary benchmarks")
    result = SupercycleCanaryReceipt(
        _digest(root["definition_digest"]),
        _digest(root["qualification_receipt_digest"]),
        UtcNs(_integer(root["issued_utc_ns"])),
        _digests(root["unit_digests"], "unit digests"),
        _digests(root["snapshot_digests"], "snapshot digests"),
        _digests(root["analysis_receipt_digests"], "analysis receipt digests"),
        tuple(_string(item) for item in _list(root["recording_ids"], "recordings")),
        _integers(root["capture_completion_latency_ns"], "capture latencies"),
        _integers(root["observed_skew_ns"], "observed skews"),
        tuple(_benchmark(item) for item in benchmarks),
        _integer(closure["feature_set_count"]),
        _integer(closure["waterfall_count"]),
        _integer(closure["starlink_suite_terminal_count"]),
        _integer(closure["dashboard_recording_count"]),
    )
    if encode_canary_receipt(result) != encoded:
        raise ValueError("canary receipt is not canonical or policy-exact")
    return result


_DEFINITION_FIELDS = (
    "schema",
    "canary_id",
    "campaign_kind",
    "authorization_scope",
    "main_campaign_authorized",
    "start_utc_ns",
    "radios",
    "station_digests",
    "qualification_receipt_digest",
    "slots",
    "recordings",
    "cells",
    "geometry_schedule",
    "unit_schedule",
    "slot_period_numerator_ns",
    "slot_period_denominator",
    "preflight_lead_ns",
    "maximum_start_lateness_ns",
    "maximum_observed_start_skew_ns",
    "hardware_block_duration_ms",
    "capture_mode",
    "capture_first",
    "no_catch_up",
    "replay_allowed",
    "raw_bytes",
    "capture_transition_limit",
    "analysis_transition_limit",
    "staged_analysis",
    "result_semantics",
)

_RECEIPT_FIELDS = (
    "schema",
    "authorization_scope",
    "main_campaign_authorized",
    "definition_digest",
    "qualification_receipt_digest",
    "issued_utc_ns",
    "unit_digests",
    "snapshot_digests",
    "analysis_receipt_digests",
    "recording_ids",
    "capture_completion_latency_ns",
    "observed_skew_ns",
    "maximum_observed_skew_ns",
    "benchmarks",
    "closure",
    "result_semantics",
)


def _benchmark(value: object) -> CanaryStageBenchmark:
    root = _mapping(value, "canary benchmark")
    if set(root) != {
        "stage",
        "workers",
        "wall_time_ns",
        "cpu_time_ns",
        "peak_rss_bytes",
    }:
        raise ValueError("canary benchmark fields differ")
    return CanaryStageBenchmark(
        _string(root["stage"]),
        _integer(root["workers"]),
        _integer(root["wall_time_ns"]),
        _integer(root["cpu_time_ns"]),
        _integer(root["peak_rss_bytes"]),
    )


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object")
    return value


def _list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"{name} must be an array")
    return value


def _pair(value: object, name: str) -> tuple[object, object]:
    items = _list(value, name)
    if len(items) != 2:
        raise ValueError(f"{name} must contain two values")
    return items[0], items[1]


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("expected string")
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("expected integer")
    return value


def _integers(value: object, name: str) -> tuple[int, ...]:
    return tuple(_integer(item) for item in _list(value, name))


def _digests(value: object, name: str) -> tuple[Digest, ...]:
    return tuple(_digest(item) for item in _list(value, name))


def _digest(value: object) -> Digest:
    text = _string(value)
    prefix = f"{DigestAlgorithm.SHA256.value}:"
    if not text.startswith(prefix):
        raise ValueError("expected sha256 digest")
    return Digest(DigestAlgorithm.SHA256, text.removeprefix(prefix))
