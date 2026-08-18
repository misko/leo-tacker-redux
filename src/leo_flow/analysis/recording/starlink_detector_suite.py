"""Pure bounded implementations of the Starlink report's detector suite.

The implementation is native Redux code.  ``leo-tracker`` is used only to
produce numerical-oracle fixtures in tests and is never imported at runtime.
"""

from __future__ import annotations

import cmath
import math
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np

from leo_flow.contracts._validation import require_finite, require_positive
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    Provenance,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    UtcNs,
    canonical_digest,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_detector_suite import (
    REPORT_METHOD_ORDER,
    V0_2,
    StarlinkDetectorMethod,
    StarlinkDetectorMethodEvidenceV0_2,
    StarlinkDetectorSuiteBundleV0_2,
    StarlinkFrameScoreSummaryV0_2,
    StarlinkMultiRadioCandidateEvidenceV0_2,
    StarlinkPssSssAcquisitionEvidenceV0_2,
    StarlinkSamplingStratum,
    StarlinkSearchMode,
)
from leo_flow.contracts.starlink_full_search_control import (
    V0_1 as FULL_SEARCH_CONTROL_V0_1,
)
from leo_flow.contracts.starlink_full_search_control import (
    StarlinkFullSearchControlMethodEvidenceV0_1,
    StarlinkFullSearchControlMode,
    StarlinkFullSearchControlSuiteV0_1,
)

from .api import AnalysisExecutionContext
from .starlink import (
    CONTROL_SYMBOL_ROLL,
    FRAME_RATE_HZ,
    KnownCodePilotTemplatePairV0_1,
    template_samples_digest,
)

ALGORITHM_ID = "starlink-report-detector-suite"
ALGORITHM_VERSION = "0.2.0"
FULL_SEARCH_CONTROL_ALGORITHM_ID = "starlink-rolled-template-full-search-control"
FULL_SEARCH_CONTROL_ALGORITHM_VERSION = "0.1.0"
ACQUIRE_CONDITIONED_CONTROL_ALGORITHM_ID = (
    "starlink-pattern-acquire-conditioned-report-method-control"
)
ACQUIRE_CONDITIONED_CONTROL_ALGORITHM_VERSION = "0.1.0"
CONFIG_SCHEMA_ID = "org.leo-flow.starlink-detector-suite-config"
PSS_SSS_TEMPLATE_SCHEMA_ID = "org.leo-flow.starlink-pss-sss-template"
OFDM_SYMBOL_DURATION_S = 4.4e-6
PILOT_BANDWIDTH_HZ = 1_875_000.0
FIRST_PILOT_SYMBOL = 2
LAST_PILOT_SYMBOL = 301
DEFAULT_GLRT_RESIDUAL_CFO_HZ = tuple(
    index / (32 * OFDM_SYMBOL_DURATION_S) for index in (*range(16), *range(-16, 0))
)


