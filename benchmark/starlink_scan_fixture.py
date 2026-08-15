"""Detector-independent paired-RX fixture for an exact Starlink scan plan.

This module is benchmark support.  It deliberately composes the pilot
generator with the public capture-plan contract and does not import analysis
or detector code.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
from dataclasses import dataclass
from typing import Any, Literal, cast

from benchmark.starlink_pilot_if import (
    EDGE_PILOT_SUBCARRIERS,
    GENERATOR_ID,
    SUBCARRIER_SPACING_HZ,
    PilotIfSpecification,
    PilotIfSpecificationError,
    generate_pilot_if,
    pilot_local_offsets_hz,
)
from leo_flow.contracts import canonical_digest
from leo_flow.contracts.capture import CapturePlan, SegmentRequest
from leo_flow.contracts.core import ReceiverChainId, SegmentId

SCHEMA = "leo-flow.paired-starlink-scan-fixture/v1"
CAMPAIGN_SCHEMA = "leo-flow.paired-starlink-scan-fixture/v2"
RECORDED_BACKGROUND_SCHEMA = "leo-flow.recorded-paired-background/v1"


class StarlinkScanFixtureError(ValueError):
    """The requested synthetic scan fixture is inconsistent or would clip."""


@dataclass(frozen=True)
class ReceiverPath:
    """Deterministic propagation and receiver noise for one RX chain.

    Delay is a causal, zero-filled integer delay.  Ambient noise is independently
    seeded per receiver and channel/edge, then frozen across null and positive
    cases.  SNR ladders therefore vary source level, never background noise.
    """

    integer_delay_samples: int = 0
    gain_linear: float = 1.0
    phase_offset_rad: float = 0.0
    ambient_noise_rms_counts: float = 8.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.integer_delay_samples, bool)
            or not isinstance(self.integer_delay_samples, int)
            or self.integer_delay_samples < 0
        ):
            raise StarlinkScanFixtureError(
                "receiver delay must be a nonnegative integer"
            )
        if not math.isfinite(self.gain_linear) or self.gain_linear <= 0:
            raise StarlinkScanFixtureError("receiver gain must be finite and positive")
        if not math.isfinite(self.phase_offset_rad):
            raise StarlinkScanFixtureError("receiver phase must be finite")
        if (
            not math.isfinite(self.ambient_noise_rms_counts)
            or self.ambient_noise_rms_counts < 0
        ):
            raise StarlinkScanFixtureError(
                "ambient receiver noise must be finite and nonnegative"
            )


@dataclass(frozen=True)
class FrozenPairedBackground:
    """Exact paired-RX bytes carved from one immutable recording.

    The source recording digest binds the parent object while the segment
    digest is verified against the bytes supplied to this generator.  A
    recorded background is never asserted to be free of an RF signal.
    """

    segment_id: SegmentId
    paired_ci16_le: bytes
    source_recording_id: str
    declared_source_recording_data_sha256: str
    source_start_sample: int
    source_segment_sha256: str
    source_signal_status: Literal["unknown"] = "unknown"

    def __post_init__(self) -> None:
        if not self.paired_ci16_le or len(self.paired_ci16_le) % 8:
            raise StarlinkScanFixtureError(
                "recorded background must be nonempty paired CI16"
            )
        if not self.source_recording_id.strip():
            raise StarlinkScanFixtureError("recorded background needs a recording ID")
        for label, value in (
            ("declared recording data", self.declared_source_recording_data_sha256),
            ("segment", self.source_segment_sha256),
        ):
            if len(value) != 64 or any(
                char not in "0123456789abcdef" for char in value
            ):
                raise StarlinkScanFixtureError(
                    f"recorded background {label} digest must be lowercase SHA-256"
                )
        if self.source_start_sample < 0:
            raise StarlinkScanFixtureError(
                "recorded background start sample must be nonnegative"
            )
        if (
            hashlib.sha256(self.paired_ci16_le).hexdigest()
            != self.source_segment_sha256
        ):
            raise StarlinkScanFixtureError(
                "recorded background bytes do not match the segment digest"
            )
        if self.source_signal_status != "unknown":
            raise StarlinkScanFixtureError(
                "recorded background signal status must remain unknown"
            )


@dataclass(frozen=True)
class StarlinkPilotScanCase:
    """One signal-present or signal-absent scan experiment."""

    signal_present: bool
    target_channels: tuple[int, ...]
    edge: Literal["lower", "upper"]
    pilot_indices: tuple[int, ...]
    seed_u64: int
    receiver_paths: tuple[ReceiverPath, ReceiverPath]
    source_signal_rms_counts: float = 128.0
    cfo_hz: float = 0.0
    frame_phase: Literal["random", "coherent"] = "random"
    converter_min: int = -2048
    converter_max: int = 2047
    clipping_policy: Literal["reject", "saturate_and_report"] = "reject"
    recorded_backgrounds: tuple[FrozenPairedBackground, ...] = ()
    frequency_drift_hz_s: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.signal_present, bool):
            raise StarlinkScanFixtureError("signal_present must be boolean")
        if not self.target_channels or len(set(self.target_channels)) != len(
            self.target_channels
        ):
            raise StarlinkScanFixtureError(
                "target channels must be nonempty and unique"
            )
        if any(channel not in range(1, 9) for channel in self.target_channels):
            raise StarlinkScanFixtureError("target channels must lie in [1, 8]")
        if self.edge not in ("lower", "upper"):
            raise StarlinkScanFixtureError("edge must be lower or upper")
        if (
            not self.pilot_indices
            or len(set(self.pilot_indices)) != len(self.pilot_indices)
            or tuple(sorted(self.pilot_indices)) != self.pilot_indices
            or not set(self.pilot_indices) <= set(EDGE_PILOT_SUBCARRIERS[self.edge])
        ):
            raise StarlinkScanFixtureError(
                "pilot indices must be a sorted, unique subset of the selected edge"
            )
        if not 0 < self.seed_u64 < 2**64:
            raise StarlinkScanFixtureError("seed_u64 must lie in [1, 2**64)")
        if len(self.receiver_paths) != 2:
            raise StarlinkScanFixtureError("exactly two receiver paths are required")
        if (
            not math.isfinite(self.source_signal_rms_counts)
            or self.source_signal_rms_counts <= 0
        ):
            raise StarlinkScanFixtureError("source signal RMS must be positive")
        if self.frame_phase not in ("random", "coherent"):
            raise StarlinkScanFixtureError("frame phase must be random or coherent")
        if not math.isfinite(self.cfo_hz):
            raise StarlinkScanFixtureError("CFO must be finite")
        if not math.isfinite(self.frequency_drift_hz_s):
            raise StarlinkScanFixtureError("frequency drift must be finite")
        if self.converter_min < -32768 or self.converter_max > 32767:
            raise StarlinkScanFixtureError("converter limits must fit signed int16")
        if self.converter_min >= self.converter_max:
            raise StarlinkScanFixtureError("converter limits must be ordered")
        if self.clipping_policy not in ("reject", "saturate_and_report"):
            raise StarlinkScanFixtureError("unsupported clipping policy")
        background_ids = tuple(item.segment_id for item in self.recorded_backgrounds)
        if len(set(background_ids)) != len(background_ids):
            raise StarlinkScanFixtureError(
                "recorded background segment IDs must be unique"
            )


@dataclass(frozen=True)
class PairedScanSegment:
    segment_id: SegmentId
    paired_ci16_le: bytes


@dataclass(frozen=True)
class PairedStarlinkScanFixture:
    """Exact paired CI16 segment payloads and their canonical ground truth."""

    segments: tuple[PairedScanSegment, ...]
    truth_json: bytes

    @property
    def truth(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.truth_json))

    @property
    def truth_sha256(self) -> str:
        return hashlib.sha256(self.truth_json).hexdigest()

    def payload_for(self, segment_id: SegmentId) -> bytes:
        for segment in self.segments:
            if segment.segment_id == segment_id:
                return segment.paired_ci16_le
        raise KeyError(segment_id)


def generate_paired_starlink_scan_fixture(
    plan: CapturePlan, case: StarlinkPilotScanCase
) -> PairedStarlinkScanFixture:
    """Generate every segment of ``plan`` with signal only at declared targets."""

    requests = tuple(
        segment for activity in plan.activities for segment in activity.segments
    )
    if len(plan.receiver_chain_ids) != 2:
        raise StarlinkScanFixtureError("paired fixture requires exactly two receivers")
    if not requests:
        raise StarlinkScanFixtureError("scan plan has no segments")

    sample_counts = {segment.sample_count for segment in requests}
    sample_rates = {segment.sample_rate_hz for segment in requests}
    if None in sample_counts or len(sample_counts) != 1:
        raise StarlinkScanFixtureError("all scan segments need one exact sample count")
    if len(sample_rates) != 1:
        raise StarlinkScanFixtureError("all scan segments need one sample rate")
    if any(
        segment.receiver_chain_ids != plan.receiver_chain_ids for segment in requests
    ):
        raise StarlinkScanFixtureError(
            "every scan segment must use the plan's paired receivers in order"
        )
    sample_count = next(iter(sample_counts))
    sample_rate_hz = next(iter(sample_rates))
    assert sample_count is not None
    if not float(sample_rate_hz).is_integer():
        raise StarlinkScanFixtureError("pilot fixture requires an integer sample rate")
    if any(path.integer_delay_samples >= sample_count for path in case.receiver_paths):
        raise StarlinkScanFixtureError("receiver delay must be shorter than a segment")

    background_by_segment = {
        item.segment_id: item for item in case.recorded_backgrounds
    }
    campaign_truth = bool(
        background_by_segment
        or case.frequency_drift_hz_s != 0
        or case.clipping_policy != "reject"
    )
    if background_by_segment and set(background_by_segment) != {
        segment.segment_id for segment in requests
    }:
        raise StarlinkScanFixtureError(
            "recorded backgrounds must cover every scan segment exactly once"
        )
    expected_background_bytes = sample_count * 8
    if any(
        len(item.paired_ci16_le) != expected_background_bytes
        for item in background_by_segment.values()
    ):
        raise StarlinkScanFixtureError(
            "recorded background sample count differs from the scan plan"
        )

    requested_targets = {(channel, case.edge) for channel in case.target_channels}
    target_requests = tuple(
        segment
        for segment in requests
        if _segment_coordinates(segment) in requested_targets
    )
    target_counts = {
        target: sum(_segment_coordinates(segment) == target for segment in requests)
        for target in requested_targets
    }
    if any(count != 1 for count in target_counts.values()):
        raise StarlinkScanFixtureError(
            "each requested target channel/edge must occur exactly once in the plan"
        )

    duration_s = (sample_count - 1) / float(sample_rate_hz)
    drifted_cfo_hz = case.cfo_hz + case.frequency_drift_hz_s * duration_s
    pilot_offsets = tuple(
        offset + case.cfo_hz
        for offset in pilot_local_offsets_hz(case.edge, case.pilot_indices)
    )
    ending_pilot_offsets = tuple(
        offset + drifted_cfo_hz
        for offset in pilot_local_offsets_hz(case.edge, case.pilot_indices)
    )
    occupied_edge_hz = (
        max(abs(offset) for offset in (*pilot_offsets, *ending_pilot_offsets))
        + SUBCARRIER_SPACING_HZ / 2
    )
    for segment in target_requests:
        limiting_half_bandwidth_hz = (
            min(segment.sample_rate_hz, segment.bandwidth_hz) / 2
        )
        if occupied_edge_hz > limiting_half_bandwidth_hz:
            raise StarlinkScanFixtureError(
                "pilot occupied support does not fit the target digital and analog bandwidth"
            )

    source = None
    source_samples: tuple[complex, ...] = ()
    if case.signal_present:
        try:
            source = generate_pilot_if(
                PilotIfSpecification(
                    sample_rate_hz=int(sample_rate_hz),
                    sample_count=sample_count,
                    edge=case.edge,
                    pilot_indices=case.pilot_indices,
                    signal_rms_counts=case.source_signal_rms_counts,
                    noise_snr_db=None,
                    seed_u64=case.seed_u64,
                    cfo_hz=case.cfo_hz,
                    frequency_drift_hz_s=case.frequency_drift_hz_s,
                    if_center_hz=target_requests[0].center_frequency_hz,
                    frame_phase=case.frame_phase,
                    converter_min=case.converter_min,
                    converter_max=case.converter_max,
                )
            )
        except PilotIfSpecificationError as error:
            raise StarlinkScanFixtureError(str(error)) from error
        source_samples = _unpack_single_receiver(source.ci16_le)

    expected_targets = {
        segment.segment_id for segment in target_requests if case.signal_present
    }
    output_segments: list[PairedScanSegment] = []
    segment_truth: list[dict[str, Any]] = []
    for segment in requests:
        contains_signal = segment.segment_id in expected_targets
        channel, edge = _segment_coordinates(segment)
        recorded_background = background_by_segment.get(segment.segment_id)
        recorded_receiver_words = (
            None
            if recorded_background is None
            else _unpack_paired_receivers(recorded_background.paired_ci16_le)
        )
        receiver_words: list[tuple[tuple[int, int], ...]] = []
        receivers_truth: list[dict[str, Any]] = []
        for receiver_index, path in enumerate(case.receiver_paths):
            signal = _receiver_signal(source_samples, path) if contains_signal else None
            noise_seed = _derived_seed(
                case.seed_u64,
                channel,
                edge,
                plan.receiver_chain_ids[receiver_index],
            )
            requested_signal_rms_counts = (
                case.source_signal_rms_counts * path.gain_linear
                if contains_signal
                else None
            )
            if recorded_receiver_words is None:
                combined, receiver_truth = _receiver_samples(
                    sample_count=sample_count,
                    signal=signal,
                    path=path,
                    noise_seed=noise_seed,
                    requested_signal_rms_counts=requested_signal_rms_counts,
                    converter_min=case.converter_min,
                    converter_max=case.converter_max,
                    clipping_policy=case.clipping_policy,
                    include_campaign_truth=campaign_truth,
                )
            else:
                assert recorded_background is not None
                combined, receiver_truth = _recorded_background_samples(
                    background=recorded_receiver_words[receiver_index],
                    lineage=recorded_background,
                    signal=signal,
                    path=path,
                    requested_signal_rms_counts=requested_signal_rms_counts,
                    converter_min=case.converter_min,
                    converter_max=case.converter_max,
                    clipping_policy=case.clipping_policy,
                )
            receiver_words.append(combined)
            receivers_truth.append(
                {
                    "receiver_chain_id": str(plan.receiver_chain_ids[receiver_index]),
                    **receiver_truth,
                    "ci16_sha256": hashlib.sha256(
                        _pack_single_receiver(combined)
                    ).hexdigest(),
                }
            )
        paired = _interleave_pair(receiver_words[0], receiver_words[1])
        output_segments.append(PairedScanSegment(segment.segment_id, paired))
        segment_document = {
            "segment_id": str(segment.segment_id),
            "channel": channel,
            "edge": edge,
            "center_frequency_hz": segment.center_frequency_hz,
            "expected_signal_present": contains_signal,
            "expected_pilot_local_offsets_hz": (
                list(pilot_offsets) if contains_signal else []
            ),
            "expected_pilot_center_frequencies_hz": (
                [segment.center_frequency_hz + offset for offset in pilot_offsets]
                if contains_signal
                else []
            ),
            "sample_count": sample_count,
            "paired_ci16_bytes": len(paired),
            "paired_ci16_sha256": hashlib.sha256(paired).hexdigest(),
            "receivers": receivers_truth,
        }
        if campaign_truth:
            segment_document["expected_pilot_ending_offsets_hz"] = (
                list(ending_pilot_offsets) if contains_signal else []
            )
        segment_truth.append(segment_document)

    source_truth = None if source is None else source.truth
    case_document: dict[str, Any] = {
        "signal_present": case.signal_present,
        "target_channels": list(case.target_channels),
        "edge": case.edge,
        "pilot_indices": list(case.pilot_indices),
        "pilot_local_offsets_hz": list(
            pilot_local_offsets_hz(case.edge, case.pilot_indices)
        ),
        "expected_pilot_offsets_with_cfo_hz": list(pilot_offsets),
        "occupied_half_bandwidth_hz": occupied_edge_hz,
        "seed_u64": case.seed_u64,
        "source_signal_rms_counts": case.source_signal_rms_counts,
        "cfo_hz": case.cfo_hz,
        "frame_phase": case.frame_phase,
        "source_reference_segment_id": str(target_requests[0].segment_id),
        "source_reference_if_center_hz": target_requests[0].center_frequency_hz,
    }
    if campaign_truth:
        case_document.update(
            {
                "frequency_drift_hz_s": case.frequency_drift_hz_s,
                "clipping_policy": case.clipping_policy,
                "background_kind": (
                    "recorded_receiver_background"
                    if background_by_segment
                    else "deterministic_synthetic_uniform"
                ),
            }
        )
    truth: dict[str, Any] = {
        "schema": CAMPAIGN_SCHEMA if campaign_truth else SCHEMA,
        "plan_id": str(plan.plan_id),
        "plan_digest": str(canonical_digest(plan)),
        "generator": GENERATOR_ID,
        "model_scope": (
            "published coded edge pilots plus deterministic receiver impairments; "
            "not a complete Starlink downlink or RF-channel model"
        ),
        "sample_contract": {
            "dtype": "int16_le",
            "layout": "sample,receiver,component",
            "receiver_chain_ids": [str(value) for value in plan.receiver_chain_ids],
            "component_order": ["i", "q"],
            "bytes_per_paired_sample": 8,
        },
        "case": case_document,
        "expected_target_segment_ids": [
            str(segment.segment_id)
            for segment in requests
            if segment.segment_id in expected_targets
        ],
        "source_fixture_truth": source_truth,
        "comparison_lineage": (
            "counterfactual-compatible frozen background: null and injections must "
            "reuse the exact background segment digest; recorded background signal "
            "status remains unknown"
            if campaign_truth
            else "counterfactual-compatible frozen background: equal base seed, channel, "
            "edge, receiver ID, path, and sample count produce byte-identical base noise"
        ),
        "segments": segment_truth,
    }
    return PairedStarlinkScanFixture(tuple(output_segments), _canonical_json(truth))


def _segment_coordinates(segment: SegmentRequest) -> tuple[int, str]:
    tags = dict(segment.tags)
    channel = tags.get("channel")
    edge = tags.get("edge")
    if isinstance(channel, bool) or not isinstance(channel, int):
        raise StarlinkScanFixtureError("scan segment lacks an integer channel tag")
    if not isinstance(edge, str) or edge not in ("lower", "upper"):
        raise StarlinkScanFixtureError("scan segment lacks a valid edge tag")
    return channel, edge


def _unpack_single_receiver(payload: bytes) -> tuple[complex, ...]:
    words = struct.unpack(f"<{len(payload) // 2}h", payload)
    return tuple(
        complex(words[index], words[index + 1]) for index in range(0, len(words), 2)
    )


def _receiver_signal(
    source: tuple[complex, ...], path: ReceiverPath
) -> tuple[complex, ...]:
    rotation = path.gain_linear * complex(
        math.cos(path.phase_offset_rad), math.sin(path.phase_offset_rad)
    )
    delayed = [0j] * path.integer_delay_samples + list(
        source[: len(source) - path.integer_delay_samples]
    )
    return tuple(value * rotation for value in delayed)


def _receiver_samples(
    *,
    sample_count: int,
    signal: tuple[complex, ...] | None,
    path: ReceiverPath,
    noise_seed: int,
    requested_signal_rms_counts: float | None,
    converter_min: int,
    converter_max: int,
    clipping_policy: Literal["reject", "saturate_and_report"],
    include_campaign_truth: bool,
) -> tuple[tuple[tuple[int, int], ...], dict[str, Any]]:
    unit_noise = _unit_uniform_noise(sample_count, noise_seed)
    active = (
        () if signal is None else tuple(i for i, value in enumerate(signal) if value)
    )
    signal_rms = _rms(signal, active) if signal is not None and active else 0.0
    normalization_indices = tuple(range(sample_count))
    raw_noise_rms = _rms(unit_noise, normalization_indices)
    noise_scale = (
        0.0
        if path.ambient_noise_rms_counts == 0
        else path.ambient_noise_rms_counts / raw_noise_rms
    )
    noise = tuple(value * noise_scale for value in unit_noise)
    base_noise_words = tuple(
        (
            _round_ties_away_from_zero(value.real),
            _round_ties_away_from_zero(value.imag),
        )
        for value in noise
    )
    if any(
        not (
            converter_min <= i_value <= converter_max
            and converter_min <= q_value <= converter_max
        )
        for i_value, q_value in base_noise_words
    ):
        raise StarlinkScanFixtureError(
            "frozen receiver background would clip the converter envelope"
        )
    achieved_noise_rms = (
        _rms(noise, active) if active else _rms(noise, normalization_indices)
    )
    requested_snr = (
        20 * math.log10(requested_signal_rms_counts / path.ambient_noise_rms_counts)
        if requested_signal_rms_counts is not None and path.ambient_noise_rms_counts > 0
        else None
    )
    achieved_snr = (
        20 * math.log10(signal_rms / achieved_noise_rms)
        if signal_rms > 0 and achieved_noise_rms > 0
        else None
    )

    combined: list[tuple[int, int]] = []
    peak_component = 0
    clipped_component_count = 0
    for index, noise_value in enumerate(noise):
        value = noise_value + (0j if signal is None else signal[index])
        i_value = _round_ties_away_from_zero(value.real)
        q_value = _round_ties_away_from_zero(value.imag)
        current_clipped = sum(
            component < converter_min or component > converter_max
            for component in (i_value, q_value)
        )
        clipped_component_count += current_clipped
        if current_clipped and clipping_policy == "reject":
            raise StarlinkScanFixtureError(
                "receiver signal plus noise would clip the converter envelope"
            )
        i_value = min(converter_max, max(converter_min, i_value))
        q_value = min(converter_max, max(converter_min, q_value))
        combined.append((i_value, q_value))
        peak_component = max(peak_component, abs(i_value), abs(q_value))

    truth: dict[str, Any] = {
        "integer_delay_samples": path.integer_delay_samples,
        "gain_linear": path.gain_linear,
        "phase_offset_rad": path.phase_offset_rad,
        "noise_seed_u64": noise_seed,
        "ambient_noise_rms_counts": path.ambient_noise_rms_counts,
        "base_noise_ci16_sha256": hashlib.sha256(
            _pack_single_receiver(base_noise_words)
        ).hexdigest(),
        "requested_snr_db": requested_snr,
        "achieved_prequantization_snr_db": achieved_snr,
        "achieved_signal_rms_counts": signal_rms,
        "achieved_noise_rms_counts": achieved_noise_rms,
        "active_signal_sample_count": len(active),
        "peak_component_counts": peak_component,
        "clipped_component_count": clipped_component_count,
        "snr_basis": "signal/noise power over delayed coded-pilot samples before output quantization",
    }
    if include_campaign_truth:
        truth.update(
            {
                "background_clipped_component_count": 0,
                "injection_added_clipped_component_count": clipped_component_count,
                "clipping_policy": clipping_policy,
                "background_semantics": "deterministic synthetic receiver noise",
            }
        )
    return tuple(combined), truth


def _recorded_background_samples(
    *,
    background: tuple[tuple[int, int], ...],
    lineage: FrozenPairedBackground,
    signal: tuple[complex, ...] | None,
    path: ReceiverPath,
    requested_signal_rms_counts: float | None,
    converter_min: int,
    converter_max: int,
    clipping_policy: Literal["reject", "saturate_and_report"],
) -> tuple[tuple[tuple[int, int], ...], dict[str, Any]]:
    active = (
        ()
        if signal is None
        else tuple(index for index, value in enumerate(signal) if value)
    )
    background_complex = tuple(
        complex(i_value, q_value) for i_value, q_value in background
    )
    signal_rms = _rms(signal, active) if signal is not None and active else 0.0
    background_indices = active or tuple(range(len(background_complex)))
    background_rms = _rms(background_complex, background_indices)
    achieved_ratio = (
        20 * math.log10(signal_rms / background_rms)
        if signal_rms > 0 and background_rms > 0
        else None
    )
    requested_ratio = (
        20 * math.log10(requested_signal_rms_counts / background_rms)
        if requested_signal_rms_counts is not None and background_rms > 0
        else None
    )
    background_clipped = sum(
        component < converter_min or component > converter_max
        for sample in background
        for component in sample
    )
    output: list[tuple[int, int]] = []
    total_clipped = 0
    added_clipped = 0
    peak = 0
    for index, (base_i, base_q) in enumerate(background):
        addition = 0j if signal is None else signal[index]
        i_value = _round_ties_away_from_zero(base_i + addition.real)
        q_value = _round_ties_away_from_zero(base_q + addition.imag)
        for value, base in ((i_value, base_i), (q_value, base_q)):
            if value < converter_min or value > converter_max:
                total_clipped += 1
                if converter_min <= base <= converter_max:
                    added_clipped += 1
        if total_clipped and clipping_policy == "reject":
            raise StarlinkScanFixtureError(
                "recorded background plus signal would clip the converter envelope"
            )
        bounded_i = (
            base_i
            if signal is None
            else min(converter_max, max(converter_min, i_value))
        )
        bounded_q = (
            base_q
            if signal is None
            else min(converter_max, max(converter_min, q_value))
        )
        output.append((bounded_i, bounded_q))
        peak = max(peak, abs(bounded_i), abs(bounded_q))

    packed_background = _pack_single_receiver(background)
    return tuple(output), {
        "integer_delay_samples": path.integer_delay_samples,
        "gain_linear": path.gain_linear,
        "phase_offset_rad": path.phase_offset_rad,
        "noise_seed_u64": None,
        "ambient_noise_rms_counts": None,
        "base_noise_ci16_sha256": hashlib.sha256(packed_background).hexdigest(),
        "requested_snr_db": requested_ratio,
        "achieved_prequantization_snr_db": achieved_ratio,
        "achieved_signal_rms_counts": signal_rms,
        "achieved_noise_rms_counts": background_rms,
        "active_signal_sample_count": len(active),
        "peak_component_counts": peak,
        "clipped_component_count": total_clipped,
        "background_clipped_component_count": background_clipped,
        "injection_added_clipped_component_count": added_clipped,
        "clipping_policy": clipping_policy,
        "background_semantics": (
            "recorded receiver background; injection-to-background ratio only; "
            "source signal status unknown"
        ),
        "snr_basis": (
            "injected signal/background power over delayed coded-pilot samples "
            "before output quantization; not calibrated RF SNR"
        ),
        "recorded_background_lineage": {
            "schema": RECORDED_BACKGROUND_SCHEMA,
            "source_recording_id": lineage.source_recording_id,
            "declared_source_recording_data_sha256": (
                lineage.declared_source_recording_data_sha256
            ),
            "source_start_sample": lineage.source_start_sample,
            "source_sample_count": len(background),
            "source_segment_sha256": lineage.source_segment_sha256,
            "source_signal_status": lineage.source_signal_status,
        },
    }


def _rms(values: tuple[complex, ...], indices: tuple[int, ...]) -> float:
    return math.sqrt(sum(abs(values[index]) ** 2 for index in indices) / len(indices))


def _derived_seed(
    seed: int,
    channel: int,
    edge: str,
    receiver_chain_id: ReceiverChainId,
) -> int:
    digest = hashlib.sha256(
        f"{seed}:channel:{channel}:edge:{edge}:receiver:{receiver_chain_id}".encode(
            "ascii"
        )
    ).digest()
    value = int.from_bytes(digest[:8], "big")
    return value or 1


def _unit_uniform_noise(count: int, seed: int) -> tuple[complex, ...]:
    state = seed
    output: list[complex] = []
    for _ in range(count):
        state, first = _xorshift64star(state)
        state, second = _xorshift64star(state)
        output.append(
            complex(2 * ((first >> 11) / 2**53) - 1, 2 * ((second >> 11) / 2**53) - 1)
        )
    return tuple(output)


def _xorshift64star(state: int) -> tuple[int, int]:
    mask64 = (1 << 64) - 1
    state ^= state >> 12
    state ^= (state << 25) & mask64
    state ^= state >> 27
    state &= mask64
    return state, (state * 2685821657736338717) & mask64


def _round_ties_away_from_zero(value: float) -> int:
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)


def _pack_single_receiver(samples: tuple[tuple[int, int], ...]) -> bytes:
    words = tuple(component for sample in samples for component in sample)
    return struct.pack(f"<{len(words)}h", *words)


def _unpack_paired_receivers(
    payload: bytes,
) -> tuple[tuple[tuple[int, int], ...], tuple[tuple[int, int], ...]]:
    words = struct.unpack(f"<{len(payload) // 2}h", payload)
    first = tuple((words[index], words[index + 1]) for index in range(0, len(words), 4))
    second = tuple(
        (words[index + 2], words[index + 3]) for index in range(0, len(words), 4)
    )
    return first, second


def _interleave_pair(
    first: tuple[tuple[int, int], ...], second: tuple[tuple[int, int], ...]
) -> bytes:
    words = tuple(
        component
        for sample_pair in zip(first, second, strict=True)
        for sample in sample_pair
        for component in sample
    )
    return struct.pack(f"<{len(words)}h", *words)


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")
