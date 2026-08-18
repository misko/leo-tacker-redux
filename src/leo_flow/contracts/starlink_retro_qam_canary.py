"""Immutable receipt for the historical Starlink pilot/QAM regression canary."""

from __future__ import annotations

from dataclasses import dataclass

from ._validation import require_finite, require_token
from .core import Digest, SchemaRef, canonical_digest


@dataclass(frozen=True)
class RetroQamReceiverMetricsV0_1:
    receiver_index: int
    winning_epoch_sample: int
    winning_cfo_hz: float
    acquire_score: float
    held_out_verify_score: float
    conditioned_control_score: float
    verify_minus_control_margin: float
    complete_frame_count: int
    hard_symbol_accuracy: float
    rms_evm: float
    soft_mean_confidence: float
    soft_mean_expected_probability: float
    soft_mean_entropy_bits: float
    soft_noise_variance: float
    model_snr_db: float

    def __post_init__(self) -> None:
        if self.receiver_index not in (0, 1):
            raise ValueError("retro-QAM receiver index must be zero or one")
        if self.winning_epoch_sample < 0 or self.complete_frame_count <= 0:
            raise ValueError("retro-QAM timing/frame support is invalid")
        for name in (
            "winning_cfo_hz",
            "acquire_score",
            "held_out_verify_score",
            "conditioned_control_score",
            "verify_minus_control_margin",
            "hard_symbol_accuracy",
            "rms_evm",
            "soft_mean_confidence",
            "soft_mean_expected_probability",
            "soft_mean_entropy_bits",
            "soft_noise_variance",
            "model_snr_db",
        ):
            require_finite(getattr(self, name), name)
        if (
            abs(
                self.verify_minus_control_margin
                - (self.held_out_verify_score - self.conditioned_control_score)
            )
            > 1e-12
        ):
            raise ValueError("retro-QAM held-out margin is inconsistent")
        for name in (
            "hard_symbol_accuracy",
            "soft_mean_confidence",
            "soft_mean_expected_probability",
        ):
            if not 0 <= getattr(self, name) <= 1:
                raise ValueError(f"{name} must lie in [0,1]")
        if not 0 <= self.soft_mean_entropy_bits <= 2:
            raise ValueError("soft entropy must lie in [0,2]")
        if self.rms_evm < 0 or self.soft_noise_variance <= 0:
            raise ValueError("retro-QAM EVM/noise metrics are invalid")


@dataclass(frozen=True)
class RetroQamDualReceiverMetricsV0_1:
    method: str
    observation_count: int
    receiver_weights: tuple[float, float]
    hard_symbol_accuracy: float
    rms_evm: float
    median_equalized_magnitude: float
    soft_mean_confidence: float
    soft_mean_expected_probability: float
    soft_mean_entropy_bits: float
    soft_noise_variance: float

    def __post_init__(self) -> None:
        if self.method != "inverse-noise-equalized-dual-rx":
            raise ValueError("unsupported retro-QAM receiver combination")
        if self.observation_count != 2_400:
            raise ValueError("combined retro-QAM metric must cover 300x8 symbols")
        if len(self.receiver_weights) != 2 or any(
            value <= 0 for value in self.receiver_weights
        ):
            raise ValueError("dual-receiver weights must be positive")
        if abs(sum(self.receiver_weights) - 1) > 1e-12:
            raise ValueError("dual-receiver weights must sum to one")
        for name in (
            "hard_symbol_accuracy",
            "rms_evm",
            "median_equalized_magnitude",
            "soft_mean_confidence",
            "soft_mean_expected_probability",
            "soft_mean_entropy_bits",
            "soft_noise_variance",
        ):
            require_finite(getattr(self, name), name)
        for name in (
            "hard_symbol_accuracy",
            "soft_mean_confidence",
            "soft_mean_expected_probability",
        ):
            if not 0 <= getattr(self, name) <= 1:
                raise ValueError(f"{name} must lie in [0,1]")
        if not 0 <= self.soft_mean_entropy_bits <= 2:
            raise ValueError("combined soft entropy must lie in [0,2]")
        if self.rms_evm < 0 or self.soft_noise_variance <= 0:
            raise ValueError("combined EVM/noise metrics are invalid")


@dataclass(frozen=True)
class StarlinkRetroQamCanaryReceiptV0_1:
    schema: SchemaRef
    corpus_id: str
    corpus_manifest_digest: Digest
    iq_object_digest: Digest
    selected_window_digest: Digest
    selected_window_start_sample: int
    selected_window_sample_count: int
    acquisition_algorithm_digest: Digest
    acquisition_config_digests: tuple[Digest, Digest]
    constellation_algorithm_digest: Digest
    receivers: tuple[RetroQamReceiverMetricsV0_1, RetroQamReceiverMetricsV0_1]
    combined: RetroQamDualReceiverMetricsV0_1
    metrics_match_oracle: bool
    candidate_only: bool
    calibrated_detection: bool | None
    producer_name: str
    producer_version: str
    git_commit: str
    completed_utc_ns: int
    reason_codes: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.starlink-retro-qam-canary-receipt"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID):
            raise ValueError("unsupported retro-QAM canary receipt")
        require_token(self.corpus_id, "corpus_id")
        require_token(self.producer_name, "producer_name")
        require_token(self.producer_version, "producer_version")
        require_token(self.git_commit, "git_commit")
        if self.selected_window_start_sample < 0:
            raise ValueError("selected window starts before the corpus")
        if self.selected_window_sample_count <= 0 or self.completed_utc_ns <= 0:
            raise ValueError("selected window/time is invalid")
        if tuple(item.receiver_index for item in self.receivers) != (0, 1):
            raise ValueError("retro-QAM receivers must be canonical and complete")
        if len(self.acquisition_config_digests) != 2:
            raise ValueError("retro-QAM config identities must cover both receivers")
        if not self.candidate_only or self.calibrated_detection is not None:
            raise ValueError("retro-QAM canary cannot claim calibrated detection")
        required = {
            "known-published-pilot-regression",
            "candidate-evidence-not-calibrated-detection",
            "leo-tracker-oracle-not-runtime-dependency",
            "whole-input-sha256-verified-before-analysis",
        }
        if not required <= set(self.reason_codes):
            raise ValueError("retro-QAM canary omits required scope disclosures")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)
