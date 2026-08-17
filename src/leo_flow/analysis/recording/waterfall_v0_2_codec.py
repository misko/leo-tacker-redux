"""Canonical, bounded JSON codec for waterfall bundle v0.2."""

from __future__ import annotations

import json

from leo_flow.contracts.core import (
    AnalysisRunId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.waterfall import WaterfallProductId
from leo_flow.contracts.waterfall_v0_2 import (
    MAX_WATERFALL_V0_2_JSON_BYTES,
    V0_2,
    WaterfallBundleV0_2,
    WaterfallCoverageV0_2,
    WaterfallTileV0_2,
    WaterfallTimeBinV0_2,
)

from .waterfall_codec import (
    MalformedWaterfallError,
    _array,
    _digest,
    _integer,
    _keys,
    _number,
    _object,
    _provenance,
    _schema,
    _string,
    _unique_object,
)

WATERFALL_V0_2_MEDIA_TYPE = "application/json"
WATERFALL_V0_2_FORMAT_ID = "waterfall-bundle-v0.2"
MAX_WATERFALL_V0_2_BUNDLE_BYTES = MAX_WATERFALL_V0_2_JSON_BYTES


def encode_waterfall_bundle_v0_2(bundle: WaterfallBundleV0_2) -> bytes:
    payload = canonical_json_bytes(bundle)
    if len(payload) > MAX_WATERFALL_V0_2_BUNDLE_BYTES:
        raise MalformedWaterfallError("waterfall v0.2 bundle exceeds size limit")
    return payload


def decode_waterfall_bundle_v0_2(data: bytes) -> WaterfallBundleV0_2:
    if len(data) > MAX_WATERFALL_V0_2_BUNDLE_BYTES:
        raise MalformedWaterfallError("waterfall v0.2 bundle exceeds size limit")
    try:
        document = json.loads(data, object_pairs_hook=_unique_object)
        if canonical_json_bytes(document) != data:
            raise MalformedWaterfallError("waterfall v0.2 bytes are not canonical JSON")
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
        if schema != SchemaRef(WaterfallBundleV0_2.SCHEMA_ID, V0_2):
            raise MalformedWaterfallError("unsupported durable waterfall v0.2 schema")
        return WaterfallBundleV0_2(
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


def _tile(value: object, index: int) -> WaterfallTileV0_2:
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
            "fft_hop_samples",
            "display_frequency_bins",
            "power_reference",
            "high_percentile",
            "frequency_bin_offsets_hz",
            "coverage",
            "time_bins",
        },
        name,
    )
    return WaterfallTileV0_2(
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
        fft_hop_samples=_integer(item["fft_hop_samples"], f"{name}.fft_hop_samples"),
        display_frequency_bins=_integer(
            item["display_frequency_bins"], f"{name}.display_frequency_bins"
        ),
        power_reference=_string(item["power_reference"], f"{name}.power_reference"),
        high_percentile=_number(item["high_percentile"], f"{name}.high_percentile"),
        frequency_bin_offsets_hz=tuple(
            _number(entry, f"{name}.frequency_bin_offsets_hz")
            for entry in _array(
                item["frequency_bin_offsets_hz"], f"{name}.frequency_bin_offsets_hz"
            )
        ),
        coverage=_coverage(item["coverage"], name),
        time_bins=tuple(
            _time_bin(entry, name, row_index)
            for row_index, entry in enumerate(
                _array(item["time_bins"], f"{name}.time_bins")
            )
        ),
    )


def _coverage(value: object, tile_name: str) -> WaterfallCoverageV0_2:
    name = f"{tile_name}.coverage"
    item = _object(value, name)
    _keys(
        item,
        {
            "contiguous_rf_span_count",
            "contiguous_rf_sample_count",
            "analyzed_sample_count",
            "discarded_tail_sample_count",
            "fft_frame_count",
            "coverage_fraction",
        },
        name,
    )
    return WaterfallCoverageV0_2(
        contiguous_rf_span_count=_integer(
            item["contiguous_rf_span_count"], f"{name}.contiguous_rf_span_count"
        ),
        contiguous_rf_sample_count=_integer(
            item["contiguous_rf_sample_count"], f"{name}.contiguous_rf_sample_count"
        ),
        analyzed_sample_count=_integer(
            item["analyzed_sample_count"], f"{name}.analyzed_sample_count"
        ),
        discarded_tail_sample_count=_integer(
            item["discarded_tail_sample_count"], f"{name}.discarded_tail_sample_count"
        ),
        fft_frame_count=_integer(item["fft_frame_count"], f"{name}.fft_frame_count"),
        coverage_fraction=_number(
            item["coverage_fraction"], f"{name}.coverage_fraction"
        ),
    )


def _time_bin(value: object, tile_name: str, index: int) -> WaterfallTimeBinV0_2:
    name = f"{tile_name}.time_bins[{index}]"
    item = _object(value, name)
    _keys(
        item,
        {
            "start_sample",
            "stop_sample",
            "midpoint_utc_ns",
            "analyzed_sample_count",
            "fft_frame_count",
            "fft_frame_start_samples",
            "average_power_db",
            "temporal_median_residual_db",
            "high_percentile_power_db",
        },
        name,
    )
    return WaterfallTimeBinV0_2(
        start_sample=_integer(item["start_sample"], f"{name}.start_sample"),
        stop_sample=_integer(item["stop_sample"], f"{name}.stop_sample"),
        midpoint_utc_ns=UtcNs(
            _integer(item["midpoint_utc_ns"], f"{name}.midpoint_utc_ns")
        ),
        analyzed_sample_count=_integer(
            item["analyzed_sample_count"], f"{name}.analyzed_sample_count"
        ),
        fft_frame_count=_integer(item["fft_frame_count"], f"{name}.fft_frame_count"),
        fft_frame_start_samples=tuple(
            _integer(entry, f"{name}.fft_frame_start_samples")
            for entry in _array(
                item["fft_frame_start_samples"], f"{name}.fft_frame_start_samples"
            )
        ),
        average_power_db=_float_tuple(item["average_power_db"], name),
        temporal_median_residual_db=_float_tuple(
            item["temporal_median_residual_db"], name
        ),
        high_percentile_power_db=_float_tuple(item["high_percentile_power_db"], name),
    )


def _float_tuple(value: object, name: str) -> tuple[float, ...]:
    return tuple(_number(entry, name) for entry in _array(value, name))
