"""Threshold decisions over already-extracted, immutable method scores."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from leo_flow.contracts.core import Digest, SegmentId, canonical_digest
from leo_flow.contracts.features import MethodScore


@dataclass(frozen=True)
class ThresholdRule:
    rule_id: str
    calibration_dataset_id: str
    thresholds: tuple[tuple[str, float], ...]

    def __post_init__(self) -> None:
        if not self.rule_id or not self.calibration_dataset_id:
            raise ValueError(
                "threshold rule identity and calibration dataset are required"
            )
        keys = [key for key, _ in self.thresholds]
        if not keys or len(keys) != len(set(keys)):
            raise ValueError("threshold method identities must be non-empty and unique")
        if any(not math.isfinite(value) for _, value in self.thresholds):
            raise ValueError("thresholds must be finite")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class MethodFiring:
    method_id: str
    method_version: str
    segment_id: SegmentId
    receiver_key: str
    window_start_sample: int
    window_stop_sample: int
    score: float
    threshold: float
    fired: bool
    rule_digest: Digest


def apply_threshold_rule(
    scores: Iterable[MethodScore], rule: ThresholdRule
) -> tuple[MethodFiring, ...]:
    """Apply one calibrated rule without changing or dropping score rows."""

    thresholds: Mapping[str, float] = dict(rule.thresholds)
    firings: list[MethodFiring] = []
    for score in scores:
        identity = f"{score.method_id}@{score.method_version}"
        if identity not in thresholds:
            raise ValueError(f"threshold rule has no entry for {identity}")
        threshold = thresholds[identity]
        firings.append(
            MethodFiring(
                score.method_id,
                score.method_version,
                score.segment_id,
                score.receiver_key,
                score.window_start_sample,
                score.window_stop_sample,
                score.score,
                threshold,
                score.score >= threshold,
                rule.digest,
            )
        )
    return tuple(firings)
