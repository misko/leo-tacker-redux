"""Common detector port and deterministic paired surrogates for Starlink pilots."""

from __future__ import annotations

import cmath
import math
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    Provenance,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    canonical_digest,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_detector_suite import (
    REPORT_METHOD_ORDER,
    StarlinkDetectorMethod,
)
from leo_flow.contracts.starlink_full_search_control import (
    StarlinkFullSearchControlMode,
)
from leo_flow.contracts.starlink_surrogate_null import (
    MAXIMUM_SURROGATES,
    MINIMUM_DEFAULT_SURROGATES,
    V0_1,
    StarlinkPairedMethodNullV0_1,
    StarlinkPairedSurrogateEvidenceV0_1,
    StarlinkPatternDetectionV0_1,
    StarlinkPatternMethodEvidenceV0_1,
    StarlinkPatternSearchMode,
    StarlinkSearchGridV0_1,
    StarlinkSearchPatternRole,
    StarlinkSearchPatternV0_1,
)

from .api import AnalysisExecutionContext
from .starlink import (
    FRAME_RATE_HZ,
    KnownCodePilotTemplatePairV0_1,
    template_samples_digest,
)
from .starlink_detector_suite import (
    StarlinkDetectorSuiteConfigV0_2,
    StarlinkDetectorSuiteV0_2,
    starlink_acquire_conditioned_control_algorithm_ref_v0_1,
    starlink_detector_suite_algorithm_ref_v0_2,
    starlink_detector_suite_config_ref_v0_2,
)
from .starlink_templates import (
    CYCLIC_PREFIX_DURATION_S,
    OFDM_SYMBOL_DURATION_S,
    SUBCARRIER_SPACING_HZ,
    qin_edge_pilot_indices_v1,
    qin_edge_pilot_states_v1,
    qin_edge_pilot_template_pair_v0_1,
)

SURROGATE_GENERATOR_ID = "splitmix64-qpsk-edge-codebook-v0.1"
SURROGATE_MASTER_SEED = 0xD1B54A32D192ED03
PATTERN_TEMPLATE_SCHEMA = SchemaRef("org.leo-flow.starlink-edge-pilot-template", V0_1)
MASK_64 = (1 << 64) - 1


@dataclass(frozen=True)
class StarlinkRadioSignalV0_1:
    """One immutable receiver stream passed through the detector port."""

    samples: tuple[complex, ...]
    recording_id: RecordingId
    recording_identity_digest: Digest
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    edge: StarlinkEdge
    sample_rate_hz: float
    input_digest: Digest

    def __post_init__(self) -> None:
        if not self.samples:
            raise ValueError("radio signal cannot be empty")
        if not math.isfinite(self.sample_rate_hz) or self.sample_rate_hz <= 0:
            raise ValueError("radio signal sample rate must be finite and positive")
        if any(
            not math.isfinite(value.real) or not math.isfinite(value.imag)
            for value in self.samples
        ):
            raise ValueError("radio signal samples must be finite")
        if self.input_digest != radio_signal_digest_v0_1(self.samples):
            raise ValueError("input_digest does not identify the radio samples")


@dataclass(frozen=True)
class StarlinkPatternTemplateV0_1:
    """Persistable pattern identity paired with its runtime-only sample template."""

    identity: StarlinkSearchPatternV0_1
    samples: tuple[complex, ...]

    def __post_init__(self) -> None:
        if len(self.samples) != self.identity.frame_sample_count:
            raise ValueError("pattern sample dimensions do not match its identity")
        if template_samples_digest(self.samples) != self.identity.template_ref.digest:
            raise ValueError("pattern template digest does not identify its samples")
        energy = math.fsum(abs(value) ** 2 for value in self.samples)
        if not math.isclose(energy, self.identity.template_energy, rel_tol=1e-12):
            raise ValueError("pattern template energy does not match its identity")


@dataclass(frozen=True)
class StarlinkConditionedPatternControlV0_1:
    """The frozen 17-symbol roll control for any known search pattern."""

    template_ref: ArtifactRef
    samples: tuple[complex, ...]

    def __post_init__(self) -> None:
        if template_samples_digest(self.samples) != self.template_ref.digest:
            raise ValueError("conditioned pattern control digest differs")


