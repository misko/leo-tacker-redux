"""Dependency-free numerical oracle for advanced blind Doppler evidence.

This module is deliberately disconnected from recording, waterfall, persistence,
and orbit services.  It supplies small, deterministic kernels that can qualify a
blind moving structure before a separate post-blind TLE association step.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from math import isfinite, sqrt
from statistics import median

Matrix = Sequence[Sequence[float]]


@dataclass(frozen=True)
class LinearTrack:
    bins: tuple[int, ...]
    slope_bins_per_row: float
    score: float
    stationary_improvement: float


@dataclass(frozen=True)
class SlopeBankEvidence:
    track: LinearTrack
    heldout_score: float
    stationary_score: float
    opposite_slope_score: float
    shuffled_scores: tuple[float, ...]
    training_rows: tuple[int, ...]
    validation_rows: tuple[int, ...]
    test_rows: tuple[int, ...]


@dataclass(frozen=True)
class CombEvidence:
    fit_score: float
    heldout_score: float
    wrong_spacing_score: float


@dataclass(frozen=True)
class BroadbandEvidence:
    lower_slope_bins_per_row: float
    upper_slope_bins_per_row: float
    edge_slope_difference: float
    width_mad_fraction: float
    texture_shift_bins: float
    texture_correlation: float


@dataclass(frozen=True)
class ReceiverMotionConsensus:
    common_slope_bins_per_row: float
    slope_difference: float
    receiver_offsets_bins: tuple[float, float]
    offset_removed_rms_bins: float
    path_correlation: float


@dataclass(frozen=True)
class TleAssociation:
    name: str
    offset_bins: float
    heldout_rms_bins: float
    runner_up_margin_bins: float
    stationary_control_rms_bins: float
    opposite_slope_control_rms_bins: float
    qualified: bool


def dedoppler_slope_bank(
    power: Matrix,
    slopes_bins_per_row: Sequence[float],
    *,
    shuffle_offsets: Sequence[int] = (1, 3, 5, 7),
) -> SlopeBankEvidence:
    """Select a slope without looking at the rows used for final evidence.

    Rows are deterministically split by index modulo three.  Training rows fit
    an intercept for every predeclared slope, validation rows choose the slope,
    and the disjoint test rows produce the reported score.  After selection,
    training and validation rows jointly refit only the intercept.  Stationary
    and opposite-slope controls receive the same refit/test treatment.
    Shuffled controls cyclically misalign test-row identities.
    """

    rows, columns = _validated_shape(power)
    if rows < 6:
        raise ValueError("de-Doppler evidence requires at least six rows")
    slopes = tuple(float(value) for value in slopes_bins_per_row)
    if (
        not slopes
        or any(not isfinite(value) for value in slopes)
        or len(slopes) != len(set(slopes))
    ):
        raise ValueError("slope bank must be nonempty, finite, and unique")
    train = tuple(range(0, rows, 3))
    validation = tuple(range(1, rows, 3))
    test = tuple(range(2, rows, 3))
    candidates = [
        _fit_linear_track(power, slope, train, validation, columns) for slope in slopes
    ]
    selected = max(
        candidates,
        key=lambda item: (item.score, -abs(item.slope_bins_per_row)),
    )
    fit_rows = tuple(sorted((*train, *validation)))
    best = _fit_linear_track(
        power, selected.slope_bins_per_row, fit_rows, test, columns
    )
    stationary = _fit_linear_track(power, 0.0, fit_rows, test, columns)
    best = LinearTrack(
        best.bins,
        best.slope_bins_per_row,
        best.score,
        best.score - stationary.score,
    )
    opposite = _fit_linear_track(
        power, -best.slope_bins_per_row, fit_rows, test, columns
    )
    shuffled = tuple(
        _shuffled_path_mean(power, best.bins, test, offset)
        for offset in shuffle_offsets
        if offset % len(test)
    )
    return SlopeBankEvidence(
        best,
        best.score,
        stationary.score,
        opposite.score,
        shuffled,
        train,
        validation,
        test,
    )


def viterbi_peel_tracks(
    power: Matrix,
    *,
    maximum_step_bins: int,
    track_count: int,
    motion_penalty: float = 0.0,
    peel_radius_bins: int = 1,
) -> tuple[LinearTrack, ...]:
    """Find continuity-constrained ridges and mask each before the next pass."""

    rows, columns = _validated_shape(power)
    if maximum_step_bins < 0 or track_count < 1 or peel_radius_bins < 0:
        raise ValueError("Viterbi limits are invalid")
    if not isfinite(motion_penalty) or motion_penalty < 0:
        raise ValueError("motion penalty must be finite and nonnegative")
    residual = [list(map(float, row)) for row in power]
    floor = min(min(row) for row in residual) - 1_000_000.0
    tracks: list[LinearTrack] = []
    for _ in range(track_count):
        scores = [list(residual[0])]
        parents: list[list[int]] = []
        for row in range(1, rows):
            next_scores: list[float] = []
            next_parents: list[int] = []
            for column in range(columns):
                low = max(0, column - maximum_step_bins)
                high = min(columns, column + maximum_step_bins + 1)
                previous = max(
                    range(low, high),
                    key=lambda candidate: (
                        scores[-1][candidate] - motion_penalty * abs(column - candidate)
                    ),
                )
                next_parents.append(previous)
                next_scores.append(
                    residual[row][column]
                    + scores[-1][previous]
                    - motion_penalty * abs(column - previous)
                )
            parents.append(next_parents)
            scores.append(next_scores)
        path = [max(range(columns), key=lambda column: scores[-1][column])]
        for row in range(rows - 2, -1, -1):
            path.append(parents[row][path[-1]])
        path.reverse()
        slope, _ = _linear_fit(tuple(range(rows)), path)
        score = _path_mean(residual, path, tuple(range(rows)))
        stationary = max(
            _column_mean(power, column, tuple(range(rows))) for column in range(columns)
        )
        tracks.append(LinearTrack(tuple(path), slope, score, score - stationary))
        for row, column in enumerate(path):
            for masked in range(
                max(0, column - peel_radius_bins),
                min(columns, column + peel_radius_bins + 1),
            ):
                residual[row][masked] = floor
    return tuple(tracks)


def comb_support(
    power: Matrix,
    path_bins: Sequence[int],
    *,
    spacing_bins: int,
    tooth_numbers: Sequence[int] = tuple(range(-4, 5)),
    wrong_spacing_bins: int | None = None,
) -> CombEvidence:
    """Cross-validate a comb: even-numbered teeth fit, odd teeth validate."""

    rows, columns = _validated_shape(power)
    if len(path_bins) != rows or spacing_bins < 1:
        raise ValueError("comb path or spacing is invalid")
    fit = tuple(tooth for tooth in tooth_numbers if tooth % 2 == 0)
    heldout = tuple(tooth for tooth in tooth_numbers if tooth % 2)
    if not fit or not heldout:
        raise ValueError("comb requires both fit and held-out teeth")
    wrong = wrong_spacing_bins if wrong_spacing_bins is not None else spacing_bins + 1
    return CombEvidence(
        _tooth_mean(power, path_bins, fit, spacing_bins, columns),
        _tooth_mean(power, path_bins, heldout, spacing_bins, columns),
        _tooth_mean(power, path_bins, heldout, wrong, columns),
    )


def broadband_motion_support(
    lower_bins: Sequence[float],
    upper_bins: Sequence[float],
    texture_rows: Matrix,
    *,
    maximum_texture_step_bins: int,
) -> BroadbandEvidence:
    """Measure independent edge motion and internal texture translation."""

    if len(lower_bins) != len(upper_bins) or len(lower_bins) < 4:
        raise ValueError("edge paths must have the same length and at least four rows")
    if any(high <= low for low, high in zip(lower_bins, upper_bins, strict=True)):
        raise ValueError("upper edge must exceed lower edge")
    rows, _ = _validated_shape(texture_rows)
    if rows != len(lower_bins) or maximum_texture_step_bins < 0:
        raise ValueError("texture and edge dimensions differ")
    indexes = tuple(range(rows))
    lower_slope, _ = _linear_fit(indexes, lower_bins)
    upper_slope, _ = _linear_fit(indexes, upper_bins)
    widths = tuple(high - low for low, high in zip(lower_bins, upper_bins, strict=True))
    center_width = median(widths)
    width_mad = median(abs(width - center_width) for width in widths)
    shifts: list[int] = []
    correlations: list[float] = []
    for previous, current in pairwise(texture_rows):
        options = [
            (shift, _shifted_correlation(previous, current, shift))
            for shift in range(
                -maximum_texture_step_bins, maximum_texture_step_bins + 1
            )
        ]
        shift, correlation = max(options, key=lambda item: item[1])
        shifts.append(shift)
        correlations.append(correlation)
    return BroadbandEvidence(
        lower_slope,
        upper_slope,
        abs(lower_slope - upper_slope),
        1.4826 * width_mad / max(center_width, 1e-12),
        float(median(shifts)),
        float(median(correlations)),
    )


def dual_receiver_consensus(
    first_bins: Sequence[float], second_bins: Sequence[float]
) -> ReceiverMotionConsensus:
    """Compare common motion after fitting one arbitrary receiver offset."""

    if len(first_bins) != len(second_bins) or len(first_bins) < 4:
        raise ValueError("receiver paths must have equal length and at least four rows")
    indexes = tuple(range(len(first_bins)))
    first_slope, _ = _linear_fit(indexes, first_bins)
    second_slope, _ = _linear_fit(indexes, second_bins)
    offset = median(
        second - first for first, second in zip(first_bins, second_bins, strict=True)
    )
    residuals = tuple(
        second - first - offset
        for first, second in zip(first_bins, second_bins, strict=True)
    )
    return ReceiverMotionConsensus(
        (first_slope + second_slope) / 2.0,
        abs(first_slope - second_slope),
        (0.0, float(offset)),
        sqrt(sum(value * value for value in residuals) / len(residuals)),
        _correlation(first_bins, second_bins),
    )


def associate_tle_post_blind(
    observed_bins: Sequence[float],
    predictions: Mapping[str, Sequence[float]],
    *,
    blind_qualified: bool,
    minimum_runner_up_margin_bins: float,
) -> TleAssociation:
    """Rank TLE curves on held-out rows after a blind detector has qualified.

    Each candidate gets only a constant offset fitted on even rows.  Odd rows
    determine the association score.  Stationary and opposite-motion curves
    are explicit controls and TLE predictions never participate in detection.
    """

    if not blind_qualified:
        raise ValueError("TLE association is forbidden before blind qualification")
    if len(observed_bins) < 6 or not predictions:
        raise ValueError("association requires observations and predictions")
    if minimum_runner_up_margin_bins < 0:
        raise ValueError("association margin must be nonnegative")
    count = len(observed_bins)
    if any(len(values) != count for values in predictions.values()):
        raise ValueError("every TLE prediction must share the observation grid")
    train = tuple(range(0, count, 2))
    heldout = tuple(range(1, count, 2))
    scored = []
    for name, values in sorted(predictions.items()):
        offset = median(observed_bins[row] - values[row] for row in train)
        rms = _rms(tuple(observed_bins[row] - values[row] - offset for row in heldout))
        scored.append((rms, name, float(offset)))
    scored.sort()
    best_rms, name, offset = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else float("inf")
    stationary_offset = median(observed_bins[row] for row in train)
    stationary_rms = _rms(
        tuple(observed_bins[row] - stationary_offset for row in heldout)
    )
    predicted = predictions[name]
    first = predicted[0]
    opposite = tuple(2.0 * first - value for value in predicted)
    opposite_offset = median(observed_bins[row] - opposite[row] for row in train)
    opposite_rms = _rms(
        tuple(observed_bins[row] - opposite[row] - opposite_offset for row in heldout)
    )
    margin = runner_up - best_rms
    qualified = bool(
        margin >= minimum_runner_up_margin_bins
        and best_rms < stationary_rms
        and best_rms < opposite_rms
    )
    return TleAssociation(
        name,
        offset,
        best_rms,
        margin,
        stationary_rms,
        opposite_rms,
        qualified,
    )


def _validated_shape(values: Matrix) -> tuple[int, int]:
    if not values or not values[0]:
        raise ValueError("matrix must not be empty")
    columns = len(values[0])
    if any(len(row) != columns for row in values):
        raise ValueError("matrix must be rectangular")
    if any(not isfinite(float(value)) for row in values for value in row):
        raise ValueError("matrix values must be finite")
    return len(values), columns


def _fit_linear_track(
    power: Matrix,
    slope: float,
    train: Sequence[int],
    heldout: Sequence[int],
    columns: int,
) -> LinearTrack:
    if not isfinite(slope):
        raise ValueError("slopes must be finite")
    intercepts = [
        intercept
        for intercept in range(columns)
        if all(
            0 <= round(intercept + slope * row) < columns for row in range(len(power))
        )
    ]
    if not intercepts:
        raise ValueError("slope has no path fully contained in the matrix")
    intercept = max(
        intercepts,
        key=lambda candidate: sum(
            power[row][round(candidate + slope * row)] for row in train
        ),
    )
    path = tuple(round(intercept + slope * row) for row in range(len(power)))
    score = _path_mean(power, path, heldout)
    stationary = _column_mean(power, intercept, heldout)
    return LinearTrack(path, float(slope), score, score - stationary)


def _track_intercept(track: LinearTrack) -> int:
    return round(track.bins[0])


def _path_mean(values: Matrix, path: Sequence[int], rows: Sequence[int]) -> float:
    return sum(values[row][path[row]] for row in rows) / len(rows)


def _column_mean(values: Matrix, column: int, rows: Sequence[int]) -> float:
    return sum(values[row][column] for row in rows) / len(rows)


def _shuffled_path_mean(
    values: Matrix, path: Sequence[int], rows: Sequence[int], offset: int
) -> float:
    row_tuple = tuple(rows)
    shifted = row_tuple[offset % len(rows) :] + row_tuple[: offset % len(rows)]
    return sum(
        values[source][path[target]]
        for source, target in zip(shifted, rows, strict=True)
    ) / len(rows)


def _tooth_mean(
    values: Matrix,
    path: Sequence[int],
    teeth: Sequence[int],
    spacing: int,
    columns: int,
) -> float:
    samples = [
        values[row][column]
        for row, center in enumerate(path)
        for tooth in teeth
        if 0 <= (column := center + tooth * spacing) < columns
    ]
    if not samples:
        raise ValueError("comb teeth do not overlap the matrix")
    return sum(samples) / len(samples)


def _linear_fit(x: Sequence[float], y: Sequence[float]) -> tuple[float, float]:
    center_x = sum(x) / len(x)
    center_y = sum(y) / len(y)
    denominator = sum((value - center_x) ** 2 for value in x)
    if denominator == 0:
        raise ValueError("linear fit requires distinct coordinates")
    slope = (
        sum(
            (first - center_x) * (second - center_y)
            for first, second in zip(x, y, strict=True)
        )
        / denominator
    )
    return slope, center_y - slope * center_x


def _shifted_correlation(
    first: Sequence[float], second: Sequence[float], shift: int
) -> float:
    if shift < 0:
        return _correlation(first[-shift:], second[:shift])
    if shift > 0:
        return _correlation(first[:-shift], second[shift:])
    return _correlation(first, second)


def _correlation(first: Sequence[float], second: Sequence[float]) -> float:
    if len(first) != len(second) or len(first) < 3:
        return 0.0
    mean_first = sum(first) / len(first)
    mean_second = sum(second) / len(second)
    covariance = sum(
        (a - mean_first) * (b - mean_second) for a, b in zip(first, second, strict=True)
    )
    first_power = sum((value - mean_first) ** 2 for value in first)
    second_power = sum((value - mean_second) ** 2 for value in second)
    denominator = sqrt(first_power * second_power)
    return covariance / denominator if denominator else 0.0


def _rms(values: Sequence[float]) -> float:
    return sqrt(sum(value * value for value in values) / len(values))
