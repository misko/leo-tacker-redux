"""Pairwise association of method firings on exactly shared sample windows."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from leo_flow.contracts.core import SegmentId
from leo_flow.contracts.features import MethodScore


@dataclass(frozen=True)
class MethodAssociationReport:
    method_ids: tuple[str, ...]
    firing_covariance: tuple[tuple[float | None, ...], ...]
    phi: tuple[tuple[float | None, ...], ...]
    shared_window_count: tuple[tuple[int, ...], ...]
    shared_sample_count: tuple[tuple[int, ...], ...]
    method_present_window_count: tuple[int, ...]
    union_window_count: int
    missing_window_count: tuple[int, ...]


def method_firing_association(
    scores: Iterable[MethodScore], thresholds: Mapping[str, float]
) -> MethodAssociationReport:
    """Report pairwise-complete binary covariance; missing is never non-firing.

    Method identity includes version (``method_id@method_version``). Only exact
    segment/receiver/sample-window keys are shared observations. Covariance uses
    the population denominator because this report describes the frozen sample.
    Pairwise deletion can make the resulting matrix non-PSD; counts and
    missingness are therefore inseparable parts of this report.
    """

    rows: dict[tuple[SegmentId, str, int, int], dict[str, bool]] = defaultdict(dict)
    for score in scores:
        method = f"{score.method_id}@{score.method_version}"
        if method not in thresholds:
            raise ValueError(f"no firing threshold for {method}")
        key = (
            score.segment_id,
            score.receiver_key,
            score.window_start_sample,
            score.window_stop_sample,
        )
        if method in rows[key]:
            raise ValueError(f"duplicate method score for shared window: {method}")
        rows[key][method] = score.score >= thresholds[method]
    if not rows:
        raise ValueError("association requires method scores")
    methods = tuple(sorted(thresholds))
    present = tuple(sum(method in row for row in rows.values()) for method in methods)
    covariance: list[list[float | None]] = []
    phi: list[list[float | None]] = []
    window_counts: list[list[int]] = []
    sample_counts: list[list[int]] = []
    for left in methods:
        covariance_row: list[float | None] = []
        phi_row: list[float | None] = []
        window_row: list[int] = []
        sample_row: list[int] = []
        for right in methods:
            shared = [
                (key, row[left], row[right])
                for key, row in rows.items()
                if left in row and right in row
            ]
            window_row.append(len(shared))
            sample_row.append(sum(key[3] - key[2] for key, _, _ in shared))
            if not shared:
                covariance_row.append(None)
                phi_row.append(None)
                continue
            xs = [float(x) for _, x, _ in shared]
            ys = [float(y) for _, _, y in shared]
            xmean = math.fsum(xs) / len(xs)
            ymean = math.fsum(ys) / len(ys)
            cov = math.fsum(
                (x - xmean) * (y - ymean) for x, y in zip(xs, ys, strict=True)
            ) / len(xs)
            xvar = math.fsum((x - xmean) ** 2 for x in xs) / len(xs)
            yvar = math.fsum((y - ymean) ** 2 for y in ys) / len(ys)
            covariance_row.append(cov)
            phi_row.append(cov / math.sqrt(xvar * yvar) if xvar and yvar else None)
        covariance.append(covariance_row)
        phi.append(phi_row)
        window_counts.append(window_row)
        sample_counts.append(sample_row)
    return MethodAssociationReport(
        method_ids=methods,
        firing_covariance=tuple(tuple(row) for row in covariance),
        phi=tuple(tuple(row) for row in phi),
        shared_window_count=tuple(tuple(row) for row in window_counts),
        shared_sample_count=tuple(tuple(row) for row in sample_counts),
        method_present_window_count=present,
        union_window_count=len(rows),
        missing_window_count=tuple(len(rows) - count for count in present),
    )