@dataclass(frozen=True)
class StarlinkDetectionParametersV0_1:
    """One pattern plus the complete bounded v0.2 report-method search plan."""

    pattern: StarlinkPatternTemplateV0_1
    suite_config: StarlinkDetectorSuiteConfigV0_2


class StarlinkDetectorV0_1(Protocol):
    """Common target/surrogate detector port; composition is intentional."""

    def detect(
        self,
        radio_signal: StarlinkRadioSignalV0_1,
        parameters: StarlinkDetectionParametersV0_1,
    ) -> StarlinkPatternDetectionV0_1: ...


class ReportMethodStarlinkDetectorV0_1:
    """Run report methods with independent or acquire-conditioned selection."""

    def __init__(
        self,
        execution: AnalysisExecutionContext,
        *,
        condition_relative_on_acquire: bool = False,
    ) -> None:
        self._execution = execution
        self._condition_relative_on_acquire = condition_relative_on_acquire

    def detect(
        self,
        radio_signal: StarlinkRadioSignalV0_1,
        parameters: StarlinkDetectionParametersV0_1,
    ) -> StarlinkPatternDetectionV0_1:
        pattern = parameters.pattern
        if (
            pattern.identity.edge is not radio_signal.edge
            or pattern.identity.sample_rate_hz != radio_signal.sample_rate_hz
        ):
            raise ValueError("pattern and radio-signal dimensions differ")
        qin = qin_edge_pilot_template_pair_v0_1(
            radio_signal.sample_rate_hz,
            radio_signal.edge,
        )
        if pattern.identity.template_ref == qin.exact_ref:
            placeholder_ref = qin.conditioned_control_ref
            placeholder_samples = qin.conditioned_control_samples
        else:
            placeholder_ref = qin.exact_ref
            placeholder_samples = qin.exact_samples
        compatibility_pair = KnownCodePilotTemplatePairV0_1(
            radio_signal.edge,
            qin.pilot_indices,
            radio_signal.sample_rate_hz,
            placeholder_ref,
            pattern.identity.template_ref,
            placeholder_samples,
            pattern.samples,
        )
        suite = StarlinkDetectorSuiteV0_2(
            parameters.suite_config,
            self._execution,
        ).analyze_full_search_control(
            radio_signal.samples,
            recording_id=radio_signal.recording_id,
            recording_identity_digest=radio_signal.recording_identity_digest,
            segment_id=radio_signal.segment_id,
            receiver_chain_id=radio_signal.receiver_chain_id,
            templates=compatibility_pair,
            condition_relative_on_acquire=self._condition_relative_on_acquire,
        )
        target_algorithm = (
            starlink_acquire_conditioned_control_algorithm_ref_v0_1()
            if self._condition_relative_on_acquire
            else starlink_detector_suite_algorithm_ref_v0_2()
        )
        search_grid = starlink_search_grid_v0_1(parameters.suite_config)
        methods = tuple(
            _map_method(
                item,
                pattern.identity,
                target_algorithm,
                radio_signal.input_digest,
                search_grid,
            )
            for item in suite.methods
        )
        identity = canonical_digest(
            {
                "input_digest": str(radio_signal.input_digest),
                "pattern_ref": pattern.identity.template_ref,
                "methods": methods,
            }
        )
        return StarlinkPatternDetectionV0_1(
            SchemaRef(StarlinkPatternDetectionV0_1.SCHEMA_ID, V0_1),
            f"slpdet_{identity.value[:32]}",
            radio_signal.recording_id,
            radio_signal.recording_identity_digest,
            radio_signal.segment_id,
            radio_signal.receiver_chain_id,
            radio_signal.edge,
            radio_signal.sample_rate_hz,
            len(radio_signal.samples),
            radio_signal.input_digest,
            search_grid,
            pattern.identity,
            methods,
            _pattern_provenance(suite.provenance, target_algorithm, pattern.identity),
            True,
        )


