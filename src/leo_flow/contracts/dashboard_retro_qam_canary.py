"""Dashboard presentation contract for the historical QAM acceptance canary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ._validation import require_finite, require_token, require_utc_ns
from .core import V0_1, Digest, SchemaRef, UtcNs


@dataclass(frozen=True)
class RetroQamCanaryReceiverViewV0_1:
    receiver_index: int
    winning_epoch_sample: int
    winning_cfo_hz: float
    held_out_verify_score: float
    conditioned_control_score: float
    verify_minus_control_margin: float
    hard_symbol_accuracy: float
    rms_evm: float
    qam_goodness: float

    def __post_init__(self) -> None:
        if self.receiver_index not in (0, 1) or self.winning_epoch_sample < 0:
            raise ValueError("invalid canary receiver identity")
        for name in (
            "winning_cfo_hz",
            "held_out_verify_score",
            "conditioned_control_score",
            "verify_minus_control_margin",
            "hard_symbol_accuracy",
            "rms_evm",
            "qam_goodness",
        ):
            require_finite(getattr(self, name), name)
        if not 0 <= self.hard_symbol_accuracy <= 1 or not 0 <= self.qam_goodness <= 1:
            raise ValueError("canary QAM metrics are out of bounds")
        if self.rms_evm < 0:
            raise ValueError("canary EVM cannot be negative")


@dataclass(frozen=True)
class RetroQamCanaryDashboardViewV0_1:
    schema: SchemaRef
    corpus_id: str
    receipt_digest: Digest
    iq_object_digest: Digest
    git_commit: str
    completed_utc_ns: UtcNs
    schedule_interval_seconds: int
    metrics_match_oracle: bool
    combined_hard_symbol_accuracy: float
    combined_rms_evm: float
    combined_qam_goodness: float
    receivers: tuple[RetroQamCanaryReceiverViewV0_1, RetroQamCanaryReceiverViewV0_1]
    candidate_only: bool
    calibrated_detection: None
    labels: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.dashboard.retro-qam-canary"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported dashboard canary schema")
        require_token(self.corpus_id, "corpus_id")
        require_token(self.git_commit, "git_commit")
        require_utc_ns(self.completed_utc_ns, "completed_utc_ns")
        if self.schedule_interval_seconds != 1800:
            raise ValueError("dashboard canary cadence differs")
        for name in (
            "combined_hard_symbol_accuracy",
            "combined_rms_evm",
            "combined_qam_goodness",
        ):
            require_finite(getattr(self, name), name)
        if not 0 <= self.combined_hard_symbol_accuracy <= 1:
            raise ValueError("combined canary accuracy is out of bounds")
        if self.combined_rms_evm < 0 or not 0 <= self.combined_qam_goodness <= 1:
            raise ValueError("combined canary quality is out of bounds")
        if tuple(item.receiver_index for item in self.receivers) != (0, 1):
            raise ValueError("dashboard canary receivers are incomplete")
        if not self.candidate_only or self.calibrated_detection is not None:
            raise ValueError("historical canary cannot claim a calibrated detection")
        required = {
            "historical-acceptance-canary-not-live-recording",
            "known-published-pilot-regression",
            "candidate-evidence-not-calibrated-detection",
        }
        if not required <= set(self.labels):
            raise ValueError("dashboard canary labels are incomplete")


class RetroQamCanaryDashboardQueryPortV0_1(Protocol):
    def latest_retro_qam_canary(self) -> RetroQamCanaryDashboardViewV0_1: ...