@dataclass(frozen=True)
class StarlinkDetectorSuiteConfigV0_2:
    """Finite searched hypotheses, split identity and resource ceilings."""

    epoch_hypotheses_samples: tuple[int, ...]
    coarse_cfo_hypotheses_hz: tuple[float, ...]
    glrt_residual_cfo_hypotheses_hz: tuple[float, ...] = DEFAULT_GLRT_RESIDUAL_CFO_HZ
    acquire_symbols: tuple[int, ...] = tuple(range(2, 302, 2))
    verify_symbols: tuple[int, ...] = tuple(range(3, 302, 2))
    maximum_probe_samples: int = 6_400_000
    maximum_outer_search_cells: int = 16_384
    maximum_effective_search_cells: int = 1_000_000
    maximum_frame_summaries: int = 100_000

    def __post_init__(self) -> None:
        _unique_nonnegative_ints(self.epoch_hypotheses_samples, "epoch hypotheses")
        _unique_finite(self.coarse_cfo_hypotheses_hz, "coarse CFO hypotheses")
        _unique_finite(
            self.glrt_residual_cfo_hypotheses_hz,
            "GLRT residual CFO hypotheses",
        )
        _pilot_symbols(self.acquire_symbols, "acquire symbols")
        _pilot_symbols(self.verify_symbols, "verify symbols")
        if set(self.acquire_symbols) & set(self.verify_symbols):
            raise ValueError("ACQUIRE and VERIFY pilot symbols must be disjoint")
        if tuple(sorted(self.acquire_symbols + self.verify_symbols)) != tuple(
            range(FIRST_PILOT_SYMBOL, LAST_PILOT_SYMBOL + 1)
        ):
            raise ValueError("ACQUIRE and VERIFY must partition all 300 pilots")
        for name in (
            "maximum_probe_samples",
            "maximum_outer_search_cells",
            "maximum_effective_search_cells",
            "maximum_frame_summaries",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.outer_search_cell_count > self.maximum_outer_search_cells:
            raise ValueError("declared outer search exceeds its resource bound")
        if self.glrt_effective_search_cell_count > self.maximum_effective_search_cells:
            raise ValueError("declared GLRT search exceeds its resource bound")

    @property
    def outer_search_cell_count(self) -> int:
        return len(self.epoch_hypotheses_samples) * len(self.coarse_cfo_hypotheses_hz)

    @property
    def glrt_effective_search_cell_count(self) -> int:
        return self.outer_search_cell_count * len(self.glrt_residual_cfo_hypotheses_hz)

    @property
    def symbol_split_digest(self) -> Digest:
        return canonical_digest(
            {
                "mode": "interleaved-disjoint",
                "acquire": self.acquire_symbols,
                "verify": self.verify_symbols,
            }
        )


@dataclass(frozen=True)
class StarlinkPssSssTemplateV0_2:
    """Externally pinned replica for optional supporting lag-Doppler search."""

    template_ref: ArtifactRef
    sample_rate_hz: float
    samples: tuple[complex, ...]
    captured_template_energy_fraction: float

    def __post_init__(self) -> None:
        require_positive(self.sample_rate_hz, "sample_rate_hz")
        if not self.samples:
            raise ValueError("PSS+SSS template cannot be empty")
        if any(not _finite_complex(value) for value in self.samples):
            raise ValueError("PSS+SSS template samples must be finite")
        if math.fsum(abs(value) ** 2 for value in self.samples) <= 0:
            raise ValueError("PSS+SSS template must have positive energy")
        if self.template_ref.schema != SchemaRef(PSS_SSS_TEMPLATE_SCHEMA_ID, V0_2):
            raise ValueError("PSS+SSS template has the wrong schema")
        if self.template_ref.digest != template_samples_digest(self.samples):
            raise ValueError("PSS+SSS reference does not identify its samples")
        require_finite(
            self.captured_template_energy_fraction,
            "captured_template_energy_fraction",
        )
        if not 0 < self.captured_template_energy_fraction <= 1:
            raise ValueError("captured PSS+SSS energy fraction must lie in (0, 1]")


@dataclass(frozen=True)
class StarlinkInjectionCaseV0_2:
    """One deterministic signal-present or null whole-search trial."""

    case_id: str
    seed: int
    sample_count: int
    signal_amplitude: float
    noise_standard_deviation: float
    epoch_sample: int
    cfo_hz: float
    cfo_drift_hz_per_s: float
    occupied_frame_indices: tuple[int, ...]

    def __post_init__(self) -> None:
        if not self.case_id or any(character.isspace() for character in self.case_id):
            raise ValueError("case_id must be a portable non-empty token")
        if (
            isinstance(self.seed, bool)
            or not isinstance(self.seed, int)
            or self.seed < 0
        ):
            raise ValueError("injection seed must be a non-negative integer")
        if (
            isinstance(self.sample_count, bool)
            or not isinstance(self.sample_count, int)
            or isinstance(self.epoch_sample, bool)
            or not isinstance(self.epoch_sample, int)
            or self.sample_count <= 0
            or self.epoch_sample < 0
        ):
            raise ValueError("injection dimensions must be non-negative and non-empty")
        for name in (
            "signal_amplitude",
            "noise_standard_deviation",
            "cfo_hz",
            "cfo_drift_hz_per_s",
        ):
            require_finite(getattr(self, name), name)
        if self.signal_amplitude < 0 or self.noise_standard_deviation < 0:
            raise ValueError("injection amplitude and noise scale cannot be negative")
        if (
            tuple(sorted(set(self.occupied_frame_indices)))
            != self.occupied_frame_indices
        ):
            raise ValueError("occupied frame indices must be sorted and unique")
        if any(value < 0 for value in self.occupied_frame_indices):
            raise ValueError("occupied frame indices must be non-negative")
        if self.signal_amplitude == 0 and self.occupied_frame_indices:
            raise ValueError("a null case cannot declare occupied frames")
        if self.signal_amplitude > 0 and not self.occupied_frame_indices:
            raise ValueError("a positive case must declare occupied frames")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class StarlinkInjectionTrialResultV0_2:
    case: StarlinkInjectionCaseV0_2
    samples_digest: Digest
    bundle: StarlinkDetectorSuiteBundleV0_2


@dataclass(frozen=True)
class StarlinkRadioCandidateObservationV0_2:
    radio_id: RadioId
    receiver_chain_id: ReceiverChainId
    channel_number: int
    edge: StarlinkEdge
    interval_start_utc_ns: UtcNs
    interval_stop_utc_ns: UtcNs
    observed_first_sample_skew_ns: int
    suite: StarlinkDetectorSuiteBundleV0_2


@dataclass(frozen=True)
class _Frames:
    rows: tuple[tuple[complex, ...], ...]
    moments_s: tuple[tuple[float, ...], ...]

    @property
    def support(self) -> int:
        return len(self.rows)


@dataclass(frozen=True)
class _Scored:
    score: float
    residual_cfo_hz: float
    summary: StarlinkFrameScoreSummaryV0_2


@dataclass(frozen=True)
class _Winner:
    epoch: int
    coarse_cfo_hz: float
    scored: _Scored


class StarlinkDetectorSuiteV0_2:
    """Run all eight report methods while keeping candidates non-decisional."""

    def __init__(
        self,
        config: StarlinkDetectorSuiteConfigV0_2,
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
        pss_sss_template: StarlinkPssSssTemplateV0_2 | None = None,
    ) -> StarlinkDetectorSuiteBundleV0_2:
        values = tuple(complex(value) for value in samples)
        self._validate_inputs(values, templates, pss_sss_template)
        algorithm_ref = starlink_detector_suite_algorithm_ref_v0_2()
        config_ref = starlink_detector_suite_config_ref_v0_2(self._config)
        suite_identity = canonical_digest(
            {
                "algorithm_digest": str(algorithm_ref.digest),
                "config_digest": str(config_ref.digest),
                "exact_template_digest": str(templates.exact_ref.digest),
                "control_template_digest": str(
                    templates.conditioned_control_ref.digest
                ),
                "pss_sss_template_digest": (
                    None
                    if pss_sss_template is None
                    else str(pss_sss_template.template_ref.digest)
                ),
                "recording_identity_digest": str(recording_identity_digest),
                "segment_id": str(segment_id),
                "receiver_chain_id": str(receiver_chain_id),
                "probe_sample_count": len(values),
                "sample_rate_hz": templates.sample_rate_hz,
                "edge": templates.edge.value,
                "methods": tuple(method.value for method in REPORT_METHOD_ORDER),
                "candidates_only": True,
            }
        )
        evidence = self._run_methods(
            values,
            templates,
            algorithm_ref=algorithm_ref,
            config_ref=config_ref,
            suite_identity=suite_identity,
        )
        pss_evidence = (
            None
            if pss_sss_template is None
            else self._pss_sss_search(values, pss_sss_template)
        )
        input_digest = canonical_digest(
            {
                "recording_identity_digest": str(recording_identity_digest),
                "segment_id": str(segment_id),
                "receiver_chain_id": str(receiver_chain_id),
            }
        )
        dependencies = [
            algorithm_ref.digest,
            templates.exact_ref.digest,
            templates.conditioned_control_ref.digest,
        ]
        if pss_sss_template is not None:
            dependencies.append(pss_sss_template.template_ref.digest)
        provenance = Provenance(
            self._execution.producer_name,
            self._execution.producer_version,
            self._execution.git_commit,
            self._execution.environment_digest,
            config_ref.digest,
            (input_digest,),
            tuple(dependencies),
            self._execution.started_utc_ns,
            self._execution.completed_utc_ns,
            self._execution.host_class,
        )
        token = canonical_digest(
            {"suite_identity_digest": str(suite_identity), "methods": evidence}
        ).value
        clipped = templates.sample_rate_hz < PILOT_BANDWIDTH_HZ
        warnings = ["uncalibrated-candidates-only"]
        if clipped:
            warnings.append("clipped-pilot-band-not-calibration-compatible")
        if pss_sss_template is None:
            warnings.append("pss-sss-supporting-acquisition-not-evaluated")
        elif pss_sss_template.captured_template_energy_fraction < 0.5:
            warnings.append("pss-sss-captured-energy-too-low-for-primary-detection")
        return StarlinkDetectorSuiteBundleV0_2(
            SchemaRef(StarlinkDetectorSuiteBundleV0_2.SCHEMA_ID, V0_2),
            f"sldetsuite_{token[:32]}",
            recording_id,
            recording_identity_digest,
            segment_id,
            receiver_chain_id,
            templates.edge,
            templates.sample_rate_hz,
            len(values),
            (
                StarlinkSamplingStratum.CLIPPED_PILOT_BAND
                if clipped
                else StarlinkSamplingStratum.FULL_PILOT_BAND
            ),
            suite_identity,
            self._config.symbol_split_digest,
            evidence,
            pss_evidence,
            provenance,
            True,
            tuple(warnings),
        )

    def analyze_full_search_control(
        self,
        samples: Sequence[complex],
        *,
        recording_id: RecordingId,
        recording_identity_digest: Digest,
        segment_id: SegmentId,
        receiver_chain_id: ReceiverChainId,
        templates: KnownCodePilotTemplatePairV0_1,
        condition_relative_on_acquire: bool = False,
    ) -> StarlinkFullSearchControlSuiteV0_1:
        """Search the rolled template independently over the target grid.

        This is additive evidence. It deliberately does not alter the published
        v0.2 same-cell control statistic.
        """

        values = tuple(complex(value) for value in samples)
        self._validate_inputs(values, templates, None)
        algorithm_ref = (
            starlink_acquire_conditioned_control_algorithm_ref_v0_1()
            if condition_relative_on_acquire
            else starlink_full_search_control_algorithm_ref_v0_1()
        )
        control_search = (
            "pattern-acquire-search-then-condition-report-methods"
            if condition_relative_on_acquire
            else "rolled-template-independent-full-search"
        )
        config_ref = starlink_detector_suite_config_ref_v0_2(self._config)
        suite_identity = canonical_digest(
            {
                "algorithm_digest": str(algorithm_ref.digest),
                "config_digest": str(config_ref.digest),
                "rolled_template_digest": str(templates.conditioned_control_ref.digest),
                "recording_identity_digest": str(recording_identity_digest),
                "segment_id": str(segment_id),
                "receiver_chain_id": str(receiver_chain_id),
                "probe_sample_count": len(values),
                "sample_rate_hz": templates.sample_rate_hz,
                "edge": templates.edge.value,
                "methods": tuple(method.value for method in REPORT_METHOD_ORDER),
                "control_search": control_search,
            }
        )
        evidence = self._run_full_search_controls(
            values,
            templates,
            algorithm_ref=algorithm_ref,
            config_ref=config_ref,
            suite_identity=suite_identity,
            condition_relative_on_acquire=condition_relative_on_acquire,
            control_search=control_search,
        )
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
                templates.conditioned_control_ref.digest,
            ),
            self._execution.started_utc_ns,
            self._execution.completed_utc_ns,
            self._execution.host_class,
        )
        token = canonical_digest(
            {"suite_identity_digest": str(suite_identity), "methods": evidence}
        ).value
        return StarlinkFullSearchControlSuiteV0_1(
            SchemaRef(
                StarlinkFullSearchControlSuiteV0_1.SCHEMA_ID,
                FULL_SEARCH_CONTROL_V0_1,
            ),
            f"slsctrl_{token[:32]}",
            recording_id,
            recording_identity_digest,
            segment_id,
            receiver_chain_id,
            templates.edge,
            templates.sample_rate_hz,
            len(values),
            suite_identity,
            evidence,
            provenance,
            True,
            (
                "not-an-empirical-null-distribution",
                "no-calibrated-detection-verdict",
            ),
        )

    def _run_full_search_controls(
        self,
        values: tuple[complex, ...],
        templates: KnownCodePilotTemplatePairV0_1,
        *,
        algorithm_ref: ArtifactRef,
        config_ref: ArtifactRef,
        suite_identity: Digest,
        condition_relative_on_acquire: bool = False,
        control_search: str = "rolled-template-independent-full-search",
    ) -> tuple[StarlinkFullSearchControlMethodEvidenceV0_1, ...]:
        anchors = _spread_symbols(8)
        relative_specs = (
            (StarlinkDetectorMethod.ANCHOR_8, anchors, "anchor"),
            (StarlinkDetectorMethod.DIFFERENTIAL_16, tuple(range(2, 18)), "contiguous"),
            (StarlinkDetectorMethod.DIFFERENTIAL_32, tuple(range(2, 34)), "contiguous"),
            (StarlinkDetectorMethod.GLRT_32, tuple(range(2, 34)), "contiguous"),
            (StarlinkDetectorMethod.GLRT_64, tuple(range(2, 66)), "contiguous"),
        )
        results: dict[
            StarlinkDetectorMethod, StarlinkFullSearchControlMethodEvidenceV0_1
        ] = {}
        acquire_winner = (
            self._search_full_frame_template_vectorized(
                values,
                templates.conditioned_control_samples,
                templates.sample_rate_hz,
                self._config.acquire_symbols,
            )
            if condition_relative_on_acquire
            else self._search_full_frame_template(
                values,
                templates.conditioned_control_samples,
                templates.sample_rate_hz,
                self._config.acquire_symbols,
            )
        )
        for method, symbols, role in relative_specs:
            if condition_relative_on_acquire:
                frames = _symbol_correlations(
                    values,
                    templates.conditioned_control_samples,
                    templates.sample_rate_hz,
                    acquire_winner.epoch,
                    acquire_winner.coarse_cfo_hz,
                    symbols,
                )
                scored = self._score_relative_frames(frames, method)
                winner = _Winner(
                    acquire_winner.epoch,
                    acquire_winner.coarse_cfo_hz,
                    scored,
                )
            else:
                winner = self._search_relative_template(
                    values,
                    templates.conditioned_control_samples,
                    templates.sample_rate_hz,
                    method,
                    symbols,
                )
            results[method] = self._full_search_control_evidence(
                method,
                algorithm_ref,
                config_ref,
                templates,
                suite_identity,
                (
                    StarlinkFullSearchControlMode.CONDITIONED_ON_ROLLED_ACQUIRE_WINNER
                    if condition_relative_on_acquire
                    else StarlinkFullSearchControlMode.SEARCHED_ROLLED_TEMPLATE
                ),
                (
                    StarlinkDetectorMethod.FULL_FRAME_ACQUIRE
                    if condition_relative_on_acquire
                    else method
                ),
                (
                    self._config.glrt_effective_search_cell_count
                    if method
                    in (StarlinkDetectorMethod.GLRT_32, StarlinkDetectorMethod.GLRT_64)
                    else self._config.outer_search_cell_count
                ),
                winner,
                symbols,
                role,
                None,
                "rolled-template-independent-full-search",
            )

        for method, symbols, role, mode in (
            (
                StarlinkDetectorMethod.FULL_FRAME_ACQUIRE,
                self._config.acquire_symbols,
                "acquire",
                StarlinkFullSearchControlMode.SEARCHED_ROLLED_TEMPLATE,
            ),
            (
                StarlinkDetectorMethod.FULL_FRAME_VERIFY,
                self._config.verify_symbols,
                "verify",
                StarlinkFullSearchControlMode.CONDITIONED_ON_ROLLED_ACQUIRE_WINNER,
            ),
            (
                StarlinkDetectorMethod.FULL_FRAME_FULL,
                tuple(range(2, 302)),
                "full",
                StarlinkFullSearchControlMode.CONDITIONED_ON_ROLLED_ACQUIRE_WINNER,
            ),
        ):
            scored = _full_frame_score(
                values,
                templates.conditioned_control_samples,
                templates.sample_rate_hz,
                acquire_winner.epoch,
                acquire_winner.coarse_cfo_hz,
                symbols,
            )
            winner = _Winner(
                acquire_winner.epoch,
                acquire_winner.coarse_cfo_hz,
                scored,
            )
            results[method] = self._full_search_control_evidence(
                method,
                algorithm_ref,
                config_ref,
                templates,
                suite_identity,
                mode,
                StarlinkDetectorMethod.FULL_FRAME_ACQUIRE,
                self._config.outer_search_cell_count,
                winner,
                symbols,
                role,
                self._config.symbol_split_digest,
                "rolled-template-independent-full-search",
            )
        return tuple(results[method] for method in REPORT_METHOD_ORDER)

    def _validate_inputs(
        self,
        values: tuple[complex, ...],
        templates: KnownCodePilotTemplatePairV0_1,
        pss_sss_template: StarlinkPssSssTemplateV0_2 | None,
    ) -> None:
        if not values:
            raise ValueError("detector suite requires non-empty samples")
        if len(values) > self._config.maximum_probe_samples:
            raise ValueError("detector suite probe exceeds maximum_probe_samples")
        if any(not _finite_complex(value) for value in values):
            raise ValueError("detector suite samples must be finite")
        if pss_sss_template is not None and not math.isclose(
            templates.sample_rate_hz,
            pss_sss_template.sample_rate_hz,
            rel_tol=0,
            abs_tol=1e-9,
        ):
            raise ValueError("pilot and PSS+SSS template sample rates differ")
        maximum_frames = math.ceil(
            len(values) / (templates.sample_rate_hz / FRAME_RATE_HZ)
        )
        if (
            maximum_frames * len(REPORT_METHOD_ORDER)
            > self._config.maximum_frame_summaries
        ):
            raise ValueError("declared analysis exceeds maximum_frame_summaries")

    def _run_methods(
        self,
        values: tuple[complex, ...],
        templates: KnownCodePilotTemplatePairV0_1,
        *,
        algorithm_ref: ArtifactRef,
        config_ref: ArtifactRef,
        suite_identity: Digest,
    ) -> tuple[StarlinkDetectorMethodEvidenceV0_2, ...]:
        anchors = _spread_symbols(8)
        relative_specs = (
            (StarlinkDetectorMethod.ANCHOR_8, anchors, "anchor"),
            (StarlinkDetectorMethod.DIFFERENTIAL_16, tuple(range(2, 18)), "contiguous"),
            (StarlinkDetectorMethod.DIFFERENTIAL_32, tuple(range(2, 34)), "contiguous"),
            (StarlinkDetectorMethod.GLRT_32, tuple(range(2, 34)), "contiguous"),
            (StarlinkDetectorMethod.GLRT_64, tuple(range(2, 66)), "contiguous"),
        )
        results: dict[StarlinkDetectorMethod, StarlinkDetectorMethodEvidenceV0_2] = {}
        for method, symbols, role in relative_specs:
            winner = self._search_relative(values, templates, method, symbols)
            exact = self._condition_relative(
                values,
                templates.exact_samples,
                templates.sample_rate_hz,
                method,
                symbols,
                winner.epoch,
                winner.coarse_cfo_hz,
                winner.scored.residual_cfo_hz,
            )
            control = self._condition_relative(
                values,
                templates.conditioned_control_samples,
                templates.sample_rate_hz,
                method,
                symbols,
                winner.epoch,
                winner.coarse_cfo_hz,
                winner.scored.residual_cfo_hz,
            )
            results[method] = self._evidence(
                method,
                algorithm_ref,
                config_ref,
                templates,
                suite_identity,
                StarlinkSearchMode.SEARCHED_EXACT,
                method,
                (
                    self._config.glrt_effective_search_cell_count
                    if method
                    in (StarlinkDetectorMethod.GLRT_32, StarlinkDetectorMethod.GLRT_64)
                    else self._config.outer_search_cell_count
                ),
                winner,
                exact,
                control,
                symbols,
                role,
                None,
            )

        acquire_winner = self._search_full_frame(
            values,
            templates,
            self._config.acquire_symbols,
        )
        for method, symbols, role, mode in (
            (
                StarlinkDetectorMethod.FULL_FRAME_ACQUIRE,
                self._config.acquire_symbols,
                "acquire",
                StarlinkSearchMode.SEARCHED_EXACT,
            ),
            (
                StarlinkDetectorMethod.FULL_FRAME_VERIFY,
                self._config.verify_symbols,
                "verify",
                StarlinkSearchMode.CONDITIONED_ON_ACQUIRE_WINNER,
            ),
            (
                StarlinkDetectorMethod.FULL_FRAME_FULL,
                tuple(range(2, 302)),
                "full",
                StarlinkSearchMode.CONDITIONED_ON_ACQUIRE_WINNER,
            ),
        ):
            exact = _full_frame_score(
                values,
                templates.exact_samples,
                templates.sample_rate_hz,
                acquire_winner.epoch,
                acquire_winner.coarse_cfo_hz,
                symbols,
            )
            control = _full_frame_score(
                values,
                templates.conditioned_control_samples,
                templates.sample_rate_hz,
                acquire_winner.epoch,
                acquire_winner.coarse_cfo_hz,
                symbols,
            )
            point = _Winner(
                acquire_winner.epoch,
                acquire_winner.coarse_cfo_hz,
                exact,
            )
            results[method] = self._evidence(
                method,
                algorithm_ref,
                config_ref,
                templates,
                suite_identity,
                mode,
                StarlinkDetectorMethod.FULL_FRAME_ACQUIRE,
                self._config.outer_search_cell_count,
                point,
                exact,
                control,
                symbols,
                role,
                self._config.symbol_split_digest,
            )
        return tuple(results[method] for method in REPORT_METHOD_ORDER)

    def _search_relative(
        self,
        values: tuple[complex, ...],
        templates: KnownCodePilotTemplatePairV0_1,
        method: StarlinkDetectorMethod,
        symbols: tuple[int, ...],
    ) -> _Winner:
        return self._search_relative_template(
            values,
            templates.exact_samples,
            templates.sample_rate_hz,
            method,
            symbols,
        )

    def _search_relative_template(
        self,
        values: tuple[complex, ...],
        template: tuple[complex, ...],
        sample_rate_hz: float,
        method: StarlinkDetectorMethod,
        symbols: tuple[int, ...],
    ) -> _Winner:
        candidates = []
        for epoch in self._config.epoch_hypotheses_samples:
            for coarse in self._config.coarse_cfo_hypotheses_hz:
                frames = _symbol_correlations(
                    values,
                    template,
                    sample_rate_hz,
                    epoch,
                    coarse,
                    symbols,
                )
                if not frames.support:
                    continue
                scored = self._score_relative_frames(frames, method)
                candidates.append(_Winner(epoch, coarse, scored))
        if not candidates:
            raise ValueError(f"no complete frame supports {method.value}")
        return max(candidates, key=_winner_key)

    def _score_relative_frames(
        self, frames: _Frames, method: StarlinkDetectorMethod
    ) -> _Scored:
        if not frames.support:
            return _Scored(0.0, 0.0, _empty_summary())
        if method is StarlinkDetectorMethod.ANCHOR_8:
            return _coherent_symbol_score(frames, 0.0)
        if method in (
            StarlinkDetectorMethod.DIFFERENTIAL_16,
            StarlinkDetectorMethod.DIFFERENTIAL_32,
        ):
            return _differential_score(frames)
        return max(
            (
                _coherent_symbol_score(frames, residual)
                for residual in self._config.glrt_residual_cfo_hypotheses_hz
            ),
            key=lambda item: (
                item.score,
                -abs(item.residual_cfo_hz),
                -item.residual_cfo_hz,
            ),
        )

    def _condition_relative(
        self,
        values: tuple[complex, ...],
        template: tuple[complex, ...],
        sample_rate_hz: float,
        method: StarlinkDetectorMethod,
        symbols: tuple[int, ...],
        epoch: int,
        coarse_cfo_hz: float,
        residual_cfo_hz: float,
    ) -> _Scored:
        frames = _symbol_correlations(
            values,
            template,
            sample_rate_hz,
            epoch,
            coarse_cfo_hz,
            symbols,
        )
        if method in (
            StarlinkDetectorMethod.DIFFERENTIAL_16,
            StarlinkDetectorMethod.DIFFERENTIAL_32,
        ):
            scored = _differential_score(frames)
            return _Scored(scored.score, residual_cfo_hz, scored.summary)
        return _coherent_symbol_score(frames, residual_cfo_hz)

    def _search_full_frame(
        self,
        values: tuple[complex, ...],
        templates: KnownCodePilotTemplatePairV0_1,
        symbols: tuple[int, ...],
    ) -> _Winner:
        return self._search_full_frame_template(
            values,
            templates.exact_samples,
            templates.sample_rate_hz,
            symbols,
        )

    def _search_full_frame_template(
        self,
        values: tuple[complex, ...],
        template: tuple[complex, ...],
        sample_rate_hz: float,
        symbols: tuple[int, ...],
    ) -> _Winner:
        candidates = []
        for epoch in self._config.epoch_hypotheses_samples:
            for cfo in self._config.coarse_cfo_hypotheses_hz:
                scored = _full_frame_score(
                    values,
                    template,
                    sample_rate_hz,
                    epoch,
                    cfo,
                    symbols,
                )
                if scored.summary.support:
                    candidates.append(_Winner(epoch, cfo, scored))
        if not candidates:
            raise ValueError("no complete frame supports full-frame acquisition")
        return max(candidates, key=_winner_key)

    def _search_full_frame_template_vectorized(
        self,
        values: tuple[complex, ...],
        template: tuple[complex, ...],
        sample_rate_hz: float,
        symbols: tuple[int, ...],
    ) -> _Winner:
        """Search the same cells using matrix evaluation for adaptive overlays."""

        indexes_tuple = _pilot_sample_indexes(sample_rate_hz, len(template), symbols)
        if not indexes_tuple:
            raise ValueError("no pilot samples support full-frame acquisition")
        indexes = np.asarray(indexes_tuple, dtype=np.int64)
        received_values = np.asarray(values, dtype=np.complex128)
        selected_template = np.asarray(template, dtype=np.complex128)[indexes]
        template_energy = math.fsum(
            abs(template[index]) ** 2 for index in indexes_tuple
        )
        reference_rows = np.asarray(
            [
                selected_template.conjugate()
                * np.exp(-2j * math.pi * coarse_cfo_hz * indexes / sample_rate_hz)
                for coarse_cfo_hz in self._config.coarse_cfo_hypotheses_hz
            ],
            dtype=np.complex128,
        )
        period = sample_rate_hz / FRAME_RATE_HZ
        candidates: list[_Winner] = []
        for epoch in self._config.epoch_hypotheses_samples:
            starts = []
            frame = 0
            while True:
                start = epoch + round(frame * period)
                if start + indexes_tuple[-1] >= len(values):
                    break
                starts.append(start)
                frame += 1
            if not starts:
                continue
            rows = received_values[
                np.asarray(starts, dtype=np.int64)[:, None] + indexes[None, :]
            ]
            data_energy = np.sum(np.abs(rows) ** 2, axis=1, dtype=np.float64)
            denominators = np.sqrt(template_energy * data_energy)
            numerators = rows @ reference_rows.T
            ratios = np.divide(
                np.abs(numerators),
                denominators[:, None],
                out=np.zeros_like(numerators.real),
                where=denominators[:, None] != 0,
            )
            for cfo_index, coarse_cfo_hz in enumerate(
                self._config.coarse_cfo_hypotheses_hz
            ):
                per_frame = tuple(float(value) for value in ratios[:, cfo_index])
                scored = _Scored(
                    _bounded_score(math.fsum(per_frame) / len(per_frame)),
                    0.0,
                    _summary(per_frame),
                )
                candidates.append(_Winner(epoch, coarse_cfo_hz, scored))
        if not candidates:
            raise ValueError("no complete frame supports full-frame acquisition")
        return max(candidates, key=_winner_key)

    def _full_search_control_evidence(
        self,
        method: StarlinkDetectorMethod,
        algorithm_ref: ArtifactRef,
        config_ref: ArtifactRef,
        templates: KnownCodePilotTemplatePairV0_1,
        suite_identity: Digest,
        search_mode: StarlinkFullSearchControlMode,
        selection_method: StarlinkDetectorMethod,
        effective_search_cells: int,
        winner: _Winner,
        symbols: tuple[int, ...],
        role: str,
        split_digest: Digest | None,
        control_search: str,
    ) -> StarlinkFullSearchControlMethodEvidenceV0_1:
        identity = canonical_digest(
            {
                "suite_identity_digest": str(suite_identity),
                "method": method.value,
                "search_mode": search_mode.value,
                "selection_method": selection_method.value,
                "effective_search_cell_count": effective_search_cells,
                "symbols": symbols,
                "control_search": control_search,
            }
        )
        return StarlinkFullSearchControlMethodEvidenceV0_1(
            SchemaRef(
                StarlinkFullSearchControlMethodEvidenceV0_1.SCHEMA_ID,
                FULL_SEARCH_CONTROL_V0_1,
            ),
            method,
            algorithm_ref,
            config_ref,
            templates.conditioned_control_ref,
            identity,
            search_mode,
            selection_method,
            effective_search_cells,
            winner.epoch,
            winner.coarse_cfo_hz,
            winner.scored.residual_cfo_hz,
            winner.scored.score,
            winner.scored.summary,
            symbols,
            role,
            split_digest,
            control_search,
            True,
            (
                "same-hypothesis-grid-as-target",
                "surrogate-control-not-verified-signal-absent",
                "no-calibrated-detection-verdict",
            ),
        )

    def _evidence(
        self,
        method: StarlinkDetectorMethod,
        algorithm_ref: ArtifactRef,
        config_ref: ArtifactRef,
        templates: KnownCodePilotTemplatePairV0_1,
        suite_identity: Digest,
        search_mode: StarlinkSearchMode,
        selection_method: StarlinkDetectorMethod,
        effective_search_cells: int,
        winner: _Winner,
        exact: _Scored,
        control: _Scored,
        symbols: tuple[int, ...],
        role: str,
        split_digest: Digest | None,
    ) -> StarlinkDetectorMethodEvidenceV0_2:
        method_search_identity = canonical_digest(
            {
                "suite_identity_digest": str(suite_identity),
                "method": method.value,
                "search_mode": search_mode.value,
                "selection_method": selection_method.value,
                "effective_search_cell_count": effective_search_cells,
                "symbols": symbols,
                "control_conditioning": (
                    "exact-winning-epoch-coarse-and-residual-cfo-fixed"
                ),
            }
        )
        return StarlinkDetectorMethodEvidenceV0_2(
            SchemaRef(StarlinkDetectorMethodEvidenceV0_2.SCHEMA_ID, V0_2),
            method,
            algorithm_ref,
            config_ref,
            templates.exact_ref,
            templates.conditioned_control_ref,
            method_search_identity,
            search_mode,
            selection_method,
            effective_search_cells,
            winner.epoch,
            winner.coarse_cfo_hz,
            exact.residual_cfo_hz,
            exact.score,
            exact.score,
            control.score,
            exact.score - control.score,
            exact.summary,
            control.summary,
            symbols,
            role,
            split_digest,
            "exact-winning-epoch-coarse-and-residual-cfo-fixed",
            True,
            (
                "search-maximum-or-selected-point-not-a-detection",
                "whole-search-calibration-required",
                "per-frame-phase-not-coherently-combined",
            ),
        )

    def _pss_sss_search(
        self,
        values: tuple[complex, ...],
        template: StarlinkPssSssTemplateV0_2,
    ) -> StarlinkPssSssAcquisitionEvidenceV0_2:
        candidates: list[_Winner] = []
        for epoch in self._config.epoch_hypotheses_samples:
            for cfo in self._config.coarse_cfo_hypotheses_hz:
                scored = _repeated_template_score(
                    values,
                    template.samples,
                    template.sample_rate_hz,
                    epoch,
                    cfo,
                )
                if scored.summary.support:
                    candidates.append(_Winner(epoch, cfo, scored))
        if not candidates:
            raise ValueError("no complete PSS+SSS template fits a declared search cell")
        winner = max(candidates, key=_winner_key)
        conditioned = _repeated_template_score(
            values,
            template.samples,
            template.sample_rate_hz,
            winner.epoch,
            winner.coarse_cfo_hz,
        )
        identity = canonical_digest(
            {
                "template_digest": str(template.template_ref.digest),
                "epoch_hypotheses_samples": self._config.epoch_hypotheses_samples,
                "doppler_hypotheses_hz": self._config.coarse_cfo_hypotheses_hz,
                "probe_sample_count": len(values),
                "statistic": "mean-per-frame-normalized-correlation-magnitude",
            }
        )
        reasons = ["supporting-acquisition-only", "not-edge-pilot-detection"]
        if template.captured_template_energy_fraction < 0.5:
            reasons.append("captured-template-energy-fraction-below-half")
        return StarlinkPssSssAcquisitionEvidenceV0_2(
            SchemaRef(StarlinkPssSssAcquisitionEvidenceV0_2.SCHEMA_ID, V0_2),
            template.template_ref,
            identity,
            self._config.outer_search_cell_count,
            winner.epoch,
            winner.coarse_cfo_hz,
            winner.scored.score,
            conditioned.score,
            conditioned.summary.support,
            template.captured_template_energy_fraction,
            True,
            tuple(reasons),
        )


