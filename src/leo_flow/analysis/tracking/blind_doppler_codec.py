"""Strict canonical codec for blind Doppler candidate bundles v0.1."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, NoReturn

from leo_flow.contracts.blind_doppler import (
    BlindDopplerBundleV0_1,
    BlindDopplerCandidateV0_1,
    DopplerPolynomialFitV0_1,
    DopplerPolynomialOrder,
    DopplerTrackPointV0_1,
    StationaryControlEvidenceV0_1,
)
from leo_flow.contracts.core import (
    Digest,
    DigestAlgorithm,
    SchemaRef,
    SchemaVersion,
    UtcNs,
    canonical_json_bytes,
)

MAX_BLIND_DOPPLER_BUNDLE_BYTES = 8 * 1024 * 1024
BLIND_DOPPLER_MEDIA_TYPE = "application/json"
BLIND_DOPPLER_FORMAT_ID = "blind-doppler-bundle-v0.1"


class MalformedBlindDopplerError(ValueError):
    """Bundle bytes are oversized, noncanonical, ambiguous, or invalid."""


def encode_blind_doppler_bundle(bundle: BlindDopplerBundleV0_1) -> bytes:
    payload = canonical_json_bytes(bundle)
    if len(payload) > MAX_BLIND_DOPPLER_BUNDLE_BYTES:
        raise MalformedBlindDopplerError("blind Doppler bundle exceeds size limit")
    return payload


def decode_blind_doppler_bundle(data: bytes) -> BlindDopplerBundleV0_1:
    if len(data) > MAX_BLIND_DOPPLER_BUNDLE_BYTES:
        raise MalformedBlindDopplerError("blind Doppler bundle exceeds size limit")
    try:
        document = json.loads(data, object_pairs_hook=_unique_object)
        if canonical_json_bytes(document) != data:
            _bad("blind Doppler bytes are not canonical JSON")
        root = _object(document, "root")
        _keys(
            root,
            {
                "schema",
                "input_identity_digest",
                "config_digest",
                "algorithm_version",
                "candidate_only",
                "examined_row_count",
                "extracted_peak_count",
                "candidates",
                "warnings",
                "reason_codes",
            },
            "root",
        )
        schema = _schema(root["schema"], "schema")
        if schema != SchemaRef(BlindDopplerBundleV0_1.SCHEMA_ID):
            _bad("unsupported durable blind Doppler schema")
        return BlindDopplerBundleV0_1(
            schema=schema,
            input_identity_digest=_digest(
                root["input_identity_digest"], "input_identity_digest"
            ),
            config_digest=_digest(root["config_digest"], "config_digest"),
            algorithm_version=_string(root["algorithm_version"], "algorithm_version"),
            candidate_only=_boolean(root["candidate_only"], "candidate_only"),
            examined_row_count=_integer(
                root["examined_row_count"], "examined_row_count"
            ),
            extracted_peak_count=_integer(
                root["extracted_peak_count"], "extracted_peak_count"
            ),
            candidates=tuple(
                _candidate(item, index)
                for index, item in enumerate(_array(root["candidates"], "candidates"))
            ),
            warnings=tuple(
                _string(item, "warning")
                for item in _array(root["warnings"], "warnings")
            ),
            reason_codes=tuple(
                _string(item, "reason_code")
                for item in _array(root["reason_codes"], "reason_codes")
            ),
        )
    except MalformedBlindDopplerError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise MalformedBlindDopplerError(str(error)) from error


def _candidate(value: object, index: int) -> BlindDopplerCandidateV0_1:
    name = f"candidates[{index}]"
    item = _object(value, name)
    _keys(
        item,
        {
            "rank",
            "component_id",
            "points",
            "fits",
            "selected_order",
            "stationary_control",
            "mean_spectral_peak_excess_db",
            "peak_layer_value_db",
            "duration_s",
            "missing_row_count",
            "missing_row_fraction",
            "edge_truncated_point_count",
            "ranking_score",
        },
        name,
    )
    return BlindDopplerCandidateV0_1(
        rank=_integer(item["rank"], f"{name}.rank"),
        component_id=_integer(item["component_id"], f"{name}.component_id"),
        points=tuple(
            _point(entry, name, point_index)
            for point_index, entry in enumerate(
                _array(item["points"], f"{name}.points")
            )
        ),
        fits=tuple(
            _fit(entry, name, fit_index)
            for fit_index, entry in enumerate(_array(item["fits"], f"{name}.fits"))
        ),
        selected_order=_order(item["selected_order"], f"{name}.selected_order"),
        stationary_control=_control(item["stationary_control"], name),
        mean_spectral_peak_excess_db=_number(
            item["mean_spectral_peak_excess_db"],
            f"{name}.mean_spectral_peak_excess_db",
        ),
        peak_layer_value_db=_number(
            item["peak_layer_value_db"], f"{name}.peak_layer_value_db"
        ),
        duration_s=_number(item["duration_s"], f"{name}.duration_s"),
        missing_row_count=_integer(
            item["missing_row_count"], f"{name}.missing_row_count"
        ),
        missing_row_fraction=_number(
            item["missing_row_fraction"], f"{name}.missing_row_fraction"
        ),
        edge_truncated_point_count=_integer(
            item["edge_truncated_point_count"], f"{name}.edge_truncated_point_count"
        ),
        ranking_score=_number(item["ranking_score"], f"{name}.ranking_score"),
    )


def _point(value: object, parent: str, index: int) -> DopplerTrackPointV0_1:
    name = f"{parent}.points[{index}]"
    item = _object(value, name)
    fields = {
        "row_index",
        "midpoint_utc_ns",
        "frequency_hz",
        "interpolated_bin",
        "layer_value_db",
        "row_baseline_db",
        "local_peak_excess_db",
        "edge_truncated",
    }
    _keys(item, fields, name)
    return DopplerTrackPointV0_1(
        row_index=_integer(item["row_index"], f"{name}.row_index"),
        midpoint_utc_ns=UtcNs(
            _integer(item["midpoint_utc_ns"], f"{name}.midpoint_utc_ns")
        ),
        frequency_hz=_number(item["frequency_hz"], f"{name}.frequency_hz"),
        interpolated_bin=_number(item["interpolated_bin"], f"{name}.interpolated_bin"),
        layer_value_db=_number(item["layer_value_db"], f"{name}.layer_value_db"),
        row_baseline_db=_number(item["row_baseline_db"], f"{name}.row_baseline_db"),
        local_peak_excess_db=_number(
            item["local_peak_excess_db"], f"{name}.local_peak_excess_db"
        ),
        edge_truncated=_boolean(item["edge_truncated"], f"{name}.edge_truncated"),
    )


def _fit(value: object, parent: str, index: int) -> DopplerPolynomialFitV0_1:
    name = f"{parent}.fits[{index}]"
    item = _object(value, name)
    _keys(
        item,
        {
            "order",
            "reference_utc_ns",
            "frequency_hz",
            "drift_rate_hz_s",
            "drift_acceleration_hz_s2",
            "residual_rms_hz",
            "robust_scale_hz",
            "inlier_count",
            "bic",
        },
        name,
    )
    return DopplerPolynomialFitV0_1(
        order=_order(item["order"], f"{name}.order"),
        reference_utc_ns=UtcNs(
            _integer(item["reference_utc_ns"], f"{name}.reference_utc_ns")
        ),
        frequency_hz=_number(item["frequency_hz"], f"{name}.frequency_hz"),
        drift_rate_hz_s=_number(item["drift_rate_hz_s"], f"{name}.drift_rate_hz_s"),
        drift_acceleration_hz_s2=_number(
            item["drift_acceleration_hz_s2"], f"{name}.drift_acceleration_hz_s2"
        ),
        residual_rms_hz=_number(item["residual_rms_hz"], f"{name}.residual_rms_hz"),
        robust_scale_hz=_number(item["robust_scale_hz"], f"{name}.robust_scale_hz"),
        inlier_count=_integer(item["inlier_count"], f"{name}.inlier_count"),
        bic=_number(item["bic"], f"{name}.bic"),
    )


def _control(value: object, parent: str) -> StationaryControlEvidenceV0_1:
    name = f"{parent}.stationary_control"
    item = _object(value, name)
    _keys(
        item,
        {
            "constant_residual_rms_hz",
            "selected_residual_rms_hz",
            "residual_improvement_fraction",
            "bic_margin_over_constant",
            "moving_model_preferred",
        },
        name,
    )
    return StationaryControlEvidenceV0_1(
        constant_residual_rms_hz=_number(
            item["constant_residual_rms_hz"], f"{name}.constant_residual_rms_hz"
        ),
        selected_residual_rms_hz=_number(
            item["selected_residual_rms_hz"], f"{name}.selected_residual_rms_hz"
        ),
        residual_improvement_fraction=_number(
            item["residual_improvement_fraction"],
            f"{name}.residual_improvement_fraction",
        ),
        bic_margin_over_constant=_number(
            item["bic_margin_over_constant"], f"{name}.bic_margin_over_constant"
        ),
        moving_model_preferred=_boolean(
            item["moving_model_preferred"], f"{name}.moving_model_preferred"
        ),
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


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        _bad(f"{name} must be a boolean")
    return value


def _order(value: object, name: str) -> DopplerPolynomialOrder:
    return DopplerPolynomialOrder(_integer(value, name))


def _bad(message: str) -> NoReturn:
    raise MalformedBlindDopplerError(message)
