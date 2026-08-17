"""Bounded exact-template Starlink pilot search and calibrated evaluation.

This component accepts immutable template samples through a narrow port. It
does not import ``leo-tracker``; that repository remains a numerical oracle.
The first slice intentionally emits search candidates, not detections.
"""

from __future__ import annotations

import cmath
import math
import struct
from collections.abc import Sequence
from dataclasses import dataclass

from leo_flow.contracts._validation import require_finite, require_positive
from leo_flow.contracts.core import (
    V0_1,
    ArtifactRef,
    Digest,
    Provenance,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    canonical_digest,
)
from leo_flow.contracts.starlink import (
    StarlinkEdge,
    StarlinkEvaluationState,
    StarlinkPilotAnalysisBundleV0_1,
    StarlinkPilotCalibrationV0_1,
    StarlinkPilotEvaluationV0_1,
    StarlinkPilotSearchCandidateV0_1,
)

from .api import AnalysisExecutionContext

ALGORITHM_ID = "starlink-known-code-pilot-search"
ALGORITHM_VERSION = "0.1.0"
CONFIG_SCHEMA_ID = "org.leo-flow.starlink-known-code-search-config"
TEMPLATE_SCHEMA_ID = "org.leo-flow.starlink-edge-pilot-template"
FRAME_RATE_HZ = 750.0
CONTROL_SYMBOL_ROLL = 17
STATISTIC = "searched-exact-minus-conditioned-control-margin"


@dataclass(frozen=True)
class KnownCodePilotTemplatePairV0_1:
    """Immutable exact/control samples with independently pinned identities."""

    edge: StarlinkEdge
    pilot_indices: tuple[int, ...]
    sample_rate_hz: float
    exact_ref: ArtifactRef
    conditioned_control_ref: ArtifactRef
    exact_samples: tuple[complex, ...]
    conditioned_control_samples: tuple[complex, ...]
    control_symbol_roll: int = CONTROL_SYMBOL_ROLL

    def __post_init__(self) -> None:
        require_positive(self.sample_rate_hz, "sample_rate_hz")
        if not self.exact_samples or len(self.exact_samples) != len(
            self.conditioned_control_samples
        ):
            raise ValueError("exact/control templates must be equal and non-empty")
        if self.exact_ref == self.conditioned_control_ref:
            raise ValueError("exact and control templates require distinct identities")
        expected_schema = SchemaRef(TEMPLATE_SCHEMA_ID, V0_1)
        if (
            self.exact_ref.schema != expected_schema
            or self.conditioned_control_ref.schema != expected_schema
        ):
            raise ValueError("template references must use the v0.1 template schema")
        if self.exact_ref.digest != template_samples_digest(self.exact_samples):
            raise ValueError("exact template reference does not identify its samples")
        if self.conditioned_control_ref.digest != template_samples_digest(
            self.conditioned_control_samples
        ):
            raise ValueError("control template reference does not identify its samples")
        if self.control_symbol_roll != CONTROL_SYMBOL_ROLL:
            raise ValueError("v0.1 requires the frozen 17-symbol control roll")
        allowed = (
            set(range(528, 536))
            if self.edge is StarlinkEdge.LOWER
            else set(range(488, 496))
        )
        if (
            not self.pilot_indices
            or tuple(sorted(set(self.pilot_indices))) != self.pilot_indices
            or not set(self.pilot_indices) <= allowed
        ):
            raise ValueError("template pilot indices do not match the declared edge")
        for samples, label in (
            (self.exact_samples, "exact"),
            (self.conditioned_control_samples, "control"),
        ):
            if any(
                not math.isfinite(value.real) or not math.isfinite(value.imag)
                for value in samples
            ):
                raise ValueError(f"{label} template contains non-finite samples")
            if math.fsum(abs(value) ** 2 for value in samples) <= 0:
                raise ValueError(f"{label} template has zero energy")
            if any(value != _complex_float32(value) for value in samples):
                raise ValueError(
                    f"{label} template samples must be canonical complex-float32"
                )


