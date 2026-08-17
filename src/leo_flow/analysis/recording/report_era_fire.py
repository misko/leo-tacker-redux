"""Fail-closed application of the frozen leo-tracker report fire rule.

The JSON artifact is Redux-owned and packaged locally.  This module never imports
or reads from leo-tracker or from an operator evidence directory at runtime.
"""

from __future__ import annotations

import hashlib
import json
import math
from importlib.resources import files
from typing import Final

from leo_flow.contracts.core import V0_1, Digest, DigestAlgorithm, SchemaRef
from leo_flow.contracts.starlink_detector_suite import StarlinkDetectorMethod
from leo_flow.contracts.starlink_report_era import (
    ProvisionalReportEraFireDecisionV0_1,
    ProvisionalReportEraFireState,
)

REPORT_ERA_THRESHOLD_ARTIFACT_SHA256: Final = (
    "c8f64ab27c1fc2f4aa6a3b55f4bfdb68c72422c9833d6de9728c7f4e54268500"
)
REPORT_ERA_SOURCE_REVISION: Final = "0bb80d14759fd8496b74e7d3219a690be18565a6"
REPORT_ERA_SCORE_SEMANTICS_V1: Final = (
    "report-era-fcore-build/distinct-candidate-point-maximum/v1@"
    + REPORT_ERA_SOURCE_REVISION
)

# Only dimensions present in the current corpus and proven as exact overlaps are
# enabled.  Artifact rows for clipped 1.25 MS/s, absent 10 MS/s, and absent 640 ms
# are retained for audit but are deliberately unreachable here.
_SUPPORTED_DIMENSIONS: Final = frozenset(
    {
        (2_500_000, 200_000),
        (2_500_000, 400_000),
        (5_000_000, 400_000),
        (5_000_000, 800_000),
    }
)


def _load_thresholds() -> dict[tuple[StarlinkDetectorMethod, int, int], float]:
    resource = files("leo_flow.analysis.recording").joinpath(
        "artifacts/report_era_thresholds_v1.json"
    )
    raw = resource.read_bytes()
    if hashlib.sha256(raw).hexdigest() != REPORT_ERA_THRESHOLD_ARTIFACT_SHA256:
        raise RuntimeError("Redux report-era threshold artifact digest mismatch")
    payload = json.loads(raw)
    if (
        payload.get("schema") != "org.leo-flow.report-era-threshold-reconstruction/v1"
        or payload.get("source_revision") != REPORT_ERA_SOURCE_REVISION
        or payload.get("candidate_only") is not True
        or payload.get("threshold_count") != 96
    ):
        raise RuntimeError("Redux report-era threshold artifact identity mismatch")

    thresholds: dict[tuple[StarlinkDetectorMethod, int, int], float] = {}
    for row in payload.get("thresholds", ()):
        method = StarlinkDetectorMethod(row["method"])
        rate = int(row["sample_rate_hz"])
        probe_samples = round(rate * float(row["probe_ms"]) / 1_000)
        key = (method, rate, probe_samples)
        if key in thresholds:
            raise RuntimeError(
                "Redux report-era threshold artifact has duplicate cells"
            )
        threshold = float(row["threshold"])
        if not math.isfinite(threshold):
            raise RuntimeError("Redux report-era threshold artifact is non-finite")
        thresholds[key] = threshold
    if len(thresholds) != 96:
        raise RuntimeError("Redux report-era threshold artifact cell count mismatch")
    return thresholds


_THRESHOLDS: Final = _load_thresholds()


def decide_provisional_report_era_fire_v0_1(
    *,
    method: StarlinkDetectorMethod,
    sample_rate_hz: float,
    probe_sample_count: int,
    reported_score: float,
    score_semantics: str,
) -> ProvisionalReportEraFireDecisionV0_1:
    """Apply strict ``score > threshold`` only to an exact historical cell.

    A caller must attest that its score is the maximum over the report's distinct
    candidate points.  Current Redux v0.2 8 ms suite scores do not have that
    identity and therefore fail closed.
    """

    if (
        isinstance(sample_rate_hz, bool)
        or not isinstance(sample_rate_hz, (int, float))
        or not math.isfinite(sample_rate_hz)
        or sample_rate_hz <= 0
    ):
        raise ValueError("sample_rate_hz must be finite and positive")
    if (
        isinstance(probe_sample_count, bool)
        or not isinstance(probe_sample_count, int)
        or probe_sample_count <= 0
    ):
        raise ValueError("probe_sample_count must be a positive integer")
    if (
        isinstance(reported_score, bool)
        or not isinstance(reported_score, (int, float))
        or not math.isfinite(reported_score)
        or not 0 <= reported_score <= 1
    ):
        raise ValueError("reported_score must be finite and lie in [0, 1]")

    rate = int(sample_rate_hz)
    dimension = (rate, probe_sample_count)
    reasons = ["not-a-calibrated-beacon-detection"]
    if sample_rate_hz != rate or dimension not in _SUPPORTED_DIMENSIONS:
        reasons.append("unsupported-report-era-dimensions")
        return _not_applicable(
            method,
            sample_rate_hz,
            probe_sample_count,
            score_semantics,
            reported_score,
            tuple(reasons),
        )
    if score_semantics != REPORT_ERA_SCORE_SEMANTICS_V1:
        reasons.append("incompatible-report-era-search-semantics")
        return _not_applicable(
            method,
            sample_rate_hz,
            probe_sample_count,
            score_semantics,
            reported_score,
            tuple(reasons),
        )

    threshold = _THRESHOLDS[(method, rate, probe_sample_count)]
    fired = reported_score > threshold
    reasons.extend(("candidate-only", "point-null-one-percent-order-statistic"))
    return ProvisionalReportEraFireDecisionV0_1(
        SchemaRef(ProvisionalReportEraFireDecisionV0_1.SCHEMA_ID, V0_1),
        method,
        sample_rate_hz,
        probe_sample_count,
        score_semantics,
        reported_score,
        threshold,
        (
            ProvisionalReportEraFireState.FIRED
            if fired
            else ProvisionalReportEraFireState.DID_NOT_FIRE
        ),
        fired,
        (
            "provisional report-era candidate fire"
            if fired
            else "provisional report-era candidate non-fire"
        ),
        Digest(DigestAlgorithm.SHA256, REPORT_ERA_THRESHOLD_ARTIFACT_SHA256),
        tuple(reasons),
    )


def _not_applicable(
    method: StarlinkDetectorMethod,
    sample_rate_hz: float,
    probe_sample_count: int,
    score_semantics: str,
    reported_score: float,
    reasons: tuple[str, ...],
) -> ProvisionalReportEraFireDecisionV0_1:
    return ProvisionalReportEraFireDecisionV0_1(
        SchemaRef(ProvisionalReportEraFireDecisionV0_1.SCHEMA_ID, V0_1),
        method,
        sample_rate_hz,
        probe_sample_count,
        score_semantics,
        reported_score,
        None,
        ProvisionalReportEraFireState.NOT_APPLICABLE,
        None,
        "report-era rule not applicable",
        Digest(DigestAlgorithm.SHA256, REPORT_ERA_THRESHOLD_ARTIFACT_SHA256),
        reasons,
    )
