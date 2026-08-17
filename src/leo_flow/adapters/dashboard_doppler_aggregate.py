"""Read immutable Doppler bundles into a bounded aggregate dashboard view."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Callable, Iterable

import psycopg
from psycopg.rows import dict_row

from leo_flow.analysis.tracking.blind_doppler_codec import (
    BLIND_DOPPLER_FORMAT_ID,
    BLIND_DOPPLER_MEDIA_TYPE,
    MAX_BLIND_DOPPLER_BUNDLE_BYTES,
    decode_blind_doppler_bundle,
)
from leo_flow.analysis.tracking.doppler_persistence import (
    ADVANCED_DOPPLER_FORMAT_ID,
    ADVANCED_DOPPLER_MEDIA_TYPE,
    MAX_ADVANCED_DOPPLER_BUNDLE_BYTES,
    decode_advanced_doppler_bundle,
)
from leo_flow.contracts.blind_doppler import (
    BlindDopplerBundleV0_1,
    DopplerPolynomialOrder,
)
from leo_flow.contracts.core import Digest, DigestAlgorithm, UtcNs
from leo_flow.contracts.dashboard_doppler_aggregate import (
    MAX_DOPPLER_AGGREGATE_CONTROL_POINTS,
    MAX_DOPPLER_AGGREGATE_POINTS,
    MAX_DOPPLER_AGGREGATE_SERIES,
    DopplerAggregateControlPointV0_1,
    DopplerAggregateQueryV0_1,
    DopplerAggregateSeriesV0_1,
    DopplerAggregateSummaryV0_1,
    DopplerAggregateTrackPointV0_1,
    DopplerAggregateViewV0_1,
)
from leo_flow.contracts.doppler_evidence import AdvancedDopplerEvidenceBundleV0_1
from leo_flow.contracts.storage import ObjectRef
from leo_flow.storage.ports import BlobReader

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]
MAXIMUM_AGGREGATE_TILES = 512
MAXIMUM_POINTS_PER_SERIES = 128
_CHANNEL = re.compile(r"(?:^|_)ch([0-9]+)(?:_|$)", re.IGNORECASE)


class DurableDashboardDopplerAggregateV0_1:
    def __init__(self, connect: ConnectionFactory, blobs: BlobReader) -> None:
        self._connect = connect
        self._blobs = blobs

    def doppler_aggregate(
        self, query: DopplerAggregateQueryV0_1
    ) -> DopplerAggregateViewV0_1:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                """SELECT *
                     FROM public.read_dashboard_doppler_aggregate_interval_count_v1(
                         %s,%s)""",
                (int(query.start_utc_ns), int(query.stop_utc_ns)),
            )
            count_row = cursor.fetchone()
            if count_row is None:
                raise ValueError("Doppler aggregate count routine returned no row")
            cursor.execute(
                """SELECT *
                     FROM public.read_dashboard_doppler_aggregate_interval_v1(
                         %s,%s,%s)""",
                (
                    int(query.start_utc_ns),
                    int(query.stop_utc_ns),
                    MAXIMUM_AGGREGATE_TILES + 1,
                ),
            )
            rows = cursor.fetchall()
        truncated = len(rows) > MAXIMUM_AGGREGATE_TILES
        rows = rows[:MAXIMUM_AGGREGATE_TILES]
        series: list[DopplerAggregateSeriesV0_1] = []
        controls: list[DopplerAggregateControlPointV0_1] = []
        recordings: set[str] = set()
        point_budget = MAX_DOPPLER_AGGREGATE_POINTS
        for row in rows:
            basic, advanced = self._read_bundles(row)
            if basic.input_identity_digest != advanced.input_identity_digest:
                raise ValueError("Doppler aggregate bundles have different inputs")
            recordings.add(str(row["recording_id"]))
            tile_series, tile_controls = _tile_evidence(row, basic, advanced, query)
            for item in tile_series:
                if len(series) >= MAX_DOPPLER_AGGREGATE_SERIES:
                    truncated = True
                    break
                if len(item.points) > point_budget:
                    truncated = True
                    continue
                series.append(item)
                point_budget -= len(item.points)
            remaining = MAX_DOPPLER_AGGREGATE_CONTROL_POINTS - len(controls)
            if len(tile_controls) > remaining:
                truncated = True
            controls.extend(tile_controls[:remaining])
        summaries = _summaries(series)
        interval_count = _integer(count_row["interval_recording_count"])
        available_count = _integer(count_row["available_recording_count"])
        return DopplerAggregateViewV0_1(
            1,
            query.start_utc_ns,
            query.stop_utc_ns,
            interval_count,
            len(rows),
            available_count,
            truncated,
            tuple(series),
            tuple(controls),
            summaries,
            (
                "advanced-path-bins-not-converted-to-physical-frequency",
                "candidate-only-evidence-not-satellite-detection",
                "overlapping-track-observations-are-not-independent",
                "radio-and-receiver-series-are-never-pooled",
            ),
        )

    def _read_bundles(
        self, row: dict[str, object]
    ) -> tuple[BlindDopplerBundleV0_1, AdvancedDopplerEvidenceBundleV0_1]:
        basic_ref = _object_ref(row, "basic")
        advanced_ref = _object_ref(row, "advanced")
        basic = decode_blind_doppler_bundle(
            self._read(
                basic_ref,
                BLIND_DOPPLER_MEDIA_TYPE,
                BLIND_DOPPLER_FORMAT_ID,
                MAX_BLIND_DOPPLER_BUNDLE_BYTES,
            )
        )
        advanced = decode_advanced_doppler_bundle(
            self._read(
                advanced_ref,
                ADVANCED_DOPPLER_MEDIA_TYPE,
                ADVANCED_DOPPLER_FORMAT_ID,
                MAX_ADVANCED_DOPPLER_BUNDLE_BYTES,
            )
        )
        spectrogram_digest = _digest(row, "spectrogram")
        if (
            basic.input_identity_digest != spectrogram_digest
            or advanced.input_identity_digest != spectrogram_digest
            or basic.config_digest != _digest(row, "basic_config")
            or advanced.config_digest != _digest(row, "advanced_config")
            or advanced.blind_bundle_digest != basic_ref.digest
        ):
            raise ValueError("Doppler aggregate bundles disagree with catalog identity")
        return basic, advanced

    def _read(
        self, ref: ObjectRef, media_type: str, format_id: str, maximum: int
    ) -> bytes:
        if (
            ref.media_type != media_type
            or ref.format_id != format_id
            or ref.byte_count > maximum
        ):
            raise ValueError("Doppler aggregate object metadata is invalid")
        metadata = self._blobs.head(ref)
        if metadata.ref != ref or not metadata.verified:
            raise ValueError("Doppler aggregate object is not verified")
        with self._blobs.open(ref) as stream:
            payload = stream.read(maximum + 1)
        if len(payload) != ref.byte_count or Digest.sha256(payload) != ref.digest:
            raise ValueError("Doppler aggregate object bytes differ")
        return payload


def _tile_evidence(
    row: dict[str, object],
    basic: BlindDopplerBundleV0_1,
    advanced: AdvancedDopplerEvidenceBundleV0_1,
    query: DopplerAggregateQueryV0_1,
) -> tuple[list[DopplerAggregateSeriesV0_1], list[DopplerAggregateControlPointV0_1]]:
    recording_id = str(row["recording_id"])
    radio_id = str(row["radio_id"])
    receiver = str(row["receiver_chain_id"])
    segment = str(row["segment_id"])
    channel, edge = _channel_edge(segment)
    if not _common_filter(query, radio_id, receiver, channel, edge):
        return [], []
    doppler_id = str(row["doppler_id"])
    waterfall_product_id = str(row["waterfall_product_id"])
    basic_bundle_digest = str(_digest(row, "basic_bundle"))
    advanced_bundle_digest = str(_digest(row, "advanced_bundle"))
    started = UtcNs(_integer(row["started_utc_ns"]))
    result: list[DopplerAggregateSeriesV0_1] = []
    by_rank = {item.rank: item for item in basic.candidates}
    if (not query.methods or "basic" in query.methods) and (
        not query.association_states or "basic-candidate" in query.association_states
    ):
        for candidate in basic.candidates:
            fit = next(
                item
                for item in candidate.fits
                if item.order == candidate.selected_order
            )
            model = _model(candidate.selected_order)
            if query.models and model not in query.models:
                continue
            result.append(
                DopplerAggregateSeriesV0_1(
                    recording_id,
                    started,
                    radio_id,
                    receiver,
                    segment,
                    channel,
                    edge,
                    doppler_id,
                    waterfall_product_id,
                    f"{doppler_id}:basic:{candidate.rank}",
                    "basic",
                    basic.algorithm_version,
                    model,
                    "basic-candidate",
                    fit.reference_utc_ns,
                    fit.frequency_hz,
                    fit.drift_rate_hz_s,
                    candidate.ranking_score,
                    str(basic.input_identity_digest),
                    str(basic.config_digest),
                    basic_bundle_digest,
                    advanced_bundle_digest,
                    True,
                    _decimate_points(
                        tuple(
                            DopplerAggregateTrackPointV0_1(
                                point.midpoint_utc_ns,
                                (int(point.midpoint_utc_ns) - int(fit.reference_utc_ns))
                                / 1e9,
                                point.frequency_hz - fit.frequency_hz,
                            )
                            for point in candidate.points
                        )
                    ),
                )
            )
    controls: list[DopplerAggregateControlPointV0_1] = []
    slope = advanced.slope_bank
    association = advanced.association
    if slope is not None and association is not None:
        state = association.state
        matched = by_rank.get(association.basic_candidate_rank or -1)
        matched_fit = None
        if matched is not None:
            matched_fit = next(
                item for item in matched.fits if item.order == matched.selected_order
            )
        if (
            (not query.methods or "advanced" in query.methods)
            and (not query.models or "slope-bank" in query.models)
            and (not query.association_states or state in query.association_states)
        ):
            result.append(
                DopplerAggregateSeriesV0_1(
                    recording_id,
                    started,
                    radio_id,
                    receiver,
                    segment,
                    channel,
                    edge,
                    doppler_id,
                    waterfall_product_id,
                    str(association.candidate_path_digest),
                    "advanced",
                    advanced.algorithm_version,
                    "slope-bank",
                    state,
                    matched_fit.reference_utc_ns
                    if matched_fit is not None
                    else started,
                    matched_fit.frequency_hz if matched_fit is not None else None,
                    slope.track.drift_rate_hz_s,
                    slope.heldout_score,
                    str(advanced.input_identity_digest),
                    str(advanced.config_digest),
                    basic_bundle_digest,
                    advanced_bundle_digest,
                    True,
                    (),
                )
            )
            control_values: Iterable[tuple[str, float]] = (
                ("heldout-path", slope.heldout_score),
                ("stationary", slope.stationary_score),
                ("opposite-slope", slope.opposite_slope_score),
                *(("time-shuffle", score) for score in slope.time_shuffle_scores),
            )
            controls = [
                DopplerAggregateControlPointV0_1(
                    recording_id,
                    radio_id,
                    receiver,
                    segment,
                    str(association.candidate_path_digest),
                    kind,
                    score,
                )
                for kind, score in control_values
            ]
    return result, controls


def _common_filter(
    query: DopplerAggregateQueryV0_1,
    radio_id: str,
    receiver: str,
    channel: str,
    edge: str,
) -> bool:
    return not any(
        values and value not in values
        for values, value in (
            (query.radio_ids, radio_id),
            (query.receiver_chain_ids, receiver),
            (query.channels, channel),
            (query.edges, edge),
        )
    )


def _summaries(
    series: list[DopplerAggregateSeriesV0_1],
) -> tuple[DopplerAggregateSummaryV0_1, ...]:
    grouped: dict[tuple[str, str, str, str, str], list[float]] = defaultdict(list)
    for item in series:
        grouped[
            (
                item.radio_id,
                item.receiver_chain_id,
                item.method,
                item.model,
                item.association_state,
            )
        ].append(item.drift_rate_hz_s)
    return tuple(
        DopplerAggregateSummaryV0_1(
            *key,
            len(values),
            _quantile(values, 0.5),
            _quantile(values, 0.1),
            _quantile(values, 0.9),
        )
        for key, values in sorted(grouped.items())
    )


def _quantile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _decimate_points(
    points: tuple[DopplerAggregateTrackPointV0_1, ...],
) -> tuple[DopplerAggregateTrackPointV0_1, ...]:
    """Bound a track while preserving endpoints and frequency extrema."""
    if len(points) <= MAXIMUM_POINTS_PER_SERIES:
        return points
    required = {
        0,
        len(points) - 1,
        min(range(len(points)), key=lambda index: points[index].frequency_offset_hz),
        max(range(len(points)), key=lambda index: points[index].frequency_offset_hz),
    }
    remaining = MAXIMUM_POINTS_PER_SERIES - len(required)
    for index in range(remaining):
        required.add(round(index * (len(points) - 1) / max(remaining - 1, 1)))
    if len(required) < MAXIMUM_POINTS_PER_SERIES:
        for index in range(len(points)):
            required.add(index)
            if len(required) == MAXIMUM_POINTS_PER_SERIES:
                break
    return tuple(points[index] for index in sorted(required))


def _channel_edge(segment: str) -> tuple[str, str]:
    match = _CHANNEL.search(segment)
    channel = f"CH{match.group(1)}" if match else "unknown"
    lowered = segment.lower()
    edge = (
        "lower"
        if "_lower" in lowered
        else "upper"
        if "_upper" in lowered
        else "unknown"
    )
    return channel, edge


def _model(order: DopplerPolynomialOrder) -> str:
    return {
        DopplerPolynomialOrder.CONSTANT: "constant",
        DopplerPolynomialOrder.LINEAR: "linear",
        DopplerPolynomialOrder.QUADRATIC: "quadratic",
    }[order]


def _object_ref(row: dict[str, object], prefix: str) -> ObjectRef:
    return ObjectRef(
        Digest(
            DigestAlgorithm(str(row[f"{prefix}_bundle_digest_algorithm"])),
            str(row[f"{prefix}_bundle_digest_value"]),
        ),
        _integer(row[f"{prefix}_bundle_byte_count"]),
        str(row[f"{prefix}_bundle_media_type"]),
        str(row[f"{prefix}_bundle_format_id"]),
        str(row[f"{prefix}_bundle_locator"]),
    )


def _digest(row: dict[str, object], prefix: str) -> Digest:
    return Digest(
        DigestAlgorithm(str(row[f"{prefix}_digest_algorithm"])),
        str(row[f"{prefix}_digest_value"]),
    )


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("database integer is invalid")
    return value
