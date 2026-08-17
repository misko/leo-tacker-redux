"""Additive contract for provisional reproduction of report-era candidate fires."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ._validation import require_finite
from .core import V0_1, Digest, SchemaRef
from .starlink_detector_suite import StarlinkDetectorMethod


class ProvisionalReportEraFireState(str, Enum):
    FIRED = "fired"
    DID_NOT_FIRE = "did-not-fire"
    NOT_APPLICABLE = "not-applicable"


@dataclass(frozen=True)
class ProvisionalReportEraFireDecisionV0_1:
    """A historical candidate-fire comparison, never a beacon detection."""

    schema: SchemaRef
    method: StarlinkDetectorMethod
    sample_rate_hz: float
    probe_sample_count: int
    score_semantics: str
    reported_score: float
    threshold: float | None
    state: ProvisionalReportEraFireState
    candidate_fire: bool | None
    public_label: str
    threshold_artifact_digest: Digest
    reason_codes: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.provisional-report-era-fire-decision"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported provisional report-era fire schema")
        require_finite(self.sample_rate_hz, "sample_rate_hz")
        require_finite(self.reported_score, "reported_score")
        if self.sample_rate_hz <= 0 or self.probe_sample_count <= 0:
            raise ValueError("report-era dimensions must be positive")
        if not 0 <= self.reported_score <= 1:
            raise ValueError("report-era score must lie in [0, 1]")
        if self.threshold is not None:
            require_finite(self.threshold, "threshold")
        if self.state is ProvisionalReportEraFireState.NOT_APPLICABLE:
            if self.threshold is not None or self.candidate_fire is not None:
                raise ValueError("an inapplicable rule cannot emit a threshold or fire")
            if self.public_label != "report-era rule not applicable":
                raise ValueError("inapplicable decisions require an explicit label")
        else:
            expected_fire = self.state is ProvisionalReportEraFireState.FIRED
            if self.threshold is None or self.candidate_fire is not expected_fire:
                raise ValueError("report-era fire state is inconsistent")
            if not 0 <= self.threshold <= 1:
                raise ValueError("report-era threshold must lie in [0, 1]")
            if self.candidate_fire is not (self.reported_score > self.threshold):
                raise ValueError(
                    "report-era strict threshold comparison is inconsistent"
                )
            expected_label = (
                "provisional report-era candidate fire"
                if expected_fire
                else "provisional report-era candidate non-fire"
            )
            if self.public_label != expected_label:
                raise ValueError("applicable decisions require the provisional label")
        if "not-a-calibrated-beacon-detection" not in self.reason_codes:
            raise ValueError("report-era decisions must deny detection semantics")
