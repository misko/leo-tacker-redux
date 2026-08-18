"""Durable terminal receipt for normalized, candidate-only QAM summaries.

The receipt closes a summary over immutable source request and product digests.
It deliberately does not turn candidate evidence into a calibrated detection.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ._validation import require_token
from .core import (
    ArtifactRef,
    Digest,
    RecordingId,
    SchemaRef,
    SchemaVersion,
    canonical_digest,
)

MAX_DASHBOARD_QAM_SUMMARY_CANDIDATES_V0_2 = 16


@dataclass(frozen=True)
class DashboardQamSummaryConfigV0_2:
    candidate_only: bool = True
    calibration_required: bool = True
    goodness_algorithm: str = "qam-goodness-v0.2"
    maximum_candidates: int = MAX_DASHBOARD_QAM_SUMMARY_CANDIDATES_V0_2
    selection: str = "highest-qam-goodness-per-recording-radio-lnb-receiver"
    streams: str = "radio-lnb-receiver-never-pooled"


DASHBOARD_QAM_SUMMARY_CONFIG_V0_2 = DashboardQamSummaryConfigV0_2()
DASHBOARD_QAM_SUMMARY_CONFIG_REF_V0_2 = ArtifactRef(
    "dashboard-capture-qam-summary-config-v0.2",
    canonical_digest(DASHBOARD_QAM_SUMMARY_CONFIG_V0_2),
    SchemaRef(
        "org.leo-flow.dashboard-capture-qam-summary-config", SchemaVersion(0, 2)
    ),
)


class QamSummarySourceKind(str, Enum):
    ACQUIRED_V0_3 = "acquired-v0.3"
    ADAPTIVE_V0_4 = "adaptive-v0.4"


class QamSummaryTerminalOutcome(str, Enum):
    COMPLETE = "complete"
    NO_CANDIDATE = "no-candidate"


@dataclass(frozen=True)
class DashboardQamSummaryReceiptV0_2:
    source_kind: QamSummarySourceKind
    analysis_id: str
    recording_id: RecordingId
    source_request_digest: Digest
    source_product_digest: Digest
    summary_config_digest: Digest
    candidate_set_digest: Digest
    terminal_outcome: QamSummaryTerminalOutcome
    candidate_count: int
    candidate_only: bool = True
    calibration_required: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.source_kind, QamSummarySourceKind):
            raise TypeError("QAM summary source kind is invalid")
        require_token(self.analysis_id, "analysis_id")
        if not isinstance(self.terminal_outcome, QamSummaryTerminalOutcome):
            raise TypeError("QAM summary terminal outcome is invalid")
        if (
            not self.candidate_only
            or not self.calibration_required
            or self.summary_config_digest
            != DASHBOARD_QAM_SUMMARY_CONFIG_REF_V0_2.digest
        ):
            raise ValueError("QAM summary safety/configuration closure differs")
        complete = self.terminal_outcome is QamSummaryTerminalOutcome.COMPLETE
        if complete and not (
            1
            <= self.candidate_count
            <= MAX_DASHBOARD_QAM_SUMMARY_CANDIDATES_V0_2
        ):
            raise ValueError("QAM summary terminal outcome conflicts with count")
        if not complete and self.candidate_count != 0:
            raise ValueError("QAM summary terminal outcome conflicts with count")


def dashboard_qam_candidate_set_digest_v0_2(
    candidates: Sequence[Mapping[str, Any]],
) -> Digest:
    """Close over the ordered normalized candidate set sent to PostgreSQL."""

    return canonical_digest(list(candidates))