def starlink_detector_suite_algorithm_ref_v0_2() -> ArtifactRef:
    return ArtifactRef(
        "starlink-report-detector-suite-v0.2",
        canonical_digest(
            {
                "algorithm_id": ALGORITHM_ID,
                "algorithm_version": ALGORITHM_VERSION,
                "methods": tuple(method.value for method in REPORT_METHOD_ORDER),
                "frame_rate_hz": FRAME_RATE_HZ,
                "ofdm_symbol_duration_s": OFDM_SYMBOL_DURATION_S,
                "pilot_symbol_range": (2, 301),
                "control_symbol_roll": CONTROL_SYMBOL_ROLL,
                "control_conditioning": (
                    "exact-winning-epoch-coarse-and-residual-cfo-fixed"
                ),
                "inter_frame_combination": "noncoherent-or-phase-cancelled",
                "full_frame_selection": "acquire-search-then-disjoint-verify",
                "decision": "none-without-exact-whole-search-calibration",
            }
        ),
        SchemaRef("org.leo-flow.recording-algorithm", V0_2),
    )


def starlink_full_search_control_algorithm_ref_v0_1() -> ArtifactRef:
    return ArtifactRef(
        "starlink-rolled-template-full-search-control-v0.1",
        canonical_digest(
            {
                "algorithm_id": FULL_SEARCH_CONTROL_ALGORITHM_ID,
                "algorithm_version": FULL_SEARCH_CONTROL_ALGORITHM_VERSION,
                "target_suite_algorithm": str(
                    starlink_detector_suite_algorithm_ref_v0_2().digest
                ),
                "semantics": "rolled-template-independent-full-search",
                "methods": tuple(method.value for method in REPORT_METHOD_ORDER),
            }
        ),
        SchemaRef("org.leo-flow.recording-algorithm", FULL_SEARCH_CONTROL_V0_1),
    )


