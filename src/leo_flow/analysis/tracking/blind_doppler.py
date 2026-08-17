"""Bounded, dependency-free blind Doppler candidate extraction."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from leo_flow.contracts.blind_doppler import (
    BlindDopplerAnalysisRequestV0_1,
    BlindDopplerBundleV0_1,
    BlindDopplerCandidateV0_1,
    DopplerPolynomialFitV0_1,
    DopplerPolynomialOrder,
    DopplerTrackPointV0_1,
    SpectrogramSliceV0_1,
    StationaryControlEvidenceV0_1,
)
from leo_flow.contracts.core import Digest, SchemaRef, canonical_digest

ALGORITHM_VERSION = "blind-doppler-v0.1"
MAX_TRACK_SEEDS_PER_COMPONENT = 512


@dataclass(frozen=True)
class BlindDopplerConfig:
    minimum_spectral_peak_excess_db: float = 6.0
    maximum_peaks_per_row: int = 12
    maximum_missing_rows: int = 2
    maximum_frequency_step_hz: float = 1_500.0
    maximum_abs_drift_rate_hz_s: float = 200_000.0
    minimum_track_points: int = 4
    minimum_duration_s: float = 0.0
    broadband_row_fraction: float = 0.20
    overlap_suppression_fraction: float = 0.60

    def __post_init__(self) -> None:
        finite = (
            self.minimum_spectral_peak_excess_db,
            self.maximum_frequency_step_hz,
            self.maximum_abs_drift_rate_hz_s,
            self.minimum_duration_s,
            self.broadband_row_fraction,
            self.overlap_suppression_fraction,
        )
        if not all(math.isfinite(value) for value in finite):
            raise ValueError("blind Doppler configuration must be finite")
        if self.minimum_spectral_peak_excess_db <= 0:
            raise ValueError("minimum_spectral_peak_excess_db must be positive")
        if not 1 <= self.maximum_peaks_per_row <= 64:
            raise ValueError("maximum_peaks_per_row is outside its bound")
        if not 0 <= self.maximum_missing_rows <= 16:
            raise ValueError("maximum_missing_rows is outside its bound")
        if self.maximum_frequency_step_hz < 0 or self.maximum_abs_drift_rate_hz_s < 0:
            raise ValueError("continuity limits must be nonnegative")
        if not 2 <= self.minimum_track_points <= 64:
            raise ValueError("minimum_track_points is outside its bound")
        if self.minimum_duration_s < 0:
            raise ValueError("minimum_duration_s must be nonnegative")
        if not 0 < self.broadband_row_fraction <= 1:
            raise ValueError("broadband_row_fraction is outside its bound")
        if not 0 <= self.overlap_suppression_fraction <= 1:
            raise ValueError("overlap_suppression_fraction is outside its bound")


@dataclass(frozen=True)
class _Peak:
    identity: int
    row_index: int
    point: DopplerTrackPointV0_1


@dataclass(frozen=True)
class _Track:
    component_id: int
    peaks: tuple[_Peak, ...]


class BasicBlindDopplerAnalyzer:
    """Extracts bounded candidates; it does not make a detection decision."""

    def __init__(self, config: BlindDopplerConfig | None = None) -> None:
        self._config = BlindDopplerConfig() if config is None else config

    def analyze_blind_doppler(
        self,
        spectrogram: SpectrogramSliceV0_1,
        request: BlindDopplerAnalysisRequestV0_1,
    ) -> BlindDopplerBundleV0_1:
        if request.input_identity_digest != spectrogram.input_identity_digest:
            raise ValueError("request and spectrogram input identities differ")
        if request.config_digest != blind_doppler_config_digest(self._config):
            raise ValueError("request config digest does not identify analyzer config")
        rows, broadband_rows = _extract_peaks(spectrogram, self._config)
        peaks = tuple(peak for row in rows for peak in row)
        links = _continuity_links(rows, self._config)
        components = _connected_components(peaks, links)
        tracks, truncated_seed_count = _extract_tracks(components, links, self._config)
        candidates = [
            _candidate(track)
            for track in tracks
            if len(track.peaks) >= self._config.minimum_track_points
            and _duration_s(track.peaks) >= self._config.minimum_duration_s
        ]
        candidates.sort(key=lambda item: item.ranking_score, reverse=True)
        candidates = candidates[: request.max_candidates]
        ranked = tuple(
            BlindDopplerCandidateV0_1(
                rank=rank,
                component_id=item.component_id,
                points=item.points,
                fits=item.fits,
                selected_order=item.selected_order,
                stationary_control=item.stationary_control,
                mean_spectral_peak_excess_db=item.mean_spectral_peak_excess_db,
                peak_layer_value_db=item.peak_layer_value_db,
                duration_s=item.duration_s,
                missing_row_count=item.missing_row_count,
                missing_row_fraction=item.missing_row_fraction,
                edge_truncated_point_count=item.edge_truncated_point_count,
                ranking_score=item.ranking_score,
            )
            for rank, item in enumerate(candidates, 1)
        )
        reasons: tuple[str, ...] = ()
        if not ranked:
            reasons = ("no_candidate_met_track_bounds",)
        warning_items: list[str] = []
        if broadband_rows:
            warning_items.append(f"broadband_rows_suppressed:{broadband_rows}")
        if truncated_seed_count:
            warning_items.append(f"track_seed_pairs_truncated:{truncated_seed_count}")
        return BlindDopplerBundleV0_1(
            schema=SchemaRef(BlindDopplerBundleV0_1.SCHEMA_ID),
            input_identity_digest=spectrogram.input_identity_digest,
            config_digest=request.config_digest,
            algorithm_version=ALGORITHM_VERSION,
            candidate_only=True,
            examined_row_count=len(spectrogram.rows),
            extracted_peak_count=len(peaks),
            candidates=ranked,
            warnings=tuple(warning_items),
            reason_codes=reasons,
        )


def blind_doppler_config_digest(config: BlindDopplerConfig) -> Digest:
    """Canonical identity used to close a request over exact tracker settings."""

    return canonical_digest(config)


def _extract_peaks(
    spectrogram: SpectrogramSliceV0_1, config: BlindDopplerConfig
) -> tuple[tuple[tuple[_Peak, ...], ...], int]:
    result: list[tuple[_Peak, ...]] = []
    identity = 0
    broadband_rows = 0
    axis = spectrogram.frequency_bin_offsets_hz
    for row_index, row in enumerate(spectrogram.rows):
        floor = statistics.median(row.power_db)
        excess = sum(
            value - floor >= config.minimum_spectral_peak_excess_db
            for value in row.power_db
        )
        if excess / len(row.power_db) >= config.broadband_row_fraction:
            result.append(())
            broadband_rows += 1
            continue
        found: list[_Peak] = []
        for index, power in enumerate(row.power_db):
            if power - floor < config.minimum_spectral_peak_excess_db:
                continue
            left = row.power_db[index - 1] if index else -math.inf
            right = (
                row.power_db[index + 1] if index + 1 < len(row.power_db) else -math.inf
            )
            if power < left or power < right or (power == left and index > 0):
                continue
            fractional_bin = float(index)
            offset_hz = axis[index]
            edge = index == 0 or index == len(axis) - 1
            if not edge:
                denominator = left - 2 * power + right
                delta = 0.0 if denominator == 0 else 0.5 * (left - right) / denominator
                delta = max(-0.5, min(0.5, delta))
                fractional_bin += delta
                if delta < 0:
                    offset_hz += delta * (axis[index] - axis[index - 1])
                else:
                    offset_hz += delta * (axis[index + 1] - axis[index])
            found.append(
                _Peak(
                    identity=identity,
                    row_index=row_index,
                    point=DopplerTrackPointV0_1(
                        row_index=row_index,
                        midpoint_utc_ns=row.midpoint_utc_ns,
                        frequency_hz=spectrogram.center_frequency_hz + offset_hz,
                        interpolated_bin=fractional_bin,
                        layer_value_db=power,
                        row_baseline_db=floor,
                        local_peak_excess_db=power - floor,
                        edge_truncated=edge,
                    ),
                )
            )
            identity += 1
        found.sort(key=lambda peak: peak.point.local_peak_excess_db, reverse=True)
        result.append(tuple(found[: config.maximum_peaks_per_row]))
    return tuple(result), broadband_rows


def _continuity_links(
    rows: tuple[tuple[_Peak, ...], ...], config: BlindDopplerConfig
) -> dict[int, tuple[int, ...]]:
    links: dict[int, list[int]] = {peak.identity: [] for row in rows for peak in row}
    for row_index, row in enumerate(rows):
        for gap in range(1, config.maximum_missing_rows + 2):
            later_index = row_index + gap
            if later_index >= len(rows):
                break
            for earlier in row:
                for later in rows[later_index]:
                    dt = (
                        later.point.midpoint_utc_ns - earlier.point.midpoint_utc_ns
                    ) / 1e9
                    bound = (
                        config.maximum_frequency_step_hz
                        + config.maximum_abs_drift_rate_hz_s * dt
                    )
                    if (
                        abs(later.point.frequency_hz - earlier.point.frequency_hz)
                        <= bound
                    ):
                        links[earlier.identity].append(later.identity)
    return {key: tuple(value) for key, value in links.items()}


def _connected_components(
    peaks: tuple[_Peak, ...], links: dict[int, tuple[int, ...]]
) -> tuple[tuple[_Peak, ...], ...]:
    by_id = {peak.identity: peak for peak in peaks}
    undirected = {identity: set(targets) for identity, targets in links.items()}
    for source, targets in links.items():
        for target in targets:
            undirected[target].add(source)
    seen: set[int] = set()
    components: list[tuple[_Peak, ...]] = []
    for identity in sorted(by_id):
        if identity in seen:
            continue
        stack = [identity]
        member_ids: list[int] = []
        seen.add(identity)
        while stack:
            current = stack.pop()
            member_ids.append(current)
            for adjacent in undirected[current]:
                if adjacent not in seen:
                    seen.add(adjacent)
                    stack.append(adjacent)
        components.append(
            tuple(
                sorted(
                    (by_id[item] for item in member_ids),
                    key=lambda p: (p.row_index, p.identity),
                )
            )
        )
    return tuple(components)


def _extract_tracks(
    components: tuple[tuple[_Peak, ...], ...],
    links: dict[int, tuple[int, ...]],
    config: BlindDopplerConfig,
) -> tuple[tuple[_Track, ...], int]:
    tracks: list[_Track] = []
    truncated_seed_count = 0
    for component_id, component in enumerate(components):
        by_id = {peak.identity: peak for peak in component}
        proposals: dict[tuple[int, ...], tuple[_Peak, ...]] = {}
        # A seed pair fixes a local velocity; prediction prevents swapping at crossings.
        seeds = [
            (first, by_id[second_id])
            for first in component
            for second_id in links[first.identity]
            if second_id in by_id
        ]
        seeds.sort(
            key=lambda pair: (
                pair[0].point.local_peak_excess_db + pair[1].point.local_peak_excess_db
            ),
            reverse=True,
        )
        truncated_seed_count += max(0, len(seeds) - MAX_TRACK_SEEDS_PER_COMPONENT)
        for first, second in seeds[:MAX_TRACK_SEEDS_PER_COMPONENT]:
            seed_path = [first, second]
            used_rows = {first.row_index, seed_path[-1].row_index}
            while True:
                previous, current = seed_path[-2:]
                dt0 = (
                    current.point.midpoint_utc_ns - previous.point.midpoint_utc_ns
                ) / 1e9
                rate = (current.point.frequency_hz - previous.point.frequency_hz) / dt0
                options = [
                    by_id[target]
                    for target in links[current.identity]
                    if target in by_id and by_id[target].row_index not in used_rows
                ]
                if not options:
                    break
                chosen = _select_prediction(options, current, rate)
                seed_path.append(chosen)
                used_rows.add(chosen.row_index)
            key = tuple(item.identity for item in seed_path)
            proposals[key] = tuple(seed_path)
        ordered = sorted(
            proposals.values(),
            key=lambda path: (
                len(path),
                sum(item.point.local_peak_excess_db for item in path),
            ),
            reverse=True,
        )
        accepted: list[tuple[_Peak, ...]] = []
        for candidate_path in ordered:
            identities = {item.identity for item in candidate_path}
            if any(
                len(identities & {item.identity for item in prior})
                / min(len(candidate_path), len(prior))
                >= config.overlap_suppression_fraction
                for prior in accepted
            ):
                continue
            accepted.append(candidate_path)
        tracks.extend(
            _Track(component_id, candidate_path) for candidate_path in accepted
        )
    return tuple(tracks), truncated_seed_count


def _prediction_error(
    peak: _Peak, current: _Peak, rate: float
) -> tuple[int, float, float]:
    dt = (peak.point.midpoint_utc_ns - current.point.midpoint_utc_ns) / 1e9
    prediction = current.point.frequency_hz + rate * dt
    return (
        peak.row_index,
        abs(peak.point.frequency_hz - prediction),
        -peak.point.local_peak_excess_db,
    )


def _select_prediction(options: list[_Peak], current: _Peak, rate: float) -> _Peak:
    return min(options, key=lambda peak: _prediction_error(peak, current, rate))


def _candidate(track: _Track) -> BlindDopplerCandidateV0_1:
    points = tuple(peak.point for peak in track.peaks)
    fits = tuple(_robust_fit(points, order) for order in _supported_orders(len(points)))
    selected = min(fits, key=lambda fit: fit.bic)
    constant = fits[0]
    improvement = (
        0.0
        if constant.residual_rms_hz == 0
        else 1 - selected.residual_rms_hz / constant.residual_rms_hz
    )
    missing = points[-1].row_index - points[0].row_index + 1 - len(points)
    span = len(points) + missing
    duration = _duration_s(track.peaks)
    mean_peak_excess = statistics.fmean(point.local_peak_excess_db for point in points)
    edge_count = sum(point.edge_truncated for point in points)
    ranking = (
        mean_peak_excess
        + 2 * math.log1p(len(points))
        + math.log1p(max(0.0, duration) * 1_000)
        - 4 * missing / span
        - 2 * edge_count / len(points)
    )
    return BlindDopplerCandidateV0_1(
        rank=1,
        component_id=track.component_id,
        points=points,
        fits=fits,
        selected_order=selected.order,
        stationary_control=StationaryControlEvidenceV0_1(
            constant_residual_rms_hz=constant.residual_rms_hz,
            selected_residual_rms_hz=selected.residual_rms_hz,
            residual_improvement_fraction=improvement,
            bic_margin_over_constant=constant.bic - selected.bic,
            moving_model_preferred=selected.order
            is not DopplerPolynomialOrder.CONSTANT,
        ),
        mean_spectral_peak_excess_db=mean_peak_excess,
        peak_layer_value_db=max(point.layer_value_db for point in points),
        duration_s=duration,
        missing_row_count=missing,
        missing_row_fraction=missing / span,
        edge_truncated_point_count=edge_count,
        ranking_score=ranking,
    )


def _duration_s(peaks: tuple[_Peak, ...]) -> float:
    return (peaks[-1].point.midpoint_utc_ns - peaks[0].point.midpoint_utc_ns) / 1e9


def _supported_orders(point_count: int) -> tuple[DopplerPolynomialOrder, ...]:
    result = [DopplerPolynomialOrder.CONSTANT]
    if point_count >= 3:
        result.append(DopplerPolynomialOrder.LINEAR)
    if point_count >= 5:
        result.append(DopplerPolynomialOrder.QUADRATIC)
    return tuple(result)


def _robust_fit(
    points: tuple[DopplerTrackPointV0_1, ...], order: DopplerPolynomialOrder
) -> DopplerPolynomialFitV0_1:
    reference_ns = points[len(points) // 2].midpoint_utc_ns
    times = [(point.midpoint_utc_ns - reference_ns) / 1e9 for point in points]
    values = [point.frequency_hz for point in points]
    weights = [1.0] * len(points)
    coefficients = [statistics.fmean(values)]
    for _ in range(5):
        coefficients = _weighted_polynomial(times, values, weights, int(order))
        residuals = [
            value - _evaluate(coefficients, time) for time, value in zip(times, values)
        ]
        center = statistics.median(residuals)
        scale = 1.4826 * statistics.median(abs(value - center) for value in residuals)
        if scale <= 1e-9:
            break
        cutoff = 1.345 * scale
        weights = [
            1.0 if abs(value - center) <= cutoff else cutoff / abs(value - center)
            for value in residuals
        ]
    residuals = [
        value - _evaluate(coefficients, time) for time, value in zip(times, values)
    ]
    rms = math.sqrt(statistics.fmean(value * value for value in residuals))
    center = statistics.median(residuals)
    scale = 1.4826 * statistics.median(abs(value - center) for value in residuals)
    inliers = sum(abs(value - center) <= max(1e-9, 2.5 * scale) for value in residuals)
    rss_per_point = max(1e-12, statistics.fmean(value * value for value in residuals))
    parameter_count = int(order) + 1
    bic = len(points) * math.log(rss_per_point) + parameter_count * math.log(
        len(points)
    )
    frequency = coefficients[0]
    rate = coefficients[1] if len(coefficients) > 1 else 0.0
    acceleration = 2 * coefficients[2] if len(coefficients) > 2 else 0.0
    return DopplerPolynomialFitV0_1(
        order=order,
        reference_utc_ns=reference_ns,
        frequency_hz=frequency,
        drift_rate_hz_s=rate,
        drift_acceleration_hz_s2=acceleration,
        residual_rms_hz=rms,
        robust_scale_hz=scale,
        inlier_count=inliers,
        bic=bic,
    )


def _weighted_polynomial(
    times: list[float], values: list[float], weights: list[float], order: int
) -> list[float]:
    size = order + 1
    matrix = [[0.0] * size for _ in range(size)]
    vector = [0.0] * size
    for time, value, weight in zip(times, values, weights):
        powers = [1.0]
        for _ in range(2 * order):
            powers.append(powers[-1] * time)
        for row in range(size):
            vector[row] += weight * value * powers[row]
            for column in range(size):
                matrix[row][column] += weight * powers[row + column]
    return _solve(matrix, vector)


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    size = len(vector)
    augmented = [row[:] + [value] for row, value in zip(matrix, vector)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-18:
            raise ValueError("degenerate Doppler fit geometry")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        scale = augmented[column][column]
        augmented[column] = [value / scale for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                value - factor * pivot_value
                for value, pivot_value in zip(augmented[row], augmented[column])
            ]
    return [augmented[row][-1] for row in range(size)]


def _evaluate(coefficients: list[float], time: float) -> float:
    return sum(value * time**power for power, value in enumerate(coefficients))
