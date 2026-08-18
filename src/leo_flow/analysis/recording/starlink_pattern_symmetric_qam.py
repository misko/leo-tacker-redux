"""Bounded, data-independent QAM acquisition for Qin and every surrogate."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol

import numpy as np

from leo_flow.analysis.qam_goodness import qam_goodness_v0_2
from leo_flow.contracts.core import Digest, SchemaRef, canonical_digest
from leo_flow.contracts.starlink_adaptive_calibration import AdaptivePatternRole
from leo_flow.contracts.starlink_adaptive_response import (
    StarlinkAdaptiveResponseBundleV0_1,
    StarlinkAdaptiveResponseStreamV0_1,
)
from leo_flow.contracts.starlink_pattern_symmetric_qam import (
    V0_5,
    PatternQamEvidenceV0_5,
    PatternQamWindowEvidenceV0_5,
    PatternSymmetricAdaptiveQamBundleV0_5,
    PatternSymmetricQamPolicyV0_5,
    PatternSymmetricQamStreamV0_5,
)

from .starlink import FRAME_RATE_HZ, KnownCodePilotTemplatePairV0_1
from .starlink_acquisition import StarlinkAcquisitionV0_3
from .starlink_surrogate_null import (
    StarlinkConditionedPatternControlV0_1,
    StarlinkPatternTemplateV0_1,
    conditioned_pattern_control_v0_1,
    precommitted_surrogate_codebook_v0_1,
    qin_exact_search_pattern_v0_1,
)
from .starlink_templates import (
    CYCLIC_PREFIX_DURATION_S,
    OFDM_SYMBOL_DURATION_S,
    SUBCARRIER_SPACING_HZ,
    qin_edge_pilot_indices_v1,
    qin_edge_pilot_states_v1,
)


class PatternQamWindowReaderV0_5(Protocol):
    def read_window(
        self,
        stream: StarlinkAdaptiveResponseStreamV0_1,
        start_sample: int,
        stop_sample: int,
    ) -> Sequence[complex]: ...


class PatternSymmetricAdaptiveQamAnalyzerV0_5:
    def __init__(
        self,
        acquisition: StarlinkAcquisitionV0_3,
        policy: PatternSymmetricQamPolicyV0_5,
    ) -> None:
        self._acquisition = acquisition
        self._policy = policy

    def analyze(
        self,
        response: StarlinkAdaptiveResponseBundleV0_1,
        reader: PatternQamWindowReaderV0_5,
    ) -> PatternSymmetricAdaptiveQamBundleV0_5:
        streams = _bounded_streams(response, self._policy)
        pattern_count = _response_pattern_count(response)
        if pattern_count > self._policy.maximum_patterns:
            raise ValueError("response pattern count exceeds bounded QAM policy")
        outputs = []
        common_windows = _common_data_independent_windows(streams, self._policy)
        run_count = len(streams) * pattern_count * len(common_windows)
        if run_count > self._policy.maximum_acquisition_runs:
            raise ValueError("pattern-symmetric QAM run count exceeds policy")
        template_digests: tuple[Digest, ...] | None = None
        for stream in streams:
            patterns = (
                qin_exact_search_pattern_v0_1(stream.sample_rate_hz, stream.edge),
                *precommitted_surrogate_codebook_v0_1(
                    stream.sample_rate_hz, stream.edge, count=pattern_count - 1
                ),
            )
            digests = tuple(item.identity.template_ref.digest for item in patterns)
            if template_digests is None:
                template_digests = digests
            elif template_digests != digests:
                raise ValueError("pattern bank differs across receiver streams")
            _verify_response_templates(stream, digests)
            pattern_outputs = []
            for pattern_index, pattern in enumerate(patterns):
                control = conditioned_pattern_control_v0_1(pattern)
                pair = _acquisition_pair(pattern, control)
                windows = []
                states = _pattern_states(pattern)
                for source_index, start, stop in common_windows:
                    samples = tuple(
                        complex(value)
                        for value in reader.read_window(stream, start, stop)
                    )
                    if len(samples) != stop - start:
                        raise ValueError("pattern QAM reader returned another interval")
                    acquired = self._acquisition.analyze_receiver(
                        samples,
                        recording_id=response.recording_id,
                        recording_identity_digest=response.recording_identity_digest,
                        segment_id=stream.segment_id,
                        receiver_chain_id=stream.receiver_chain_id,
                        templates=pair,
                    )
                    accuracy, evm, support = known_pattern_qam_quality_v0_5(
                        samples,
                        stream.sample_rate_hz,
                        stream.edge,
                        states,
                        acquired.winner.refined_epoch_sample,
                        acquired.winner.refined_cfo_hz,
                    )
                    windows.append(
                        PatternQamWindowEvidenceV0_5(
                            source_index,
                            start,
                            stop,
                            acquired.search_identity_digest,
                            acquired.algorithm_ref.digest,
                            acquired.config_ref.digest,
                            acquired.coarse_search_cell_count,
                            acquired.refinement_search_cell_count,
                            acquired.winner.refined_epoch_sample,
                            acquired.winner.refined_cfo_hz,
                            support,
                            accuracy,
                            evm,
                            qam_goodness_v0_2(accuracy, evm),
                        )
                    )
                pattern_outputs.append(
                    PatternQamEvidenceV0_5(
                        pattern_index,
                        AdaptivePatternRole.QIN
                        if pattern_index == 0
                        else AdaptivePatternRole.SURROGATE,
                        pattern.identity.template_ref.digest,
                        control.template_ref.digest,
                        tuple(windows),
                    )
                )
            outputs.append(
                PatternSymmetricQamStreamV0_5(
                    stream.radio_id,
                    stream.segment_id,
                    stream.receiver_chain_id,
                    stream.edge,
                    stream.sample_rate_hz,
                    tuple(pattern_outputs),
                )
            )
        assert template_digests is not None
        outputs.sort(key=lambda item: item.identity)
        identity = canonical_digest(
            {
                "response": response.digest,
                "policy": self._policy.digest,
                "streams": outputs,
            }
        ).value
        return PatternSymmetricAdaptiveQamBundleV0_5(
            SchemaRef(PatternSymmetricAdaptiveQamBundleV0_5.SCHEMA_ID, V0_5),
            f"slpsqam5_{identity[:32]}",
            response.recording_id,
            response.recording_identity_digest,
            response.digest,
            self._policy,
            template_digests,
            tuple(outputs),
            run_count,
            True,
            None,
            (
                "identical-data-independent-windows-for-every-pattern",
                "identical-epoch-cfo-acquisition-for-every-pattern",
                "known-pattern-qam-not-detection",
                "retro-and-j1-are-conditioned-numerical-canaries-only",
            ),
        )


def known_pattern_qam_quality_v0_5(
    samples: Sequence[complex],
    sample_rate_hz: float,
    edge,
    expected_states: Sequence[Sequence[int]],
    epoch_sample: int,
    cfo_hz: float,
) -> tuple[float, float, int]:
    """Cross-fit a known 300x8 QPSK pattern at a preselected acquisition winner."""

    states = np.asarray(expected_states, dtype=np.int8)
    if states.shape != (300, 8):
        raise ValueError("known-pattern states must be 300 by 8")
    starts = _complete_frame_starts(len(samples), sample_rate_hz, epoch_sample)
    if not starts:
        raise ValueError("pattern QAM window has no complete frame")
    demodulator = _KnownPatternDemodulator(samples, sample_rate_hz, edge, cfo_hz)
    ideal = np.exp(0.5j * np.pi * (states.astype(float) + 0.5))
    frames = tuple(demodulator.frame(start) for start in starts)
    aligned = tuple(
        frame * np.exp(-1j * np.angle(np.sum(frame * np.conj(ideal))))
        for frame in frames
    )
    stacked = np.mean(aligned, axis=0)
    equalized = np.empty_like(stacked)
    indexes = np.arange(300)
    for parity in range(2):
        training = indexes % 2 != parity
        testing = ~training
        channel = np.mean(stacked[training] * np.conj(ideal[training]), axis=0)
        equalized[testing] = stacked[testing] / np.where(
            np.abs(channel) > 1e-20, channel, 1 + 0j
        )
    constellation = np.exp(0.5j * np.pi * (np.arange(4, dtype=float) + 0.5))
    hard = np.argmin(np.abs(equalized[..., None] - constellation) ** 2, axis=-1)
    errors = equalized - ideal
    return (
        float(np.mean(hard == states)),
        float(np.sqrt(np.mean(np.abs(errors) ** 2))),
        len(starts),
    )


def _bounded_streams(
    response: StarlinkAdaptiveResponseBundleV0_1, policy: PatternSymmetricQamPolicyV0_5
) -> tuple[StarlinkAdaptiveResponseStreamV0_1, ...]:
    if len(response.streams) > policy.maximum_receivers:
        raise ValueError("response receiver count exceeds bounded QAM policy")
    group = {
        (
            item.radio_id,
            item.segment_id,
            item.channel_number,
            item.edge,
            item.sample_rate_hz,
            item.segment_sample_count,
        )
        for item in response.streams
    }
    if len(group) != 1:
        raise ValueError("pattern-symmetric QAM requires one physical stream group")
    return tuple(sorted(response.streams, key=lambda item: str(item.receiver_chain_id)))


def _response_pattern_count(response: StarlinkAdaptiveResponseBundleV0_1) -> int:
    counts = {
        1 + len(point.surrogates)
        for stream in response.streams
        for point in stream.points
    }
    if len(counts) != 1:
        raise ValueError("adaptive response pattern count changed")
    return counts.pop()


def _common_data_independent_windows(
    streams: tuple[StarlinkAdaptiveResponseStreamV0_1, ...],
    policy: PatternSymmetricQamPolicyV0_5,
) -> tuple[tuple[int, int, int], ...]:
    memberships = tuple(
        {
            (item.start_sample, item.stop_sample): item.window_index
            for item in stream.selection.exact_windows
        }
        for stream in streams
    )
    common = set(memberships[0])
    for membership in memberships[1:]:
        common &= set(membership)
    ordered = sorted(common)
    if not ordered:
        raise ValueError("receiver streams share no adaptive windows")
    count = min(len(ordered), policy.maximum_windows_per_stream)
    chosen = sorted(
        {
            round(index * (len(ordered) - 1) / max(count - 1, 1))
            for index in range(count)
        }
    )
    output = []
    for index in chosen:
        source_start, source_stop = ordered[index]
        source_indexes = {
            membership[(source_start, source_stop)] for membership in memberships
        }
        if len(source_indexes) != 1:
            raise ValueError("receiver window indexes differ for shared geometry")
        center = (source_start + source_stop) // 2
        start = min(
            max(0, center - policy.qam_window_sample_count // 2),
            streams[0].segment_sample_count - policy.qam_window_sample_count,
        )
        source_index = source_indexes.pop()
        output.append((source_index, start, start + policy.qam_window_sample_count))
    return tuple(output)


def _verify_response_templates(
    stream: StarlinkAdaptiveResponseStreamV0_1, digests: tuple[Digest, ...]
) -> None:
    for point in stream.points:
        actual = tuple(item.template_digest for item in point.surrogates)
        if actual != digests[1:]:
            raise ValueError(
                "response surrogate bank differs from precommitted codebook"
            )
    declared = {item.digest for item in stream.selection.pattern_refs}
    if declared != set(digests):
        raise ValueError(
            "response pattern membership differs from precommitted codebook"
        )


def _acquisition_pair(
    pattern: StarlinkPatternTemplateV0_1,
    control: StarlinkConditionedPatternControlV0_1,
) -> KnownCodePilotTemplatePairV0_1:
    return KnownCodePilotTemplatePairV0_1(
        pattern.identity.edge,
        pattern.identity.pilot_subcarrier_indices,
        pattern.identity.sample_rate_hz,
        pattern.identity.template_ref,
        control.template_ref,
        pattern.samples,
        control.samples,
    )


def _pattern_states(
    pattern: StarlinkPatternTemplateV0_1,
) -> tuple[tuple[int, ...], ...]:
    if pattern.identity.role.value == "qin-exact":
        return qin_edge_pilot_states_v1(pattern.identity.edge)
    assert pattern.identity.codebook_index is not None
    from .starlink_surrogate_null import precommitted_surrogate_states_v0_1

    return precommitted_surrogate_states_v0_1(pattern.identity.codebook_index)


class _KnownPatternDemodulator:
    def __init__(
        self, samples: Sequence[complex], rate: float, edge, cfo: float
    ) -> None:
        self.samples, self.rate, self.cfo = samples, rate, cfo
        indexes = qin_edge_pilot_indices_v1(edge)
        absolute = np.asarray(
            [
                (item if item < 512 else item - 1024) * SUBCARRIER_SPACING_HZ
                for item in indexes
            ]
        )
        self.frequencies = absolute - np.mean(absolute)
        self.designs: dict[tuple[int, int], np.ndarray] = {}

    def frame(self, frame_start: int) -> np.ndarray:
        result = np.empty((300, 8), np.complex128)
        for row, symbol in enumerate(range(2, 302)):
            local_start = round(symbol * self.rate * OFDM_SYMBOL_DURATION_S)
            local_stop = round((symbol + 1) * self.rate * OFDM_SYMBOL_DURATION_S)
            key = (local_start, local_stop)
            solve = self.designs.get(key)
            if solve is None:
                local = np.arange(local_start, local_stop)
                time_s = (
                    local / self.rate
                    - symbol * OFDM_SYMBOL_DURATION_S
                    - CYCLIC_PREFIX_DURATION_S
                )
                solve = np.linalg.pinv(
                    np.exp(2j * np.pi * time_s[:, None] * self.frequencies[None, :])
                    / math.sqrt(8)
                )
                self.designs[key] = solve
            start, stop = frame_start + local_start, frame_start + local_stop
            values = np.asarray(self.samples[start:stop], np.complex128)
            values *= np.exp(
                -2j * np.pi * self.cfo * np.arange(start, stop) / self.rate
            )
            result[row] = solve @ values
        return result


def _complete_frame_starts(
    sample_count: int, rate: float, epoch: int
) -> tuple[int, ...]:
    content = round(302 * rate * OFDM_SYMBOL_DURATION_S)
    result: list[int] = []
    frame = 0
    while True:
        start = epoch + round(frame * rate / FRAME_RATE_HZ)
        if start + content > sample_count:
            return tuple(result)
        result.append(start)
        frame += 1