class StarlinkPairedSurrogateAnalyzerV0_1:
    """Invoke one detector identically for exact Qin and every surrogate."""

    def __init__(
        self,
        detector: StarlinkDetectorV0_1,
        suite_config: StarlinkDetectorSuiteConfigV0_2,
    ) -> None:
        self._detector = detector
        self._suite_config = suite_config

    def analyze(
        self,
        radio_signal: StarlinkRadioSignalV0_1,
        *,
        surrogate_count: int = MINIMUM_DEFAULT_SURROGATES,
    ) -> StarlinkPairedSurrogateEvidenceV0_1:
        _surrogate_count(surrogate_count)
        exact_pattern = qin_exact_search_pattern_v0_1(
            radio_signal.sample_rate_hz,
            radio_signal.edge,
        )
        surrogate_patterns = precommitted_surrogate_codebook_v0_1(
            radio_signal.sample_rate_hz,
            radio_signal.edge,
            count=surrogate_count,
        )
        exact = self._detector.detect(
            radio_signal,
            StarlinkDetectionParametersV0_1(exact_pattern, self._suite_config),
        )
        surrogates = tuple(
            self._detector.detect(
                radio_signal,
                StarlinkDetectionParametersV0_1(pattern, self._suite_config),
            )
            for pattern in surrogate_patterns
        )
        exact_methods = {item.method: item for item in exact.methods}
        method_nulls = tuple(
            _method_null(method, exact_methods[method], surrogates)
            for method in REPORT_METHOD_ORDER
        )
        codebook_digest = canonical_digest(
            tuple(pattern.identity for pattern in surrogate_patterns)
        )
        identity = canonical_digest(
            {
                "exact": exact.digest,
                "surrogates": tuple(item.digest for item in surrogates),
                "method_nulls": method_nulls,
                "codebook_digest": codebook_digest,
            }
        )
        return StarlinkPairedSurrogateEvidenceV0_1(
            SchemaRef(StarlinkPairedSurrogateEvidenceV0_1.SCHEMA_ID, V0_1),
            f"slsnull_{identity.value[:32]}",
            exact,
            surrogates,
            method_nulls,
            codebook_digest,
            MINIMUM_DEFAULT_SURROGATES,
            True,
            (
                "finite-paired-surrogate-controls",
                "not-verified-signal-absent",
                "not-calibrated-detection",
            ),
        )


def radio_signal_v0_1(
    samples: Sequence[complex],
    *,
    recording_id: RecordingId,
    recording_identity_digest: Digest,
    segment_id: SegmentId,
    receiver_chain_id: ReceiverChainId,
    edge: StarlinkEdge,
    sample_rate_hz: float,
) -> StarlinkRadioSignalV0_1:
    values = tuple(complex(value) for value in samples)
    return StarlinkRadioSignalV0_1(
        values,
        recording_id,
        recording_identity_digest,
        segment_id,
        receiver_chain_id,
        edge,
        sample_rate_hz,
        radio_signal_digest_v0_1(values),
    )


def radio_signal_digest_v0_1(samples: Sequence[complex]) -> Digest:
    return canonical_digest(
        tuple((float(value.real), float(value.imag)) for value in samples)
    )


def qin_exact_search_pattern_v0_1(
    sample_rate_hz: float,
    edge: StarlinkEdge,
) -> StarlinkPatternTemplateV0_1:
    pair = qin_edge_pilot_template_pair_v0_1(sample_rate_hz, edge)
    states = qin_edge_pilot_states_v1(edge)
    identity = StarlinkSearchPatternV0_1(
        SchemaRef(StarlinkSearchPatternV0_1.SCHEMA_ID, V0_1),
        f"qin-exact-{edge.value}-{pair.exact_ref.digest.value[:16]}",
        StarlinkSearchPatternRole.QIN_EXACT,
        pair.exact_ref,
        edge,
        pair.pilot_indices,
        2,
        301,
        FRAME_RATE_HZ,
        sample_rate_hz,
        len(pair.exact_samples),
        math.fsum(abs(value) ** 2 for value in pair.exact_samples),
        canonical_digest(states),
        "qin-appendix-a-2602.02627v1",
        None,
        None,
        True,
    )
    return StarlinkPatternTemplateV0_1(identity, pair.exact_samples)