def starlink_acquire_conditioned_control_algorithm_ref_v0_1() -> ArtifactRef:
    return ArtifactRef(
        "starlink-pattern-acquire-conditioned-control-v0.1",
        canonical_digest(
            {
                "algorithm_id": ACQUIRE_CONDITIONED_CONTROL_ALGORITHM_ID,
                "algorithm_version": ACQUIRE_CONDITIONED_CONTROL_ALGORITHM_VERSION,
                "target_suite_algorithm": str(
                    starlink_detector_suite_algorithm_ref_v0_2().digest
                ),
                "selection": "independent-full-frame-acquire-per-pattern",
                "relative_methods": "conditioned-on-pattern-acquire-winner",
                "methods": tuple(method.value for method in REPORT_METHOD_ORDER),
            }
        ),
        SchemaRef("org.leo-flow.recording-algorithm", FULL_SEARCH_CONTROL_V0_1),
    )


def starlink_detector_suite_config_ref_v0_2(
    config: StarlinkDetectorSuiteConfigV0_2,
) -> ArtifactRef:
    return ArtifactRef(
        "starlink-report-detector-suite-config-v0.2",
        canonical_digest(config),
        SchemaRef(CONFIG_SCHEMA_ID, V0_2),
    )


def synthesize_starlink_injection_v0_2(
    templates: KnownCodePilotTemplatePairV0_1,
    case: StarlinkInjectionCaseV0_2,
    *,
    maximum_samples: int = 6_400_000,
) -> tuple[complex, ...]:
    """Create a deterministic null or exact Qin-pilot injection trial."""

    if case.sample_count > maximum_samples:
        raise ValueError("injection case exceeds maximum_samples")
    rng = random.Random(case.seed)
    scale = case.noise_standard_deviation / math.sqrt(2)
    values = [
        complex(rng.gauss(0.0, scale), rng.gauss(0.0, scale))
        for _ in range(case.sample_count)
    ]
    period = templates.sample_rate_hz / FRAME_RATE_HZ
    for frame in case.occupied_frame_indices:
        start = case.epoch_sample + round(frame * period)
        if start + len(templates.exact_samples) > len(values):
            raise ValueError("occupied injection frame falls outside the trial")
        frame_phase = cmath.exp(1j * rng.uniform(-math.pi, math.pi))
        for local_index, reference in enumerate(templates.exact_samples):
            sample_index = start + local_index
            time_s = sample_index / templates.sample_rate_hz
            phase_cycles = (
                case.cfo_hz * time_s + 0.5 * case.cfo_drift_hz_per_s * time_s * time_s
            )
            values[sample_index] += (
                case.signal_amplitude
                * frame_phase
                * cmath.exp(2j * math.pi * phase_cycles)
                * reference
            )
    return tuple(values)


