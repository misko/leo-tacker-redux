"""Streaming complete-IQ OFDM-periodicity prescreen for Starlink refinement."""

from __future__ import annotations

import math

import numpy as np
from numpy.typing import NDArray

from leo_flow.contracts.capture import SegmentManifest
from leo_flow.contracts.core import (
    V0_1,
    Provenance,
    SchemaRef,
    SegmentId,
    UtcNs,
    canonical_digest,
)
from leo_flow.contracts.starlink_full_dwell_timeline_product import (
    FullDwellTimelineStreamSelectionV0_1,
)
from leo_flow.contracts.starlink_pilot_prescreen import (
    StarlinkPilotPrescreenBundleV0_1,
    StarlinkPilotPrescreenRequestV0_1,
    StarlinkPilotPrescreenStreamV0_1,
    StarlinkPilotPrescreenWindowV0_1,
)
from leo_flow.storage.ports import RecordingView

from .api import AnalysisExecutionContext
from .starlink_full_dwell_timeline_product import contiguous_tile_intervals_v0_1

OFDM_USEFUL_DURATION_S = 4.0e-6
OFDM_TOTAL_DURATION_S = 4.4e-6


def ofdm_periodicity_v0_1(
    samples: NDArray[np.complex128], sample_rate_hz: float
) -> tuple[float, int, int, int]:
    """Return normalized cyclic-prefix periodicity over every symbol phase."""

    if samples.ndim != 1 or not len(samples):
        raise ValueError("OFDM periodicity requires one nonempty sample vector")
    if not math.isfinite(sample_rate_hz) or sample_rate_hz <= 0:
        raise ValueError("OFDM periodicity sample rate is invalid")
    useful = round(sample_rate_hz * OFDM_USEFUL_DURATION_S)
    total = round(sample_rate_hz * OFDM_TOTAL_DURATION_S)
    prefix = total - useful
    if useful <= 0 or prefix <= 0:
        raise ValueError("sample rate cannot resolve the OFDM cyclic prefix")
    best = (0.0, 0)
    for phase in range(total):
        indexes = np.arange(phase, len(samples) - useful, total, dtype=np.int64)
        if prefix > 1 and len(indexes):
            indexes = (indexes[:, None] + np.arange(prefix)[None, :]).reshape(-1)
            indexes = indexes[indexes + useful < len(samples)]
        if not len(indexes):
            continue
        left = samples[indexes]
        right = samples[indexes + useful]
        denominator = math.sqrt(
            float(np.vdot(left, left).real) * float(np.vdot(right, right).real)
        )
        score = float(abs(np.vdot(right, left)) / denominator) if denominator else 0.0
        candidate = (min(1.0, max(0.0, score)), -phase)
        if candidate > (best[0], -best[1]):
            best = (candidate[0], phase)
    return best[0], best[1], useful, total


