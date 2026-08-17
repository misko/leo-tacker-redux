"""Read-only aggregate projection over durable temporal pilot bundles."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row

from leo_flow.analysis.recording.starlink_temporal_pilot_codec import (
    MAX_STARLINK_TEMPORAL_PILOT_BYTES,
    STARLINK_TEMPORAL_PILOT_FORMAT_ID,
    STARLINK_TEMPORAL_PILOT_MEDIA_TYPE,
    decode_starlink_temporal_pilot,
)
from leo_flow.contracts.core import Digest, DigestAlgorithm
from leo_flow.contracts.dashboard import TimeRangeQuery
from leo_flow.contracts.dashboard_temporal_pilot import (
    TemporalPilotAggregateStratumV0_1,
    TemporalPilotAggregateViewV0_1,
)
from leo_flow.contracts.starlink_temporal_pilot import (
    StarlinkTemporalMethodPointV0_1,
    StarlinkTemporalPilotRecordingBundleV0_1,
    StarlinkTemporalStreamEvidenceV0_1,
)
from leo_flow.contracts.storage import ObjectRef
from leo_flow.storage.ports import BlobReader

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]
MAXIMUM_AGGREGATE_RECORDINGS = 512


class DurableDashboardTemporalPilotAggregateV0_1:
    def __init__(self, connect: ConnectionFactory, blobs: BlobReader) -> None:
        self._connect, self._blobs = connect, blobs

    def temporal_pilot_aggregate(
        self, query: TimeRangeQuery
    ) -> TemporalPilotAggregateViewV0_1:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                "SELECT * FROM public.read_dashboard_temporal_pilot_interval_v1(%s,%s,%s)",
                (
                    int(query.start_utc_ns),
                    int(query.stop_utc_ns),
                    MAXIMUM_AGGREGATE_RECORDINGS + 1,
                ),
            )
            rows = cursor.fetchall()
        truncated = len(rows) > MAXIMUM_AGGREGATE_RECORDINGS
        rows = rows[:MAXIMUM_AGGREGATE_RECORDINGS]
        grouped: dict[
            tuple[str, str, str, str],
            list[
                tuple[
                    str,
                    StarlinkTemporalStreamEvidenceV0_1,
                    tuple[StarlinkTemporalMethodPointV0_1, ...],
                ]
            ],
        ] = defaultdict(list)
        recordings: set[str] = set()
        for row in rows:
            recording_id = str(row["recording_id"])
            bundle = self._read(_object_ref(row))
            if str(bundle.recording_id) != recording_id:
                raise ValueError(
                    "temporal aggregate bundle belongs to another recording"
                )
            recordings.add(recording_id)
            for stream in bundle.streams:
                points_by_method = {
                    summary.method: tuple(
                        point
                        for point in stream.points
                        if point.method is summary.method
                    )
                    for summary in stream.dwell_summaries
                }
                for summary in stream.dwell_summaries:
                    key = (
                        summary.method.value,
                        str(stream.radio_id),
                        str(stream.receiver_chain_id),
                        stream.edge.value,
                    )
                    grouped[key].append(
                        (recording_id, stream, points_by_method[summary.method])
                    )
        strata = tuple(_stratum(key, values) for key, values in sorted(grouped.items()))
        return TemporalPilotAggregateViewV0_1(
            1,
            query.start_utc_ns,
            query.stop_utc_ns,
            len(recordings),
            truncated,
            strata,
            (
                "stratified-sampling-not-continuous-coverage",
                "candidate-evidence-not-calibrated-detection",
                "overlapping-windows-statistically-dependent",
            ),
        )

    def _read(self, ref: ObjectRef) -> StarlinkTemporalPilotRecordingBundleV0_1:
        if (
            ref.byte_count > MAX_STARLINK_TEMPORAL_PILOT_BYTES
            or ref.media_type != STARLINK_TEMPORAL_PILOT_MEDIA_TYPE
            or ref.format_id != STARLINK_TEMPORAL_PILOT_FORMAT_ID
        ):
            raise ValueError("temporal aggregate object metadata is invalid")
        metadata = self._blobs.head(ref)
        if metadata.ref != ref or not metadata.verified:
            raise ValueError("temporal aggregate object is not verified")
        with self._blobs.open(ref) as stream:
            payload = stream.read(MAX_STARLINK_TEMPORAL_PILOT_BYTES + 1)
        if len(payload) != ref.byte_count or Digest.sha256(payload) != ref.digest:
            raise ValueError("temporal aggregate bytes differ")
        return decode_starlink_temporal_pilot(payload)


def _stratum(
    key: tuple[str, str, str, str],
    values: list[
        tuple[
            str,
            StarlinkTemporalStreamEvidenceV0_1,
            tuple[StarlinkTemporalMethodPointV0_1, ...],
        ]
    ],
) -> TemporalPilotAggregateStratumV0_1:
    streams = [item[1] for item in values]
    points = [point for item in values for point in item[2]]
    return TemporalPilotAggregateStratumV0_1(
        method=key[0],
        radio_id=key[1],
        receiver_chain_id=key[2],
        edge=key[3],
        recording_count=len({item[0] for item in values}),
        probe_count=len(points),
        mean_probe_maximum_qin_score=sum(point.qin.score for point in points)
        / len(points),
        mean_probe_maximum_surrogate_score=sum(
            max(control.winner.score for control in point.surrogates)
            for point in points
        )
        / len(points),
        mean_union_coverage_fraction=sum(stream.coverage_fraction for stream in streams)
        / len(streams),
        candidate_window_fraction=sum(
            point.qin_minus_max_surrogate > 0 for point in points
        )
        / len(points),
    )


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


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("database integer is invalid")
    return value