def run_starlink_injection_cases_v0_2(
    analyzer: StarlinkDetectorSuiteV0_2,
    templates: KnownCodePilotTemplatePairV0_1,
    cases: Sequence[StarlinkInjectionCaseV0_2],
    *,
    recording_namespace: str = "injection",
    maximum_cases: int = 10_000,
) -> tuple[StarlinkInjectionTrialResultV0_2, ...]:
    """Run independent complete searches; output scores, never verdict bits."""

    if not cases:
        raise ValueError("injection evaluation requires at least one case")
    if (
        isinstance(maximum_cases, bool)
        or not isinstance(maximum_cases, int)
        or maximum_cases <= 0
    ):
        raise ValueError("maximum_cases must be a positive integer")
    if len(cases) > maximum_cases:
        raise ValueError("injection evaluation exceeds maximum_cases")
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("injection evaluation case identities must be unique")
    results = []
    for case in cases:
        values = synthesize_starlink_injection_v0_2(templates, case)
        samples_digest = _complex_samples_digest(values)
        bundle = analyzer.analyze_receiver(
            values,
            recording_id=RecordingId(f"rec_{recording_namespace}_{case.case_id}"),
            recording_identity_digest=canonical_digest(
                {"case_digest": str(case.digest), "samples_digest": str(samples_digest)}
            ),
            segment_id=SegmentId(f"seg_{recording_namespace}_{case.case_id}"),
            receiver_chain_id=ReceiverChainId("rx_injection_fixture"),
            templates=templates,
        )
        results.append(StarlinkInjectionTrialResultV0_2(case, samples_digest, bundle))
    return tuple(results)