def precommitted_surrogate_codebook_v0_1(
    sample_rate_hz: float,
    edge: StarlinkEdge,
    *,
    count: int = MINIMUM_DEFAULT_SURROGATES,
) -> tuple[StarlinkPatternTemplateV0_1, ...]:
    """Materialize data-independent patterns from the fixed v0.1 codebook."""

    _surrogate_count(count)
    return tuple(
        _surrogate_pattern(sample_rate_hz, edge, index) for index in range(count)
    )


def precommitted_surrogate_states_v0_1(
    index: int,
) -> tuple[tuple[int, ...], ...]:
    """Expose the fixed, data-independent 300-by-8 codeword for audit/tests."""

    if isinstance(index, bool) or not isinstance(index, int):
        raise TypeError("surrogate index must be an integer")
    if not 0 <= index < MAXIMUM_SURROGATES:
        raise ValueError("surrogate index must lie in [0,31]")
    return _qpsk_states(_splitmix64(SURROGATE_MASTER_SEED + index))


def conditioned_pattern_control_v0_1(
    pattern: StarlinkPatternTemplateV0_1,
) -> StarlinkConditionedPatternControlV0_1:
    """Apply the same frozen 17-symbol roll to Qin or any surrogate."""

    if pattern.identity.role is StarlinkSearchPatternRole.QIN_EXACT:
        pair = qin_edge_pilot_template_pair_v0_1(
            pattern.identity.sample_rate_hz, pattern.identity.edge
        )
        return StarlinkConditionedPatternControlV0_1(
            pair.conditioned_control_ref, pair.conditioned_control_samples
        )
    assert pattern.identity.codebook_index is not None
    states = precommitted_surrogate_states_v0_1(pattern.identity.codebook_index)
    rolled = tuple(states[(index - 17) % 300] for index in range(300))
    samples = _match_template_energy(
        _synthesize_edge_frame(
            pattern.identity.sample_rate_hz, pattern.identity.edge, rolled
        ),
        pattern.samples,
    )
    digest = template_samples_digest(samples)
    return StarlinkConditionedPatternControlV0_1(
        ArtifactRef(
            f"{pattern.identity.pattern_id}-roll17-control-{digest.value[:16]}",
            digest,
            PATTERN_TEMPLATE_SCHEMA,
        ),
        samples,
    )


def _surrogate_pattern(
    sample_rate_hz: float,
    edge: StarlinkEdge,
    index: int,
) -> StarlinkPatternTemplateV0_1:
    seed = _splitmix64(SURROGATE_MASTER_SEED + index)
    states = precommitted_surrogate_states_v0_1(index)
    samples = _synthesize_edge_frame(sample_rate_hz, edge, states)
    exact_samples = qin_edge_pilot_template_pair_v0_1(
        sample_rate_hz, edge
    ).exact_samples
    samples = _match_template_energy(samples, exact_samples)
    payload_digest = template_samples_digest(samples)
    if payload_digest == template_samples_digest(exact_samples):
        raise ValueError("surrogate codeword unexpectedly equals the Qin template")
    template_ref = ArtifactRef(
        f"starlink-surrogate-{index:02d}-{edge.value}-{payload_digest.value[:16]}",
        payload_digest,
        PATTERN_TEMPLATE_SCHEMA,
    )
    identity = StarlinkSearchPatternV0_1(
        SchemaRef(StarlinkSearchPatternV0_1.SCHEMA_ID, V0_1),
        f"surrogate-{index:02d}-{edge.value}-{payload_digest.value[:16]}",
        StarlinkSearchPatternRole.PRECOMMITTED_SURROGATE,
        template_ref,
        edge,
        qin_edge_pilot_indices_v1(edge),
        2,
        301,
        FRAME_RATE_HZ,
        sample_rate_hz,
        len(samples),
        math.fsum(abs(value) ** 2 for value in samples),
        canonical_digest(states),
        SURROGATE_GENERATOR_ID,
        seed,
        index,
        True,
    )
    return StarlinkPatternTemplateV0_1(identity, samples)


