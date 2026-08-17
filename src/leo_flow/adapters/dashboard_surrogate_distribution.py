"""Durable Qin-versus-surrogate distributions for the read-only dashboard."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row

from leo_flow.analysis.recording.starlink_surrogate_null_recording_codec import (
    MAX_STARLINK_SURROGATE_NULL_RECORDING_BYTES,
    STARLINK_SURROGATE_NULL_RECORDING_FORMAT_ID,
    STARLINK_SURROGATE_NULL_RECORDING_MEDIA_TYPE,
    decode_starlink_surrogate_null_recording,
)
from leo_flow.contracts.core import Digest, DigestAlgorithm
from leo_flow.contracts.dashboard import TimeRangeQuery
from leo_flow.contracts.dashboard_score_distribution import (
    SCORE_HISTOGRAM_BIN_COUNT,
    ScoreHistogramBinV0_1,
)
from leo_flow.contracts.dashboard_surrogate_distribution import (
    SurrogateScoreDistributionV0_1,
    SurrogateScoreDistributionViewV0_1,
)
from leo_flow.contracts.starlink_surrogate_null_pipeline import (
    StarlinkSurrogateNullRecordingBundleV0_1,
)
from leo_flow.contracts.storage import ObjectRef
from leo_flow.storage.ports import BlobReader

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]
MAXIMUM_AGGREGATE_RECORDINGS = 512


class DurableDashboardSurrogateDistributionV0_1:
    def __init__(self, connect: ConnectionFactory, blobs: BlobReader) -> None:
        self._connect = connect
        self._blobs = blobs

    def surrogate_score_distributions(
        self, query: TimeRangeQuery
    ) -> SurrogateScoreDistributionViewV0_1:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                """SELECT *
                     FROM public.read_dashboard_surrogate_null_interval_v1(
                         %s,%s,%s)""",
                (
                    int(query.start_utc_ns),
                    int(query.stop_utc_ns),
                    MAXIMUM_AGGREGATE_RECORDINGS + 1,
                ),
            )
            rows = cursor.fetchall()
        truncated = len(rows) > MAXIMUM_AGGREGATE_RECORDINGS
        rows = rows[:MAXIMUM_AGGREGATE_RECORDINGS]
        grouped: dict[tuple[str, str, str, str, str], list[tuple[str, float]]] = (
            defaultdict(list)
        )
        recordings: set[str] = set()
        for row in rows:
            recording_id = str(row["recording_id"])
            bundle = self._read_bundle(_object_ref(row))
            if str(bundle.recording_id) != recording_id:
                raise ValueError(
                    "surrogate aggregate bundle belongs to another recording"
                )
            recordings.add(recording_id)
            for stream in bundle.streams:
                for method in stream.evidence.method_nulls:
                    prefix = (
                        method.method.value,
                        str(stream.radio_id),
                        str(stream.receiver_chain_id),
                        stream.edge.value,
                    )
                    grouped[(*prefix, "qin")].append(
                        (recording_id, method.target_score)
                    )
                    grouped[(*prefix, "surrogate")].extend(
                        (recording_id, score) for score in method.surrogate_scores
                    )
        distributions = tuple(
            _distribution(key, values) for key, values in sorted(grouped.items())
        )
        return SurrogateScoreDistributionViewV0_1(
            1,
            query.start_utc_ns,
            query.stop_utc_ns,
            SCORE_HISTOGRAM_BIN_COUNT,
            "recording+segment+radio+receiver-chain+edge+method+pattern",
            len(recordings),
            truncated,
            distributions,
            (
                "finite-surrogate-ensemble-not-calibrated-null-distribution",
                "candidate-evidence-not-detection",
            ),
        )

    def _read_bundle(self, ref: ObjectRef) -> StarlinkSurrogateNullRecordingBundleV0_1:
        if (
            ref.byte_count > MAX_STARLINK_SURROGATE_NULL_RECORDING_BYTES
            or ref.media_type != STARLINK_SURROGATE_NULL_RECORDING_MEDIA_TYPE
            or ref.format_id != STARLINK_SURROGATE_NULL_RECORDING_FORMAT_ID
        ):
            raise ValueError("surrogate aggregate object metadata is invalid")
        metadata = self._blobs.head(ref)
        if metadata.ref != ref or not metadata.verified:
            raise ValueError("surrogate aggregate object is not verified")
        with self._blobs.open(ref) as stream:
            payload = stream.read(MAX_STARLINK_SURROGATE_NULL_RECORDING_BYTES + 1)
        if len(payload) != ref.byte_count or Digest.sha256(payload) != ref.digest:
            raise ValueError("surrogate aggregate object bytes differ")
        return decode_starlink_surrogate_null_recording(payload)


def _object_ref(row: dict[str, object]) -> ObjectRef:
    return ObjectRef(
        Digest(
            DigestAlgorithm(str(row["bundle_digest_algorithm"])),
            str(row["bundle_digest_value"]),
        ),
        _integer(row["bundle_byte_count"]),
        str(row["bundle_media_type"]),
        str(row["bundle_format_id"]),
        str(row["bundle_locator"]),
    )


def _distribution(
    key: tuple[str, str, str, str, str], values: list[tuple[str, float]]
) -> SurrogateScoreDistributionV0_1:
    point_count = len(values)
    scores = [item[1] for item in values]
    mean = sum(scores) / point_count
    deviation = math.sqrt(sum((score - mean) ** 2 for score in scores) / point_count)
    counts = [0] * SCORE_HISTOGRAM_BIN_COUNT
    for score in scores:
        counts[
            min(int(score * SCORE_HISTOGRAM_BIN_COUNT), SCORE_HISTOGRAM_BIN_COUNT - 1)
        ] += 1
    width = 1.0 / SCORE_HISTOGRAM_BIN_COUNT
    bins = tuple(
        ScoreHistogramBinV0_1(
            index,
            index * width,
            (index + 1) * width,
            count,
            count / point_count / width,
        )
        for index, count in enumerate(counts)
    )
    return SurrogateScoreDistributionV0_1(
        *key,
        len({item[0] for item in values}),
        point_count,
        mean,
        deviation,
        min(scores),
        max(scores),
        bins,
    )


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("database integer is invalid")
    return value