def build_multi_radio_candidate_evidence_v0_2(
    observations: Sequence[StarlinkRadioCandidateObservationV0_2],
    *,
    maximum_time_gap_ns: int,
    maximum_cfo_span_hz: float,
    maximum_observations: int = 64,
) -> StarlinkMultiRadioCandidateEvidenceV0_2:
    """Corroborate candidates noncoherently without inventing sync or verdicts."""

    if len(observations) < 2:
        raise ValueError("multi-radio evidence requires at least two observations")
    if (
        isinstance(maximum_observations, bool)
        or not isinstance(maximum_observations, int)
        or maximum_observations < 2
    ):
        raise ValueError("maximum_observations must be an integer of at least two")
    if len(observations) > maximum_observations:
        raise ValueError("multi-radio evidence exceeds maximum_observations")
    if any(item.observed_first_sample_skew_ns < 0 for item in observations):
        raise ValueError("observed first-sample skew must be non-negative")
    if maximum_time_gap_ns < 0:
        raise ValueError("maximum time gap must be non-negative")
    require_finite(maximum_cfo_span_hz, "maximum_cfo_span_hz")
    if maximum_cfo_span_hz < 0:
        raise ValueError("maximum CFO span must be non-negative")
    first = observations[0]
    if any(
        item.suite.receiver_chain_id != item.receiver_chain_id
        or item.suite.edge is not item.edge
        for item in observations
    ):
        raise ValueError("observation identity conflicts with its detector suite")
    if any(
        item.channel_number != first.channel_number or item.edge is not first.edge
        for item in observations
    ):
        raise ValueError("multi-radio observations must name one channel edge")
    radios = tuple(sorted({item.radio_id for item in observations}))
    if len(radios) < 2:
        raise ValueError("observations do not contain two distinct radios")
    starts = [int(item.interval_start_utc_ns) for item in observations]
    stops = [int(item.interval_stop_utc_ns) for item in observations]
    if max(starts) > min(stops) + maximum_time_gap_ns:
        raise ValueError("multi-radio candidate intervals are not compatible")
    verify_cfos = [
        _method(
            item.suite, StarlinkDetectorMethod.FULL_FRAME_VERIFY
        ).winning_coarse_cfo_hz
        + _method(
            item.suite, StarlinkDetectorMethod.FULL_FRAME_VERIFY
        ).winning_residual_cfo_hz
        for item in observations
    ]
    observed_span = max(verify_cfos) - min(verify_cfos)
    if observed_span > maximum_cfo_span_hz:
        raise ValueError("multi-radio candidate CFO span exceeds the declared bound")
    receivers = tuple(sorted({item.receiver_chain_id for item in observations}))
    if len(receivers) != len(radios):
        raise ValueError("one distinct receiver chain is required per radio")
    by_radio = {}
    for item in observations:
        if item.radio_id in by_radio:
            raise ValueError("candidate evidence accepts one suite per radio")
        by_radio[item.radio_id] = item.suite.ref
    suite_refs = tuple(by_radio[radio] for radio in radios)
    if len({ref.digest for ref in suite_refs}) != len(suite_refs):
        raise ValueError("multi-radio evidence cannot count one suite twice")
    identity = canonical_digest(
        {
            "channel_number": first.channel_number,
            "edge": first.edge.value,
            "radios": radios,
            "suite_digests": tuple(str(ref.digest) for ref in suite_refs),
            "maximum_time_gap_ns": maximum_time_gap_ns,
            "maximum_cfo_span_hz": maximum_cfo_span_hz,
        }
    ).value
    return StarlinkMultiRadioCandidateEvidenceV0_2(
        SchemaRef(StarlinkMultiRadioCandidateEvidenceV0_2.SCHEMA_ID, V0_2),
        f"slmultiradio_{identity[:32]}",
        first.channel_number,
        first.edge,
        UtcNs(min(starts)),
        UtcNs(max(stops)),
        max(item.observed_first_sample_skew_ns for item in observations),
        observed_span,
        radios,
        receivers,
        suite_refs,
        "software-coordinated-multi-radio",
        "none-noncoherent-evidence-only",
        True,
        (
            "candidate-corroboration-not-a-detection",
            "calibrated-stream-decisions-required-for-event",
            "no-hardware-synchronization-claim",
        ),
    )


