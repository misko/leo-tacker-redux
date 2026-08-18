"""Pattern-aware bounded QAM-window selection from adaptive response evidence."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable

from leo_flow.contracts.starlink_adaptive_qam import (
    AdaptiveQamSelectionReason,
    StarlinkAdaptiveQamWindowSelectionV0_4,
)
from leo_flow.contracts.starlink_adaptive_response import (
    StarlinkAdaptiveResponsePointV0_1,
    StarlinkAdaptiveResponseStreamV0_1,
)
from leo_flow.contracts.starlink_detector_suite import StarlinkDetectorMethod


def adaptive_qam_window_selections_v0_4(
    stream: StarlinkAdaptiveResponseStreamV0_1,
    *,
    qam_window_sample_count: int,
    maximum_windows: int = 12,
) -> tuple[StarlinkAdaptiveQamWindowSelectionV0_4, ...]:
    """Retain target, margin, and control maxima without LNB-specific offsets."""

    if (
        qam_window_sample_count <= 0
        or qam_window_sample_count > stream.segment_sample_count
        or not 3 <= maximum_windows <= 24
    ):
        raise ValueError("adaptive QAM selection bounds are invalid")
    points = {
        item.window_index: item
        for item in stream.points
        if item.method is StarlinkDetectorMethod.FULL_FRAME_ACQUIRE
    }
    windows = tuple(
        item for item in stream.selection.exact_windows if item.window_index in points
    )
    if not windows:
        raise ValueError("adaptive response has no full-frame-acquire windows")
    quota = max(1, maximum_windows // 3)
    reasons: dict[int, set[AdaptiveQamSelectionReason]] = defaultdict(set)

    def retain(
        reason: AdaptiveQamSelectionReason,
        key: Callable[[StarlinkAdaptiveResponsePointV0_1], float],
    ) -> None:
        ranked = sorted(
            windows,
            key=lambda window: (-key(points[window.window_index]), window.start_sample),
        )
        for window in ranked[:quota]:
            reasons[window.window_index].add(reason)

    retain(AdaptiveQamSelectionReason.QIN_SCORE, lambda point: point.qin.score)
    retain(
        AdaptiveQamSelectionReason.QIN_MARGIN,
        lambda point: point.qin_minus_max_surrogate,
    )
    retain(
        AdaptiveQamSelectionReason.SURROGATE_SCORE,
        lambda point: max(item.winner.score for item in point.surrogates),
    )
    if len(reasons) < min(maximum_windows, len(windows)):
        ranked = sorted(
            windows,
            key=lambda window: (
                -max(
                    points[window.window_index].qin.score,
                    max(
                        item.winner.score
                        for item in points[window.window_index].surrogates
                    ),
                ),
                window.start_sample,
            ),
        )
        for window in ranked:
            if len(reasons) >= min(maximum_windows, len(windows)):
                break
            if window.window_index not in reasons:
                reasons[window.window_index].add(AdaptiveQamSelectionReason.FILL)
    selected = tuple(window for window in windows if window.window_index in reasons)
    output = []
    for window in selected:
        point = points[window.window_index]
        center = (window.start_sample + window.stop_sample) // 2
        start = min(
            max(0, center - qam_window_sample_count // 2),
            stream.segment_sample_count - qam_window_sample_count,
        )
        maximum_surrogate = max(item.winner.score for item in point.surrogates)
        output.append(
            StarlinkAdaptiveQamWindowSelectionV0_4(
                window.window_index,
                window.start_sample,
                window.stop_sample,
                start,
                start + qam_window_sample_count,
                tuple(
                    sorted(reasons[window.window_index], key=lambda item: item.value)
                ),
                point.qin.score,
                maximum_surrogate,
                point.qin.score - maximum_surrogate,
            )
        )
    return tuple(sorted(output, key=lambda item: item.qam_start_sample))


def shared_adaptive_qam_window_selections_v0_4(
    streams: tuple[StarlinkAdaptiveResponseStreamV0_1, ...],
    *,
    qam_window_sample_count: int,
    maximum_windows: int = 12,
) -> tuple[tuple[StarlinkAdaptiveQamWindowSelectionV0_4, ...], ...]:
    """Choose identical physical windows for authoritative dual-RX evidence.

    Rankings use the maximum evidence across receivers, but every emitted stream
    retains its own Qin/control scores. Only windows that were independently
    evaluated on every receiver are eligible, so pairing never invents a score
    or imports a historical label-derived frequency correction.
    """

    if len(streams) < 2:
        return tuple(
            adaptive_qam_window_selections_v0_4(
                stream,
                qam_window_sample_count=qam_window_sample_count,
                maximum_windows=maximum_windows,
            )
            for stream in streams
        )
    if not 3 <= maximum_windows <= 24 or qam_window_sample_count <= 0:
        raise ValueError("shared adaptive QAM selection bounds are invalid")
    group_identity = {
        (
            item.radio_id,
            item.segment_id,
            item.channel_number,
            item.edge,
            item.sample_rate_hz,
            item.segment_sample_count,
        )
        for item in streams
    }
    receivers = {item.receiver_chain_id for item in streams}
    if len(group_identity) != 1 or len(receivers) != len(streams):
        raise ValueError("shared adaptive QAM streams are not one receiver group")

    point_maps = []
    window_maps = []
    for stream in streams:
        points = {
            item.window_index: item
            for item in stream.points
            if item.method is StarlinkDetectorMethod.FULL_FRAME_ACQUIRE
        }
        windows = {
            (item.start_sample, item.stop_sample): item
            for item in stream.selection.exact_windows
            if item.window_index in points
        }
        if not windows:
            raise ValueError("shared adaptive response has no exact windows")
        point_maps.append(points)
        window_maps.append(windows)
    common = set(window_maps[0])
    for windows in window_maps[1:]:
        common.intersection_update(windows)
    if not common:
        raise ValueError("dual-receiver adaptive responses share no exact windows")

    ordered_common = tuple(sorted(common))
    quota = max(1, maximum_windows // 3)
    reasons: dict[tuple[int, int], set[AdaptiveQamSelectionReason]] = defaultdict(set)

    def aggregate(
        key: tuple[int, int],
        score: Callable[[StarlinkAdaptiveResponsePointV0_1], float],
    ) -> float:
        return max(
            score(points[windows[key].window_index])
            for points, windows in zip(point_maps, window_maps, strict=True)
        )

    def retain_shared(
        reason: AdaptiveQamSelectionReason,
        score: Callable[[StarlinkAdaptiveResponsePointV0_1], float],
    ) -> None:
        ranked = sorted(ordered_common, key=lambda key: (-aggregate(key, score), key))
        for key in ranked[:quota]:
            reasons[key].add(reason)

    retain_shared(AdaptiveQamSelectionReason.QIN_SCORE, lambda point: point.qin.score)
    retain_shared(
        AdaptiveQamSelectionReason.QIN_MARGIN,
        lambda point: point.qin_minus_max_surrogate,
    )
    retain_shared(
        AdaptiveQamSelectionReason.SURROGATE_SCORE,
        lambda point: max(item.winner.score for item in point.surrogates),
    )
    limit = min(maximum_windows, len(ordered_common))
    if len(reasons) < limit:
        ranked = sorted(
            ordered_common,
            key=lambda key: (
                -aggregate(
                    key,
                    lambda point: max(
                        point.qin.score,
                        max(item.winner.score for item in point.surrogates),
                    ),
                ),
                key,
            ),
        )
        for key in ranked:
            if len(reasons) >= limit:
                break
            if key not in reasons:
                reasons[key].add(AdaptiveQamSelectionReason.FILL)

    outputs = []
    for stream, points, windows in zip(streams, point_maps, window_maps, strict=True):
        selected = []
        for key in sorted(reasons):
            window = windows[key]
            point = points[window.window_index]
            center = (window.start_sample + window.stop_sample) // 2
            start = min(
                max(0, center - qam_window_sample_count // 2),
                stream.segment_sample_count - qam_window_sample_count,
            )
            maximum_surrogate = max(item.winner.score for item in point.surrogates)
            selected.append(
                StarlinkAdaptiveQamWindowSelectionV0_4(
                    window.window_index,
                    window.start_sample,
                    window.stop_sample,
                    start,
                    start + qam_window_sample_count,
                    tuple(sorted(reasons[key], key=lambda item: item.value)),
                    point.qin.score,
                    maximum_surrogate,
                    point.qin.score - maximum_surrogate,
                )
            )
        outputs.append(tuple(selected))
    return tuple(outputs)