def _qpsk_states(seed: int) -> tuple[tuple[int, ...], ...]:
    state = seed
    rows: list[tuple[int, ...]] = []
    for _ in range(300):
        row = []
        for _ in range(8):
            state = _splitmix64(state)
            row.append(state & 0x3)
        rows.append(tuple(row))
    return tuple(rows)


def _splitmix64(value: int) -> int:
    value = (value + 0x9E3779B97F4A7C15) & MASK_64
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & MASK_64
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & MASK_64
    return (value ^ (value >> 31)) & MASK_64


def _synthesize_edge_frame(
    sample_rate_hz: float,
    edge: StarlinkEdge,
    states: tuple[tuple[int, ...], ...],
) -> tuple[complex, ...]:
    if not math.isfinite(sample_rate_hz) or sample_rate_hz <= 0:
        raise ValueError("sample_rate_hz must be finite and positive")
    if len(states) != 300 or any(
        len(row) != 8 or any(state not in (0, 1, 2, 3) for state in row)
        for row in states
    ):
        raise ValueError("surrogate states must be a 300-by-8 QPSK matrix")
    count = round(sample_rate_hz / FRAME_RATE_HZ)
    if count <= 0 or count > 1_000_000:
        raise ValueError("sample rate produces an unsupported template length")
    indices = qin_edge_pilot_indices_v1(edge)
    tuning_offset_hz = math.fsum(_subcarrier_offset_hz(i) for i in indices) / 8
    symbols = tuple(
        tuple(_complex64(cmath.exp(0.5j * math.pi * (state + 0.5))) for state in row)
        for row in states
    )
    output = []
    for sample_index in range(count):
        time_s = sample_index / sample_rate_hz
        symbol_index = math.floor(time_s / OFDM_SYMBOL_DURATION_S)
        if symbol_index < 2 or symbol_index > 301:
            output.append(0j)
            continue
        local_time_s = time_s - symbol_index * OFDM_SYMBOL_DURATION_S
        value = 0j
        for column, subcarrier in enumerate(indices):
            frequency_hz = _subcarrier_offset_hz(subcarrier) - tuning_offset_hz
            value += symbols[symbol_index - 2][column] * cmath.exp(
                2j * math.pi * frequency_hz * (local_time_s - CYCLIC_PREFIX_DURATION_S)
            )
        output.append(_complex64(value / math.sqrt(8)))
    return tuple(output)


def _subcarrier_offset_hz(index: int) -> float:
    signed = index if index < 512 else index - 1024
    return signed * SUBCARRIER_SPACING_HZ


def _complex64(value: complex) -> complex:
    real = 0.0 if abs(value.real) < 1e-10 else value.real
    imag = 0.0 if abs(value.imag) < 1e-10 else value.imag
    return complex(
        struct.unpack("!f", struct.pack("!f", real))[0],
        struct.unpack("!f", struct.pack("!f", imag))[0],
    )


def _match_template_energy(
    samples: tuple[complex, ...],
    reference: tuple[complex, ...],
) -> tuple[complex, ...]:
    source_energy = math.fsum(abs(value) ** 2 for value in samples)
    reference_energy = math.fsum(abs(value) ** 2 for value in reference)
    if source_energy <= 0 or reference_energy <= 0:
        raise ValueError("pattern energy must be positive")
    scale = math.sqrt(reference_energy / source_energy)
    return tuple(_complex64(value * scale) for value in samples)