def _symbol_correlations(
    values: tuple[complex, ...],
    template: tuple[complex, ...],
    sample_rate_hz: float,
    epoch_sample: int,
    coarse_cfo_hz: float,
    symbols: tuple[int, ...],
) -> _Frames:
    period = sample_rate_hz / FRAME_RATE_HZ
    symbol_period = sample_rate_hz * OFDM_SYMBOL_DURATION_S
    rows: list[tuple[complex, ...]] = []
    moments: list[tuple[float, ...]] = []
    frame = 0
    while True:
        frame_start = epoch_sample + round(frame * period)
        if frame_start >= len(values):
            break
        row = []
        row_moments = []
        complete = True
        for symbol in symbols:
            local_start = round(symbol * symbol_period)
            local_stop = min(round((symbol + 1) * symbol_period), len(template))
            count = local_stop - local_start
            start = frame_start + local_start
            if count < 2 or start + count > len(values):
                complete = False
                break
            phase = cmath.exp(-2j * math.pi * coarse_cfo_hz * start / sample_rate_hz)
            step = cmath.exp(-2j * math.pi * coarse_cfo_hz / sample_rate_hz)
            numerator = 0j
            for offset in range(count):
                numerator += (
                    template[local_start + offset].conjugate()
                    * values[start + offset]
                    * phase
                )
                phase *= step
            row.append(numerator)
            row_moments.append((local_start + (count - 1) / 2) / sample_rate_hz)
        if not complete:
            break
        rows.append(tuple(row))
        moments.append(tuple(row_moments))
        frame += 1
    return _Frames(tuple(rows), tuple(moments))


def _coherent_symbol_score(frames: _Frames, residual_cfo_hz: float) -> _Scored:
    numerators = []
    denominators = []
    for row, moments in zip(frames.rows, frames.moments_s, strict=True):
        origin = moments[0]
        coherent = sum(
            value * cmath.exp(-2j * math.pi * residual_cfo_hz * (moment - origin))
            for value, moment in zip(row, moments, strict=True)
        )
        numerators.append(abs(coherent) ** 2)
        denominators.append(math.fsum(abs(value) for value in row) ** 2)
    return _ratio_score(numerators, denominators, residual_cfo_hz)


