"""Bounded read-only adapter for the atomically replaced canary receipt."""

from __future__ import annotations

import json
import math
import os
import stat
from pathlib import Path
from typing import Any, cast

from leo_flow.contracts.core import (
    V0_1,
    Digest,
    DigestAlgorithm,
    SchemaRef,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.dashboard_retro_qam_canary import (
    RetroQamCanaryDashboardViewV0_1,
    RetroQamCanaryReceiverViewV0_1,
)

MAXIMUM_CANARY_RECEIPT_BYTES = 64 * 1024
_REQUIRED_REASONS = {
    "known-published-pilot-regression",
    "candidate-evidence-not-calibrated-detection",
    "leo-tracker-oracle-not-runtime-dependency",
    "whole-input-sha256-verified-before-analysis",
}


class FileRetroQamCanaryDashboardQueryV0_1:
    def __init__(self, receipt_path: Path) -> None:
        if not receipt_path.is_absolute() or ".." in receipt_path.parts:
            raise ValueError("canary receipt path must be absolute and resolved")
        self._receipt_path = receipt_path

    def latest_retro_qam_canary(self) -> RetroQamCanaryDashboardViewV0_1:
        payload = _read_bounded_regular_file(self._receipt_path)
        canonical = payload[:-1] if payload.endswith(b"\n") else payload
        try:
            document = cast(dict[str, Any], json.loads(canonical))
        except (TypeError, ValueError) as error:
            raise ValueError("canary receipt is not JSON") from error
        if canonical_json_bytes(document) != canonical:
            raise ValueError("canary receipt is not canonical")
        schema = _mapping(document.get("schema"), "schema")
        version = _mapping(schema.get("version"), "schema version")
        if schema.get(
            "schema_id"
        ) != "org.leo-flow.starlink-retro-qam-canary-receipt" or version != {
            "major": 0,
            "minor": 1,
        }:
            raise ValueError("unsupported canary receipt schema")
        if (
            document.get("candidate_only") is not True
            or document.get("calibrated_detection") is not None
            or not _REQUIRED_REASONS <= set(_strings(document.get("reason_codes")))
        ):
            raise ValueError("canary receipt safety semantics differ")
        receivers = tuple(
            _receiver(_mapping(item, "receiver"))
            for item in _list(document.get("receivers"), "receivers")
        )
        if len(receivers) != 2:
            raise ValueError("canary receipt must contain two receivers")
        combined = _mapping(document.get("combined"), "combined")
        combined_accuracy = _number(
            combined.get("hard_symbol_accuracy"), "combined accuracy"
        )
        combined_evm = _number(combined.get("rms_evm"), "combined EVM")
        return RetroQamCanaryDashboardViewV0_1(
            SchemaRef(RetroQamCanaryDashboardViewV0_1.SCHEMA_ID, V0_1),
            _string(document.get("corpus_id"), "corpus_id"),
            Digest.sha256(canonical),
            _digest(document.get("iq_object_digest"), "IQ object digest"),
            _string(document.get("git_commit"), "git_commit"),
            UtcNs(_integer(document.get("completed_utc_ns"), "completed_utc_ns")),
            1800,
            _boolean(document.get("metrics_match_oracle"), "metrics_match_oracle"),
            combined_accuracy,
            combined_evm,
            qam_goodness_v0_2(combined_accuracy, combined_evm),
            receivers,
            True,
            None,
            (
                "historical-acceptance-canary-not-live-recording",
                "known-published-pilot-regression",
                "candidate-evidence-not-calibrated-detection",
                "leo-tracker-oracle-not-runtime-dependency",
            ),
        )


def qam_goodness_v0_2(accuracy: float, rms_evm: float) -> float:
    chance_corrected = max(0.0, min(1.0, (accuracy - 0.25) / 0.75))
    compactness = 1.0 / (1.0 + (rms_evm / 2.0) ** 2)
    return math.sqrt(chance_corrected * compactness)


def _receiver(document: dict[str, Any]) -> RetroQamCanaryReceiverViewV0_1:
    accuracy = _number(document.get("hard_symbol_accuracy"), "receiver accuracy")
    evm = _number(document.get("rms_evm"), "receiver EVM")
    return RetroQamCanaryReceiverViewV0_1(
        _integer(document.get("receiver_index"), "receiver index"),
        _integer(document.get("winning_epoch_sample"), "winning epoch"),
        _number(document.get("winning_cfo_hz"), "winning CFO"),
        _number(document.get("held_out_verify_score"), "held-out score"),
        _number(document.get("conditioned_control_score"), "control score"),
        _number(document.get("verify_minus_control_margin"), "score margin"),
        accuracy,
        evm,
        qam_goodness_v0_2(accuracy, evm),
    )


def _read_bounded_regular_file(path: Path) -> bytes:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or not 0 < details.st_size <= MAXIMUM_CANARY_RECEIPT_BYTES
        ):
            raise ValueError("canary receipt is not a bounded regular file")
        payload = os.read(descriptor, details.st_size + 1)
        if len(payload) != details.st_size:
            raise ValueError("canary receipt changed during read")
        return payload
    finally:
        os.close(descriptor)


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be an object")
    return cast(dict[str, Any], value)


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"{label} must be an array")
    return cast(list[object], value)


def _strings(value: object) -> tuple[str, ...]:
    values = _list(value, "string values")
    if any(not isinstance(item, str) for item in values):
        raise ValueError("string values contain another type")
    return tuple(cast(str, item) for item in values)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{label} must be a boolean")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _digest(value: object, label: str) -> Digest:
    document = _mapping(value, label)
    if document.get("algorithm") != "sha256":
        raise ValueError(f"{label} must use sha256")
    return Digest(DigestAlgorithm.SHA256, _string(document.get("value"), label))