class CompleteIqPilotPrescreenAnalyzerV0_1:
    """Read each interleaved tile once and score every receiver independently."""

    def __init__(self, execution: AnalysisExecutionContext) -> None:
        self._execution = execution

    def analyze(
        self, recording: RecordingView, request: StarlinkPilotPrescreenRequestV0_1
    ) -> StarlinkPilotPrescreenBundleV0_1:
        if recording.manifest.recording_id != request.recording_id:
            raise ValueError("recording and pilot-prescreen request differ")
        segments = {item.segment_id: item for item in recording.manifest.segments}
        cache: dict[
            tuple[SegmentId, int, int], tuple[tuple[float, float, int, int, int], ...]
        ] = {}
        streams = tuple(
            self._stream(recording, request, selection, segments, cache)
            for selection in request.streams
        )
        recording_digest = request.recording_object_ref.identity_digest()
        provenance = Provenance(
            self._execution.producer_name,
            self._execution.producer_version,
            self._execution.git_commit,
            self._execution.environment_digest,
            request.plan.digest,
            (recording_digest,),
            (),
            self._execution.started_utc_ns,
            self._execution.completed_utc_ns,
            self._execution.host_class,
        )
        token = canonical_digest(
            (request.digest, tuple(canonical_digest(item) for item in streams))
        ).value
        return StarlinkPilotPrescreenBundleV0_1(
            SchemaRef(StarlinkPilotPrescreenBundleV0_1.SCHEMA_ID, V0_1),
            f"slps_{token[:32]}",
            request.recording_id,
            recording_digest,
            request.digest,
            request.plan,
            streams,
            provenance,
            True,
            None,
            (
                "complete-iq-ofdm-periodicity-prescreen-not-starlink-detection",
                "pattern-blind-selection-shared-by-qin-and-surrogates",
                "stationary-ofdm-and-tones-may-score-high",
                "exact-target-control-refinement-required",
                "tile-union-covers-every-recorded-sample",
            ),
        )

    def _stream(
        self,
        recording: RecordingView,
        request: StarlinkPilotPrescreenRequestV0_1,
        selection: FullDwellTimelineStreamSelectionV0_1,
        segments: dict[SegmentId, SegmentManifest],
        cache: dict[
            tuple[SegmentId, int, int], tuple[tuple[float, float, int, int, int], ...]
        ],
    ) -> StarlinkPilotPrescreenStreamV0_1:
        segment = segments.get(selection.segment_id)
        if segment is None:
            raise ValueError("pilot-prescreen segment is unavailable")
        if (
            segment.sample_count != selection.segment_sample_count
            or segment.actual_sample_rate_hz != selection.sample_rate_hz
            or recording.manifest.radio_id != selection.radio_id
        ):
            raise ValueError("pilot-prescreen stream differs from manifest")
        tags = dict(segment.requested.tags)
        if (
            int(tags.get("channel", -1)) != selection.channel_number
            or tags.get("edge") != selection.edge.value
        ):
            raise ValueError("pilot-prescreen RF scope differs from manifest")
        try:
            receiver_index = segment.requested.receiver_chain_ids.index(
                selection.receiver_chain_id
            )
        except ValueError as error:
            raise ValueError("pilot-prescreen receiver is unavailable") from error
        receiver_count = len(segment.requested.receiver_chain_ids)
        measured = []
        intervals = contiguous_tile_intervals_v0_1(
            segment.sample_count, request.plan.tile_sample_count
        )
        if len(intervals) > request.plan.maximum_window_count_per_stream:
            raise ValueError("pilot-prescreen window count exceeds its bound")
        for start, stop in intervals:
            key = (segment.segment_id, start, stop)
            values = cache.get(key)
            if values is None:
                raw = recording.read_iq_bytes(segment.segment_id, start, stop)
                expected = (stop - start) * receiver_count * 4
                if len(raw) != expected:
                    raise ValueError("pilot-prescreen reader returned another interval")
                components = np.frombuffer(raw, dtype="<i2").reshape(
                    stop - start, receiver_count, 2
                )
                rows = []
                for index in range(receiver_count):
                    samples = np.asarray(
                        components[:, index, 0].astype(np.float64)
                        + 1j * components[:, index, 1].astype(np.float64),
                        dtype=np.complex128,
                    )
                    power = float(np.mean(np.abs(samples) ** 2))
                    score, phase, useful, total = ofdm_periodicity_v0_1(
                        samples, selection.sample_rate_hz
                    )
                    rows.append((power, score, phase, useful, total))
                values = tuple(rows)
                cache[key] = values
            measured.append((start, stop, *values[receiver_index]))
        periodicity_indexes = tuple(
            index
            for index, _ in sorted(
                enumerate(measured), key=lambda item: (-item[1][3], item[1][0])
            )[: request.plan.maximum_periodicity_seeds_per_stream]
        )
        power_indexes = tuple(
            index
            for index, _ in sorted(
                enumerate(measured), key=lambda item: (-item[1][2], item[1][0])
            )[: request.plan.maximum_power_seeds_per_stream]
        )
        periodicity_rank = {
            index: rank for rank, index in enumerate(periodicity_indexes)
        }
        power_rank = {index: rank for rank, index in enumerate(power_indexes)}
        windows = tuple(
            StarlinkPilotPrescreenWindowV0_1(
                index,
                start,
                stop,
                UtcNs(
                    int(segment.start_utc_ns)
                    + round(start * 1_000_000_000 / selection.sample_rate_hz)
                ),
                UtcNs(
                    int(segment.start_utc_ns)
                    + round(stop * 1_000_000_000 / selection.sample_rate_hz)
                ),
                power,
                score,
                phase,
                useful,
                total,
                periodicity_rank.get(index),
                power_rank.get(index),
            )
            for index, (start, stop, power, score, phase, useful, total) in enumerate(
                measured
            )
        )
        return StarlinkPilotPrescreenStreamV0_1(
            selection, windows, segment.sample_count, 1.0
        )