def _differential_score(frames: _Frames) -> _Scored:
    frame_vectors = []
    frame_weights = []
    for row in frames.rows:
        products = tuple(
            row[index + 1] * row[index].conjugate() for index in range(len(row) - 1)
        )
        frame_vectors.append(sum(products, 0j))
        frame_weights.append(
            math.fsum(
                abs(row[index + 1]) * abs(row[index]) for index in range(len(row) - 1)
            )
        )
    total = sum(frame_vectors, 0j)
    weight = math.fsum(frame_weights)
    score = _bounded_score(abs(total) / weight if weight else 0.0)
    per_frame = tuple(
        abs(value) / denominator if denominator else 0.0
        for value, denominator in zip(frame_vectors, frame_weights, strict=True)
    )
    step_s = (
        _median(
            moment[index + 1] - moment[index]
            for moment in frames.moments_s
            for index in range(len(moment) - 1)
        )
        if frames.rows
        else OFDM_SYMBOL_DURATION_S
    )
    residual = (
        math.atan2(total.imag, total.real) / (2 * math.pi * step_s) if total else 0.0
    )
    return _Scored(score, residual, _summary(per_frame))


def _full_frame_score(
    values: tuple[complex, ...],
    template: tuple[complex, ...],
    sample_rate_hz: float,
    epoch_sample: int,
    cfo_hz: float,
    symbols: tuple[int, ...],
) -> _Scored:
    indexes = _pilot_sample_indexes(sample_rate_hz, len(template), symbols)
    if not indexes:
        return _Scored(0.0, 0.0, _empty_summary())
    template_energy = math.fsum(abs(template[index]) ** 2 for index in indexes)
    period = sample_rate_hz / FRAME_RATE_HZ
    per_frame = []
    frame = 0
    while True:
        start = epoch_sample + round(frame * period)
        if start + indexes[-1] >= len(values):
            break
        numerator = 0j
        data_energy = 0.0
        for index in indexes:
            phase = cmath.exp(-2j * math.pi * cfo_hz * index / sample_rate_hz)
            received = values[start + index]
            numerator += template[index].conjugate() * received * phase
            data_energy += abs(received) ** 2
        denominator = math.sqrt(template_energy * data_energy)
        per_frame.append(abs(numerator) / denominator if denominator else 0.0)
        frame += 1
    return _Scored(
        _bounded_score(math.fsum(per_frame) / len(per_frame) if per_frame else 0.0),
        0.0,
        _summary(per_frame),
    )


def _repeated_template_score(
    values: tuple[complex, ...],
    template: tuple[complex, ...],
    sample_rate_hz: float,
    epoch_sample: int,
    cfo_hz: float,
) -> _Scored:
    template_energy = math.fsum(abs(value) ** 2 for value in template)
    period = sample_rate_hz / FRAME_RATE_HZ
    per_frame = []
    frame = 0
    while True:
        start = epoch_sample + round(frame * period)
        if start + len(template) > len(values):
            break
        numerator = 0j
        data_energy = 0.0
        phase = 1 + 0j
        step = cmath.exp(-2j * math.pi * cfo_hz / sample_rate_hz)
        for offset, reference in enumerate(template):
            received = values[start + offset]
            numerator += reference.conjugate() * received * phase
            data_energy += abs(received) ** 2
            phase *= step
        denominator = math.sqrt(template_energy * data_energy)
        per_frame.append(abs(numerator) / denominator if denominator else 0.0)
        frame += 1
    return _Scored(
        _bounded_score(math.fsum(per_frame) / len(per_frame) if per_frame else 0.0),
        0.0,
        _summary(per_frame),
    )


def _ratio_score(
    numerators: Sequence[float],
    denominators: Sequence[float],
    residual_cfo_hz: float,
) -> _Scored:
    total_denominator = math.fsum(denominators)
    score = _bounded_score(
        math.fsum(numerators) / total_denominator if total_denominator else 0.0
    )
    per_frame = tuple(
        numerator / denominator if denominator else 0.0
        for numerator, denominator in zip(numerators, denominators, strict=True)
    )
    return _Scored(score, residual_cfo_hz, _summary(per_frame))


def _summary(values: Iterable[float]) -> StarlinkFrameScoreSummaryV0_2:
    frozen = tuple(float(min(1.0, max(0.0, value))) for value in values)
    if not frozen:
        return _empty_summary()
    return StarlinkFrameScoreSummaryV0_2(
        math.fsum(frozen) / len(frozen),
        max(frozen),
        len(frozen),
    )


def _bounded_score(value: float) -> float:
    """Clamp normalized floating arithmetic to its mathematical [0, 1] range."""

    if not math.isfinite(value):
        raise ValueError("normalized detector score is not finite")
    return float(min(1.0, max(0.0, value)))


def _empty_summary() -> StarlinkFrameScoreSummaryV0_2:
    # Internal sentinel only.  Method evidence rejects zero-support summaries.
    return StarlinkFrameScoreSummaryV0_2(0.0, 0.0, 0)


def _winner_key(item: _Winner) -> tuple[float, float, int, float, float]:
    return (
        item.scored.score,
        -abs(item.coarse_cfo_hz + item.scored.residual_cfo_hz),
        -item.epoch,
        -item.coarse_cfo_hz,
        -item.scored.residual_cfo_hz,
    )


def _pilot_sample_indexes(
    sample_rate_hz: float,
    template_length: int,
    symbols: tuple[int, ...],
) -> tuple[int, ...]:
    symbol_period = sample_rate_hz * OFDM_SYMBOL_DURATION_S
    indexes: list[int] = []
    for symbol in symbols:
        start = round(symbol * symbol_period)
        stop = min(round((symbol + 1) * symbol_period), template_length)
        if stop - start < 2:
            raise ValueError(f"pilot symbol {symbol} has fewer than two samples")
        indexes.extend(range(start, stop))
    return tuple(indexes)


def _spread_symbols(count: int) -> tuple[int, ...]:
    span = LAST_PILOT_SYMBOL - FIRST_PILOT_SYMBOL
    return tuple(
        sorted(
            {
                round(FIRST_PILOT_SYMBOL + index * span / (count - 1))
                for index in range(count)
            }
        )
    )


def _method(
    bundle: StarlinkDetectorSuiteBundleV0_2,
    method: StarlinkDetectorMethod,
) -> StarlinkDetectorMethodEvidenceV0_2:
    return next(item for item in bundle.methods if item.method is method)


def _median(values: Iterable[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("median requires values")
    middle = len(ordered) // 2
    return (
        ordered[middle]
        if len(ordered) % 2
        else (ordered[middle - 1] + ordered[middle]) / 2
    )


def _unique_nonnegative_ints(values: tuple[int, ...], label: str) -> None:
    if (
        not values
        or len(set(values)) != len(values)
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in values
        )
    ):
        raise ValueError(f"{label} must be unique non-negative integers")


def _unique_finite(values: tuple[float, ...], label: str) -> None:
    if not values or len(set(values)) != len(values):
        raise ValueError(f"{label} must be non-empty and unique")
    for value in values:
        require_finite(value, label)


def _pilot_symbols(values: tuple[int, ...], label: str) -> None:
    if (
        not values
        or tuple(sorted(set(values))) != values
        or values[0] < FIRST_PILOT_SYMBOL
        or values[-1] > LAST_PILOT_SYMBOL
    ):
        raise ValueError(f"{label} must be a sorted subset of 2..301")


def _finite_complex(value: complex) -> bool:
    return math.isfinite(value.real) and math.isfinite(value.imag)


def _complex_samples_digest(values: Sequence[complex]) -> Digest:
    return canonical_digest(
        tuple((float(value.real), float(value.imag)) for value in values)
    )