def _map_method(
    method: object,
    pattern: StarlinkSearchPatternV0_1,
    algorithm_ref: ArtifactRef,
    input_digest: Digest,
    search_grid: StarlinkSearchGridV0_1,
) -> StarlinkPatternMethodEvidenceV0_1:
    # Kept local to the compatibility adapter so no private detector state leaks.
    from leo_flow.contracts.starlink_full_search_control import (
        StarlinkFullSearchControlMethodEvidenceV0_1,
    )

    if not isinstance(method, StarlinkFullSearchControlMethodEvidenceV0_1):
        raise TypeError("detector adapter received unknown method evidence")
    search_mode = (
        StarlinkPatternSearchMode.SEARCHED
        if method.search_mode is StarlinkFullSearchControlMode.SEARCHED_ROLLED_TEMPLATE
        else StarlinkPatternSearchMode.CONDITIONED_ON_PATTERN_ACQUIRE_WINNER
    )
    search_plan = canonical_digest(
        {
            "algorithm_ref": algorithm_ref,
            "search_grid": search_grid,
            "method": method.method.value,
            "search_mode": search_mode.value,
            "selection_method": method.selection_method.value,
            "effective_search_cell_count": method.effective_search_cell_count,
            "pilot_symbol_indices": method.pilot_symbol_indices,
            "symbol_set_role": method.symbol_set_role,
            "symbol_split_digest": method.symbol_split_digest,
            "maximization": "independent-per-pattern",
        }
    )
    identity = canonical_digest(
        {
            "search_plan_digest": search_plan,
            "input_digest": input_digest,
            "pattern_ref": pattern.template_ref,
        }
    )
    return StarlinkPatternMethodEvidenceV0_1(
        SchemaRef(StarlinkPatternMethodEvidenceV0_1.SCHEMA_ID, V0_1),
        method.method,
        algorithm_ref,
        method.config_ref,
        input_digest,
        pattern,
        search_plan,
        identity,
        search_mode,
        method.selection_method,
        method.effective_search_cell_count,
        method.winning_epoch_sample,
        method.winning_coarse_cfo_hz,
        method.winning_residual_cfo_hz,
        method.full_search_control_score,
        method.control_frames,
        method.pilot_symbol_indices,
        method.symbol_set_role,
        method.symbol_split_digest,
    )


def starlink_search_grid_v0_1(
    config: StarlinkDetectorSuiteConfigV0_2,
) -> StarlinkSearchGridV0_1:
    return StarlinkSearchGridV0_1(
        starlink_detector_suite_config_ref_v0_2(config),
        config.epoch_hypotheses_samples,
        config.coarse_cfo_hypotheses_hz,
        config.glrt_residual_cfo_hypotheses_hz,
        config.acquire_symbols,
        config.verify_symbols,
        config.maximum_probe_samples,
        config.maximum_outer_search_cells,
        config.maximum_effective_search_cells,
        config.maximum_frame_summaries,
    )


def _method_null(
    method: StarlinkDetectorMethod,
    target: StarlinkPatternMethodEvidenceV0_1,
    surrogates: tuple[StarlinkPatternDetectionV0_1, ...],
) -> StarlinkPairedMethodNullV0_1:
    scores = tuple(
        next(item.score for item in result.methods if item.method is method)
        for result in surrogates
    )
    probability = (1 + sum(score >= target.score for score in scores)) / (
        len(scores) + 1
    )
    return StarlinkPairedMethodNullV0_1(method, target.score, scores, probability)


def _pattern_provenance(
    provenance: Provenance,
    algorithm_ref: ArtifactRef,
    pattern: StarlinkSearchPatternV0_1,
) -> Provenance:
    dependencies = tuple(
        dict.fromkeys(
            (
                *provenance.dependency_digests,
                algorithm_ref.digest,
                pattern.template_ref.digest,
                pattern.qpsk_state_matrix_digest,
            )
        )
    )
    return Provenance(
        provenance.producer_name,
        provenance.producer_version,
        provenance.git_commit,
        provenance.environment_digest,
        provenance.normalized_config_digest,
        provenance.input_digests,
        dependencies,
        provenance.started_utc_ns,
        provenance.completed_utc_ns,
        provenance.host_class,
    )


def _surrogate_count(count: int) -> None:
    if isinstance(count, bool) or not isinstance(count, int):
        raise TypeError("surrogate count must be an integer")
    if not 1 <= count <= MAXIMUM_SURROGATES:
        raise ValueError("surrogate count must lie in [1,32]")