@dataclass(frozen=True)
class KnownCodePilotSearchConfigV0_1:
    epoch_hypotheses_samples: tuple[int, ...]
    cfo_hypotheses_hz: tuple[float, ...]
    maximum_search_cells: int = 4096
    maximum_probe_samples: int = 1_000_000

    def __post_init__(self) -> None:
        if (
            not self.epoch_hypotheses_samples
            or len(set(self.epoch_hypotheses_samples))
            != len(self.epoch_hypotheses_samples)
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in self.epoch_hypotheses_samples
            )
        ):
            raise ValueError("epoch hypotheses must be unique non-negative integers")
        if not self.cfo_hypotheses_hz or len(set(self.cfo_hypotheses_hz)) != len(
            self.cfo_hypotheses_hz
        ):
            raise ValueError("CFO hypotheses must be non-empty and unique")
        for value in self.cfo_hypotheses_hz:
            require_finite(value, "cfo hypothesis")
        for name in ("maximum_search_cells", "maximum_probe_samples"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.search_cell_count > self.maximum_search_cells:
            raise ValueError("declared search exceeds maximum_search_cells")

    @property
    def search_cell_count(self) -> int:
        return len(self.epoch_hypotheses_samples) * len(self.cfo_hypotheses_hz)


def known_code_pilot_algorithm_ref_v0_1() -> ArtifactRef:
    return ArtifactRef(
        f"{ALGORITHM_ID}-v0.1",
        canonical_digest(
            {
                "algorithm_id": ALGORITHM_ID,
                "algorithm_version": ALGORITHM_VERSION,
                "frame_rate_hz": FRAME_RATE_HZ,
                "searched_statistic": (
                    "mean-per-frame-normalized-correlation-magnitude"
                ),
                "combination": "noncoherent-across-frames",
                "control": ("17-symbol-roll-conditioned-at-winning-epoch-and-cfo"),
                "template_payload": "interleaved-complex-float32-little-endian",
                "decision": "none-without-exact-whole-search-calibration",
            }
        ),
        SchemaRef("org.leo-flow.recording-algorithm", V0_1),
    )


def known_code_pilot_config_ref_v0_1(
    config: KnownCodePilotSearchConfigV0_1,
) -> ArtifactRef:
    return ArtifactRef(
        "starlink-known-code-search-config-v0.1",
        canonical_digest(config),
        SchemaRef(CONFIG_SCHEMA_ID, V0_1),
    )


def template_samples_digest(samples: Sequence[complex]) -> Digest:
    """Hash canonical interleaved complex-float32 little-endian payload bytes."""

    payload = bytearray()
    for value in samples:
        try:
            payload.extend(struct.pack("<ff", value.real, value.imag))
        except (OverflowError, struct.error) as exc:
            raise ValueError("template sample is outside finite float32") from exc
    return Digest.sha256(bytes(payload))


def _complex_float32(value: complex) -> complex:
    try:
        real, imag = struct.unpack("<ff", struct.pack("<ff", value.real, value.imag))
    except (OverflowError, struct.error) as exc:
        raise ValueError("template sample is outside finite float32") from exc
    return complex(real, imag)


class KnownCodePilotSearchV0_1:
    """Search a declared finite bank and condition the control at its winner."""

    def __init__(
        self,
        config: KnownCodePilotSearchConfigV0_1,
        execution: AnalysisExecutionContext,
    ) -> None:
        self._config = config
        self._execution = execution

    def analyze_receiver(
        self,
        samples: Sequence[complex],
        *,
        recording_id: RecordingId,
        recording_identity_digest: Digest,
        segment_id: SegmentId,
        receiver_chain_id: ReceiverChainId,
        templates: KnownCodePilotTemplatePairV0_1,
    ) -> StarlinkPilotAnalysisBundleV0_1:
        values = tuple(complex(value) for value in samples)
        if not values:
            raise ValueError("pilot search requires non-empty samples")
        if len(values) > self._config.maximum_probe_samples:
            raise ValueError("pilot search probe exceeds maximum_probe_samples")
        if any(
            not math.isfinite(value.real) or not math.isfinite(value.imag)
            for value in values
        ):
            raise ValueError("pilot search samples must be finite")
        algorithm_ref = known_code_pilot_algorithm_ref_v0_1()
        config_ref = known_code_pilot_config_ref_v0_1(self._config)
        search_identity = {
            "algorithm_digest": str(algorithm_ref.digest),
            "config_digest": str(config_ref.digest),
            "exact_template_digest": str(templates.exact_ref.digest),
            "conditioned_control_template_digest": str(
                templates.conditioned_control_ref.digest
            ),
            "edge": templates.edge.value,
            "pilot_indices": templates.pilot_indices,
            "sample_rate_hz": templates.sample_rate_hz,
            "probe_sample_count": len(values),
            "epoch_hypotheses_samples": self._config.epoch_hypotheses_samples,
            "cfo_hypotheses_hz": self._config.cfo_hypotheses_hz,
            "statistic": STATISTIC,
        }
        search_digest = canonical_digest(search_identity)
        scored: list[tuple[float, int, float, int]] = []
        for epoch in self._config.epoch_hypotheses_samples:
            for cfo in self._config.cfo_hypotheses_hz:
                score, support = _conditioned_score(
                    values,
                    templates.exact_samples,
                    templates.sample_rate_hz,
                    epoch,
                    cfo,
                )
                if support:
                    scored.append((score, epoch, cfo, support))
        if not scored:
            raise ValueError("no declared search cell contains one complete frame")
        searched_score, epoch, cfo, frame_support = max(
            scored,
            key=lambda item: (item[0], -abs(item[2]), -item[1], -item[2]),
        )
        exact_score, exact_support = _conditioned_score(
            values,
            templates.exact_samples,
            templates.sample_rate_hz,
            epoch,
            cfo,
        )
        control_score, control_support = _conditioned_score(
            values,
            templates.conditioned_control_samples,
            templates.sample_rate_hz,
            epoch,
            cfo,
        )
        if exact_support != frame_support or control_support != frame_support:
            raise RuntimeError("exact/control frame support differs at winning cell")
        candidate_identity = {
            "recording_identity_digest": str(recording_identity_digest),
            "segment_id": str(segment_id),
            "receiver_chain_id": str(receiver_chain_id),
            "search_identity_digest": str(search_digest),
        }
        token = canonical_digest(candidate_identity).value
        candidate_id = f"slcandidate_{token[:32]}"
        input_digest = canonical_digest(
            {
                "recording_identity_digest": str(recording_identity_digest),
                "segment_id": str(segment_id),
                "receiver_chain_id": str(receiver_chain_id),
            }
        )
        provenance = Provenance(
            self._execution.producer_name,
            self._execution.producer_version,
            self._execution.git_commit,
            self._execution.environment_digest,
            config_ref.digest,
            (input_digest,),
            (
                algorithm_ref.digest,
                templates.exact_ref.digest,
                templates.conditioned_control_ref.digest,
            ),
            self._execution.started_utc_ns,
            self._execution.completed_utc_ns,
            self._execution.host_class,
        )
        candidate = StarlinkPilotSearchCandidateV0_1(
            SchemaRef(StarlinkPilotSearchCandidateV0_1.SCHEMA_ID, V0_1),
            candidate_id,
            recording_id,
            recording_identity_digest,
            segment_id,
            receiver_chain_id,
            templates.edge,
            templates.pilot_indices,
            algorithm_ref,
            config_ref,
            templates.exact_ref,
            templates.conditioned_control_ref,
            search_digest,
            templates.sample_rate_hz,
            len(values),
            templates.sample_rate_hz / FRAME_RATE_HZ,
            self._config.epoch_hypotheses_samples,
            self._config.cfo_hypotheses_hz,
            self._config.search_cell_count,
            epoch,
            cfo,
            searched_score,
            exact_score,
            control_score,
            exact_score - control_score,
            frame_support,
            "winning-epoch-and-cfo-fixed",
            "not_evaluated",
            provenance,
            (
                "search-maximum-not-a-detection",
                "calibrated-whole-search-threshold-required",
                "pss-not-evaluated",
            ),
        )
        analysis_token = canonical_digest(
            {
                "recording_identity_digest": str(recording_identity_digest),
                "candidate_id": candidate_id,
            }
        ).value
        return StarlinkPilotAnalysisBundleV0_1(
            SchemaRef(StarlinkPilotAnalysisBundleV0_1.SCHEMA_ID, V0_1),
            f"slanalysis_{analysis_token[:32]}",
            recording_id,
            recording_identity_digest,
            (candidate,),
            ("uncalibrated-candidates-only",),
        )


def evaluate_pilot_candidate_v0_1(
    candidate: StarlinkPilotSearchCandidateV0_1,
    calibration: StarlinkPilotCalibrationV0_1 | None,
    *,
    hardware_profile_digest: Digest | None = None,
) -> StarlinkPilotEvaluationV0_1:
    """Apply only a calibration that closes over the exact candidate identity."""

    candidate_digest = canonical_digest(candidate)
    if calibration is None:
        return StarlinkPilotEvaluationV0_1(
            SchemaRef(StarlinkPilotEvaluationV0_1.SCHEMA_ID, V0_1),
            candidate.candidate_id,
            candidate_digest,
            StarlinkEvaluationState.UNCALIBRATED,
            STATISTIC,
            candidate.exact_minus_control_margin,
            None,
            None,
            None,
            ("no-matching-calibration",),
        )
    if hardware_profile_digest is None:
        raise ValueError("calibrated evaluation requires a hardware profile digest")
    expected = {
        "algorithm": candidate.algorithm_ref.digest,
        "config": candidate.config_ref.digest,
        "exact template": candidate.exact_template_ref.digest,
        "conditioned control template": (
            candidate.conditioned_control_template_ref.digest
        ),
        "search identity": candidate.search_identity_digest,
        "hardware profile": hardware_profile_digest,
    }
    actual = {
        "algorithm": calibration.algorithm_digest,
        "config": calibration.config_digest,
        "exact template": calibration.exact_template_digest,
        "conditioned control template": (
            calibration.conditioned_control_template_digest
        ),
        "search identity": calibration.search_identity_digest,
        "hardware profile": calibration.hardware_profile_digest,
    }
    mismatches = [name for name in expected if expected[name] != actual[name]]
    if mismatches:
        raise ValueError(
            "calibration identity mismatch: " + ", ".join(sorted(mismatches))
        )
    if calibration.statistic != STATISTIC:
        raise ValueError("calibration statistic differs from candidate statistic")
    score = candidate.exact_minus_control_margin
    return StarlinkPilotEvaluationV0_1(
        SchemaRef(StarlinkPilotEvaluationV0_1.SCHEMA_ID, V0_1),
        candidate.candidate_id,
        candidate_digest,
        StarlinkEvaluationState.CALIBRATED,
        STATISTIC,
        score,
        calibration.ref,
        calibration.threshold,
        score >= calibration.threshold,
        (),
    )


def _conditioned_score(
    values: tuple[complex, ...],
    template: tuple[complex, ...],
    sample_rate_hz: float,
    epoch_sample: int,
    cfo_hz: float,
) -> tuple[float, int]:
    template_energy = math.fsum(abs(value) ** 2 for value in template)
    period = sample_rate_hz / FRAME_RATE_HZ
    scores: list[float] = []
    frame = 0
    while True:
        start = epoch_sample + round(frame * period)
        if start + len(template) > len(values):
            break
        numerator = 0j
        data_energy = 0.0
        for local_index, reference in enumerate(template):
            sample_index = start + local_index
            received = values[sample_index]
            phase = cmath.exp(-2j * math.pi * cfo_hz * sample_index / sample_rate_hz)
            corrected = received * phase
            numerator += reference.conjugate() * corrected
            data_energy += abs(corrected) ** 2
        denominator = math.sqrt(template_energy * data_energy)
        scores.append(abs(numerator) / denominator if denominator else 0.0)
        frame += 1
    return (math.fsum(scores) / len(scores), len(scores)) if scores else (0.0, 0)
