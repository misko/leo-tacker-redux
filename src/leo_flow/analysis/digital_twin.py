"""Deterministic, dependency-free RF digital-twin generation and comparison."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any, cast

from leo_flow.contracts.core import (
    V0_1,
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    Provenance,
    RadioId,
    ReceiverChainId,
    SchemaRef,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.digital_twin import (
    MAX_TWIN_JSON_BYTES,
    MAX_TWIN_STATISTIC_VALUES,
    DigitalTwinAnalyzerInputV0_1,
    DigitalTwinBroadbandInterferenceV0_1,
    DigitalTwinBundleV0_1,
    DigitalTwinBurstScheduleV0_1,
    DigitalTwinComparisonViewV0_1,
    DigitalTwinDistributionComparisonV0_1,
    DigitalTwinDistributionFactsV0_1,
    DigitalTwinDopplerAnalyzerOutputV0_1,
    DigitalTwinDopplerAnalyzerPortV0_1,
    DigitalTwinEmissionKind,
    DigitalTwinFloatRangeV0_1,
    DigitalTwinImpairmentConfigV0_1,
    DigitalTwinObservationV0_1,
    DigitalTwinPathPointTruthV0_1,
    DigitalTwinPatternRole,
    DigitalTwinPilotAnalyzerOutputV0_1,
    DigitalTwinPilotAnalyzerPortV0_1,
    DigitalTwinPilotPatternV0_1,
    DigitalTwinRealDataSummaryV0_1,
    DigitalTwinReceiverConfigV0_1,
    DigitalTwinReceiverTruthV0_1,
    DigitalTwinScenarioRequestV0_1,
    DigitalTwinScenarioV0_1,
    DigitalTwinStatisticKind,
    DigitalTwinToneInterferenceV0_1,
    DigitalTwinTrialAnalysisV0_1,
    DigitalTwinTruthV0_1,
)
from leo_flow.contracts.starlink import StarlinkEdge

_MASK_64 = 0xFFFF_FFFF_FFFF_FFFF
GENERATOR_ALGORITHM = "rf-digital-twin-v0.1"
PRNG_ALGORITHM = "splitmix64-box-muller-v0.1"


class _SplitMix64:
    def __init__(self, seed: int) -> None:
        self._state = seed & _MASK_64
        self._spare_normal: float | None = None

    def next_u64(self) -> int:
        self._state = (self._state + 0x9E37_79B9_7F4A_7C15) & _MASK_64
        value = self._state
        value = ((value ^ (value >> 30)) * 0xBF58_476D_1CE4_E5B9) & _MASK_64
        value = ((value ^ (value >> 27)) * 0x94D0_49BB_1331_11EB) & _MASK_64
        return (value ^ (value >> 31)) & _MASK_64

    def uniform(self) -> float:
        return ((self.next_u64() >> 11) + 0.5) / (1 << 53)

    def normal(self) -> float:
        if self._spare_normal is not None:
            value = self._spare_normal
            self._spare_normal = None
            return value
        radius = math.sqrt(-2.0 * math.log(self.uniform()))
        angle = 2.0 * math.pi * self.uniform()
        self._spare_normal = radius * math.sin(angle)
        return radius * math.cos(angle)


def make_qin_pattern_v0_1(
    *,
    pattern_id: str,
    source_template_ref: ArtifactRef,
    edge: StarlinkEdge,
    sample_rate_hz: float,
    samples: Sequence[complex],
) -> DigitalTwinPilotPatternV0_1:
    """Seal externally supplied exact Qin samples into the public twin value."""

    i_samples = tuple(float(value.real) for value in samples)
    q_samples = tuple(float(value.imag) for value in samples)
    digest = _sample_values_digest(i_samples, q_samples)
    energy = sum(i * i + q * q for i, q in zip(i_samples, q_samples, strict=True))
    indices = (
        tuple(range(528, 536)) if edge is StarlinkEdge.LOWER else tuple(range(488, 496))
    )
    return DigitalTwinPilotPatternV0_1(
        SchemaRef(DigitalTwinPilotPatternV0_1.SCHEMA_ID, V0_1),
        pattern_id,
        DigitalTwinPatternRole.QIN_EXACT,
        source_template_ref,
        edge,
        indices,
        None,
        None,
        sample_rate_hz,
        i_samples,
        q_samples,
        digest,
        energy,
    )


def generate_surrogate_patterns_v0_1(
    exact: DigitalTwinPilotPatternV0_1,
    *,
    seed: int,
    count: int,
) -> tuple[DigitalTwinPilotPatternV0_1, ...]:
    """Generate precommitted QPSK-phase surrogates with matched sample energy."""

    if exact.role is not DigitalTwinPatternRole.QIN_EXACT:
        raise ValueError("surrogates require the exact Qin target pattern")
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= _MASK_64:
        raise ValueError("surrogate seed must be an unsigned 64-bit integer")
    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 32:
        raise ValueError("surrogate count is outside its bound")
    result: list[DigitalTwinPilotPatternV0_1] = []
    scale = 1.0 / math.sqrt(2.0)
    for index in range(count):
        random = _SplitMix64(_derive_seed(seed, index + 1))
        i_values: list[float] = []
        q_values: list[float] = []
        for exact_i, exact_q in zip(exact.i_samples, exact.q_samples, strict=True):
            magnitude = math.hypot(exact_i, exact_q)
            quadrant = random.next_u64() & 3
            i_values.append(magnitude * scale * (1.0 if quadrant in (0, 3) else -1.0))
            q_values.append(magnitude * scale * (1.0 if quadrant in (0, 1) else -1.0))
        i_samples = tuple(i_values)
        q_samples = tuple(q_values)
        energy = sum(i * i + q * q for i, q in zip(i_samples, q_samples, strict=True))
        result.append(
            DigitalTwinPilotPatternV0_1(
                SchemaRef(DigitalTwinPilotPatternV0_1.SCHEMA_ID, V0_1),
                f"dtpattern_surrogate_{seed:016x}_{index:02d}",
                DigitalTwinPatternRole.SURROGATE,
                exact.source_template_ref,
                exact.edge,
                exact.pilot_indices,
                seed,
                index,
                exact.sample_rate_hz,
                i_samples,
                q_samples,
                _sample_values_digest(i_samples, q_samples),
                energy,
            )
        )
    return tuple(result)


def generate_digital_twin_v0_1(
    request: DigitalTwinScenarioRequestV0_1,
) -> DigitalTwinBundleV0_1:
    """Materialize one deterministic scenario, exact truth, and receiver samples."""

    request_digest = Digest.sha256(canonical_json_bytes(request))
    random = _SplitMix64(request.seed)
    cfo = _sample_range(random, request.cfo_hz)
    drift_rate = _sample_range(random, request.drift_rate_hz_s)
    acceleration = _sample_range(random, request.drift_acceleration_hz_s2)
    amplitude = (
        0.0
        if request.emission_kind is DigitalTwinEmissionKind.NULL
        else _sample_range(random, request.amplitude)
    )
    surrogates = generate_surrogate_patterns_v0_1(
        request.exact_qin_pattern,
        seed=request.surrogate_seed,
        count=request.surrogate_count,
    )
    patterns = (request.exact_qin_pattern, *surrogates)
    emitted = _emitted_pattern(request, surrogates)
    scenario = DigitalTwinScenarioV0_1(
        request_digest,
        request.request_id,
        request.seed,
        emitted.pattern_id if emitted is not None else None,
        request.emission_kind,
        request.center_frequency_hz,
        request.sample_rate_hz,
        request.frame_count,
        cfo,
        drift_rate,
        acceleration,
        amplitude,
        request.burst,
        request.receivers,
        request.impairments,
        request.generator_ref,
        PRNG_ALGORITHM,
    )
    truth = _truth(scenario)
    observations = tuple(
        _synthesize_receiver(
            scenario,
            receiver,
            emitted,
            _derive_seed(request.seed, receiver_index + 0x1000),
        )
        for receiver_index, receiver in enumerate(request.receivers)
    )
    warnings = ("deterministic-digital-twin-not-real-rf",)
    return DigitalTwinBundleV0_1(
        SchemaRef(DigitalTwinBundleV0_1.SCHEMA_ID, V0_1),
        scenario,
        patterns,
        truth,
        observations,
        request.provenance,
        warnings,
    )


def analyze_digital_twin_v0_1(
    bundle: DigitalTwinBundleV0_1,
    *,
    doppler_analyzer: DigitalTwinDopplerAnalyzerPortV0_1,
    pilot_analyzer: DigitalTwinPilotAnalyzerPortV0_1,
) -> DigitalTwinTrialAnalysisV0_1:
    """Run injected analyzers without exposing scenario truth to either port."""

    doppler_outputs: list[DigitalTwinDopplerAnalyzerOutputV0_1] = []
    pilot_outputs: list[DigitalTwinPilotAnalyzerOutputV0_1] = []
    warnings = set(bundle.warnings)
    warnings.add("candidate-only-analysis-not-calibration-or-detection")
    for observation in bundle.observations:
        analyzer_input = DigitalTwinAnalyzerInputV0_1(
            bundle.scenario.request_digest, observation, bundle.patterns
        )
        doppler = doppler_analyzer.analyze_doppler(analyzer_input)
        pilot = pilot_analyzer.analyze_pilot(analyzer_input)
        _validate_analyzer_identity(bundle, observation, doppler)
        _validate_analyzer_identity(bundle, observation, pilot)
        doppler_outputs.append(doppler)
        pilot_outputs.append(pilot)
        warnings.update(doppler.warnings)
        warnings.update(pilot.warnings)
    return DigitalTwinTrialAnalysisV0_1(
        SchemaRef(DigitalTwinTrialAnalysisV0_1.SCHEMA_ID, V0_1),
        bundle.scenario.request_digest,
        bundle.digest,
        Digest.sha256(canonical_json_bytes(bundle.truth)),
        True,
        None,
        tuple(doppler_outputs),
        tuple(pilot_outputs),
        tuple(sorted(warnings)),
    )


def compare_digital_twin_to_real_v0_1(
    analyses: Sequence[DigitalTwinTrialAnalysisV0_1],
    real_summary: DigitalTwinRealDataSummaryV0_1,
) -> DigitalTwinComparisonViewV0_1:
    """Compare candidate/control distributions without deriving thresholds."""

    if not analyses:
        raise ValueError("comparison requires at least one digital-twin analysis")
    if len(analyses) > MAX_TWIN_STATISTIC_VALUES:
        raise ValueError("digital-twin analysis count exceeds its comparison bound")
    digests = tuple(item.twin_bundle_digest for item in analyses)
    if len(digests) != len(set(digests)):
        raise ValueError("digital-twin analyses contain duplicate bundle identities")
    twin_values: dict[tuple[str, DigitalTwinStatisticKind], list[float]] = defaultdict(
        list
    )
    for analysis in analyses:
        for output in analysis.doppler:
            for statistic in output.statistics:
                twin_values[(statistic.method_id, statistic.statistic)].append(
                    statistic.value
                )
                if (
                    len(twin_values[(statistic.method_id, statistic.statistic)])
                    > MAX_TWIN_STATISTIC_VALUES
                ):
                    raise ValueError("twin statistic distribution exceeds its bound")
        for pilot_output in analysis.pilot:
            for statistic in pilot_output.statistics:
                twin_values[(statistic.method_id, statistic.statistic)].append(
                    statistic.value
                )
                if (
                    len(twin_values[(statistic.method_id, statistic.statistic)])
                    > MAX_TWIN_STATISTIC_VALUES
                ):
                    raise ValueError("twin statistic distribution exceeds its bound")
    real_values = {
        (item.method_id, item.statistic): item.values
        for item in real_summary.distributions
    }
    common = sorted(
        set(twin_values) & set(real_values), key=lambda item: (item[0], item[1].value)
    )
    comparisons = tuple(
        DigitalTwinDistributionComparisonV0_1(
            method_id,
            statistic,
            _distribution_facts(twin_values[(method_id, statistic)]),
            _distribution_facts(real_values[(method_id, statistic)]),
            _mean(twin_values[(method_id, statistic)])
            - _mean(real_values[(method_id, statistic)]),
            _quantile(sorted(twin_values[(method_id, statistic)]), 0.5)
            - _quantile(sorted(real_values[(method_id, statistic)]), 0.5),
            _empirical_ks(
                twin_values[(method_id, statistic)],
                real_values[(method_id, statistic)],
            ),
        )
        for method_id, statistic in common
    )
    twin_only = tuple(
        sorted(_statistic_label(key) for key in set(twin_values) - set(real_values))
    )
    real_only = tuple(
        sorted(_statistic_label(key) for key in set(real_values) - set(twin_values))
    )
    warnings = (
        DigitalTwinComparisonViewV0_1.REQUIRED_WARNING,
        "distribution-distance-is-descriptive-only",
    )
    return DigitalTwinComparisonViewV0_1(
        SchemaRef(DigitalTwinComparisonViewV0_1.SCHEMA_ID, V0_1),
        digests,
        real_summary.source_summary_digest,
        real_summary.window_label,
        True,
        None,
        comparisons,
        twin_only,
        real_only,
        tuple(sorted(warnings)),
    )


def encode_digital_twin_bundle_v0_1(bundle: DigitalTwinBundleV0_1) -> bytes:
    return canonical_json_bytes(bundle)


def decode_digital_twin_bundle_v0_1(payload: bytes) -> DigitalTwinBundleV0_1:
    if len(payload) > MAX_TWIN_JSON_BYTES:
        raise ValueError("digital-twin bundle exceeds its JSON byte bound")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("digital-twin bundle is not valid JSON") from error
    try:
        if not isinstance(value, dict):
            raise TypeError("digital-twin bundle must be a JSON object")
        bundle = _decode_bundle(cast(Mapping[str, Any], value))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("digital-twin bundle does not match v0.1") from error
    if encode_digital_twin_bundle_v0_1(bundle) != payload:
        raise ValueError("digital-twin bundle is not canonical v0.1 JSON")
    return bundle


def _truth(scenario: DigitalTwinScenarioV0_1) -> DigitalTwinTruthV0_1:
    frame_rate = 750.0
    physical_presence = tuple(
        scenario.emission_kind is not DigitalTwinEmissionKind.NULL
        and _burst_on(scenario.burst, frame)
        for frame in range(scenario.frame_count)
    )
    receiver_truth = tuple(
        DigitalTwinReceiverTruthV0_1(
            receiver.radio_id,
            receiver.receiver_chain_id,
            receiver.lnb_offset_hz,
            receiver.missing_frame_indices,
            tuple(
                DigitalTwinPathPointTruthV0_1(
                    frame,
                    (frame + 0.5) / frame_rate,
                    physical_presence[frame],
                    scenario.cfo_hz
                    + receiver.lnb_offset_hz
                    + scenario.drift_rate_hz_s * ((frame + 0.5) / frame_rate)
                    + 0.5
                    * scenario.drift_acceleration_hz_s2
                    * ((frame + 0.5) / frame_rate) ** 2,
                )
                for frame in range(scenario.frame_count)
            ),
        )
        for receiver in scenario.receivers
    )
    present_count = sum(physical_presence)
    return DigitalTwinTruthV0_1(
        scenario.request_digest,
        scenario.emission_kind,
        scenario.emitted_pattern_id,
        present_count > 0,
        present_count,
        scenario.drift_rate_hz_s,
        scenario.drift_acceleration_hz_s2,
        receiver_truth,
    )


def _synthesize_receiver(
    scenario: DigitalTwinScenarioV0_1,
    receiver: DigitalTwinReceiverConfigV0_1,
    emitted: DigitalTwinPilotPatternV0_1 | None,
    seed: int,
) -> DigitalTwinObservationV0_1:
    random = _SplitMix64(seed)
    frame_samples = round(scenario.sample_rate_hz / 750.0)
    total_samples = frame_samples * scenario.frame_count
    missing = set(receiver.missing_frame_indices)
    i_values: list[float] = []
    q_values: list[float] = []
    for sample_index in range(total_samples):
        frame = sample_index // frame_samples
        if frame in missing:
            i_values.append(0.0)
            q_values.append(0.0)
            continue
        time_s = sample_index / scenario.sample_rate_hz
        gain = receiver.gain_linear * (
            1.0
            + scenario.impairments.gain_variation_fraction
            * math.sin(
                2.0
                * math.pi
                * frame
                / scenario.impairments.gain_variation_period_frames
                + receiver.phase_rad
            )
        )
        value = 0j
        if emitted is not None and _burst_on(scenario.burst, frame):
            local = sample_index % frame_samples
            pattern_value = complex(emitted.i_samples[local], emitted.q_samples[local])
            phase = receiver.phase_rad + 2.0 * math.pi * (
                (scenario.cfo_hz + receiver.lnb_offset_hz) * time_s
                + 0.5 * scenario.drift_rate_hz_s * time_s**2
                + scenario.drift_acceleration_hz_s2 * time_s**3 / 6.0
            )
            value += scenario.amplitude * gain * pattern_value * _phasor(phase)
        for tone in (
            *scenario.impairments.stationary_tones,
            *scenario.impairments.narrowband_interferers,
        ):
            phase = tone.phase_rad + 2.0 * math.pi * (
                tone.offset_hz * time_s + 0.5 * tone.drift_rate_hz_s * time_s**2
            )
            value += tone.amplitude * gain * _phasor(phase)
        broadband = scenario.impairments.broadband
        if broadband is not None and _burst_on(broadband.burst, frame):
            value += complex(
                broadband.standard_deviation * random.normal(),
                broadband.standard_deviation * random.normal(),
            )
        value += complex(
            scenario.impairments.awgn_standard_deviation * random.normal(),
            scenario.impairments.awgn_standard_deviation * random.normal(),
        )
        i_values.append(float(value.real))
        q_values.append(float(value.imag))
    i_samples = tuple(i_values)
    q_samples = tuple(q_values)
    return DigitalTwinObservationV0_1(
        receiver.radio_id,
        receiver.receiver_chain_id,
        scenario.sample_rate_hz,
        frame_samples,
        scenario.frame_count,
        receiver.missing_frame_indices,
        i_samples,
        q_samples,
        _sample_values_digest(i_samples, q_samples),
    )


def _emitted_pattern(
    request: DigitalTwinScenarioRequestV0_1,
    surrogates: tuple[DigitalTwinPilotPatternV0_1, ...],
) -> DigitalTwinPilotPatternV0_1 | None:
    if request.emission_kind is DigitalTwinEmissionKind.NULL:
        return None
    if request.emission_kind is DigitalTwinEmissionKind.QIN_EXACT:
        return request.exact_qin_pattern
    assert request.emission_surrogate_index is not None
    return surrogates[request.emission_surrogate_index]


def _sample_values_digest(
    i_samples: tuple[float, ...], q_samples: tuple[float, ...]
) -> Digest:
    return Digest.sha256(
        canonical_json_bytes({"i_samples": i_samples, "q_samples": q_samples})
    )


def _sample_range(random: _SplitMix64, bounds: DigitalTwinFloatRangeV0_1) -> float:
    return bounds.minimum + (bounds.maximum - bounds.minimum) * random.uniform()


def _derive_seed(seed: int, stream: int) -> int:
    mixer = _SplitMix64((seed ^ (stream * 0xD134_2543_DE82_EF95)) & _MASK_64)
    return mixer.next_u64()


def _burst_on(schedule: DigitalTwinBurstScheduleV0_1, frame: int) -> bool:
    return (frame - schedule.phase_frames) % schedule.period_frames < schedule.on_frames


def _phasor(phase: float) -> complex:
    return complex(math.cos(phase), math.sin(phase))


def _validate_analyzer_identity(
    bundle: DigitalTwinBundleV0_1,
    observation: DigitalTwinObservationV0_1,
    output: DigitalTwinDopplerAnalyzerOutputV0_1 | DigitalTwinPilotAnalyzerOutputV0_1,
) -> None:
    if (
        output.scenario_request_digest != bundle.scenario.request_digest
        or output.radio_id != observation.radio_id
        or output.receiver_chain_id != observation.receiver_chain_id
    ):
        raise ValueError("analyzer output identity differs from its twin input")


def _distribution_facts(values: Sequence[float]) -> DigitalTwinDistributionFactsV0_1:
    ordered = sorted(values)
    mean = _mean(ordered)
    deviation = math.sqrt(sum((value - mean) ** 2 for value in ordered) / len(ordered))
    return DigitalTwinDistributionFactsV0_1(
        len(ordered),
        mean,
        deviation,
        ordered[0],
        _quantile(ordered, 0.1),
        _quantile(ordered, 0.5),
        _quantile(ordered, 0.9),
        ordered[-1],
    )


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _quantile(ordered: Sequence[float], fraction: float) -> float:
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _empirical_ks(first: Sequence[float], second: Sequence[float]) -> float:
    first_ordered = sorted(first)
    second_ordered = sorted(second)
    values = sorted(set(first_ordered) | set(second_ordered))
    first_index = 0
    second_index = 0
    distance = 0.0
    for value in values:
        while first_index < len(first_ordered) and first_ordered[first_index] <= value:
            first_index += 1
        while (
            second_index < len(second_ordered) and second_ordered[second_index] <= value
        ):
            second_index += 1
        distance = max(
            distance,
            abs(first_index / len(first_ordered) - second_index / len(second_ordered)),
        )
    return distance


def _statistic_label(key: tuple[str, DigitalTwinStatisticKind]) -> str:
    return f"{key[0]}:{key[1].value}"


def _decode_bundle(value: Mapping[str, Any]) -> DigitalTwinBundleV0_1:
    _require_keys(
        value,
        {
            "schema",
            "scenario",
            "patterns",
            "truth",
            "observations",
            "provenance",
            "warnings",
        },
    )
    scenario = _decode_scenario(_mapping(value["scenario"]))
    patterns = tuple(
        _decode_pattern(_mapping(item)) for item in _sequence(value["patterns"])
    )
    truth = _decode_truth(_mapping(value["truth"]))
    observations = tuple(
        _decode_observation(_mapping(item)) for item in _sequence(value["observations"])
    )
    return DigitalTwinBundleV0_1(
        _schema(_mapping(value["schema"])),
        scenario,
        patterns,
        truth,
        observations,
        _provenance(_mapping(value["provenance"])),
        _strings(value["warnings"]),
    )


def _decode_pattern(value: Mapping[str, Any]) -> DigitalTwinPilotPatternV0_1:
    return DigitalTwinPilotPatternV0_1(
        _schema(_mapping(value["schema"])),
        str(value["pattern_id"]),
        DigitalTwinPatternRole(str(value["role"])),
        _artifact(_mapping(value["source_template_ref"])),
        StarlinkEdge(str(value["edge"])),
        _ints(value["pilot_indices"]),
        _optional_int(value["surrogate_seed"]),
        _optional_int(value["surrogate_index"]),
        float(value["sample_rate_hz"]),
        _floats(value["i_samples"]),
        _floats(value["q_samples"]),
        _digest(_mapping(value["sample_values_digest"])),
        float(value["energy"]),
    )


def _decode_scenario(value: Mapping[str, Any]) -> DigitalTwinScenarioV0_1:
    return DigitalTwinScenarioV0_1(
        _digest(_mapping(value["request_digest"])),
        str(value["request_id"]),
        int(value["seed"]),
        cast(str | None, value["emitted_pattern_id"]),
        DigitalTwinEmissionKind(str(value["emission_kind"])),
        float(value["center_frequency_hz"]),
        float(value["sample_rate_hz"]),
        int(value["frame_count"]),
        float(value["cfo_hz"]),
        float(value["drift_rate_hz_s"]),
        float(value["drift_acceleration_hz_s2"]),
        float(value["amplitude"]),
        _burst(_mapping(value["burst"])),
        tuple(_receiver(_mapping(item)) for item in _sequence(value["receivers"])),
        _impairments(_mapping(value["impairments"])),
        _artifact(_mapping(value["generator_ref"])),
        str(value["prng_algorithm"]),
    )


def _decode_truth(value: Mapping[str, Any]) -> DigitalTwinTruthV0_1:
    receivers = tuple(
        DigitalTwinReceiverTruthV0_1(
            RadioId(str(item["radio_id"])),
            ReceiverChainId(str(item["receiver_chain_id"])),
            float(item["lnb_offset_hz"]),
            _ints(item["missing_frame_indices"]),
            tuple(
                DigitalTwinPathPointTruthV0_1(
                    int(point["frame_index"]),
                    float(point["midpoint_seconds"]),
                    bool(point["pilot_present"]),
                    float(point["frequency_offset_hz"]),
                )
                for point in map(_mapping, _sequence(item["path"]))
            ),
        )
        for item in map(_mapping, _sequence(value["receiver_truth"]))
    )
    return DigitalTwinTruthV0_1(
        _digest(_mapping(value["scenario_request_digest"])),
        DigitalTwinEmissionKind(str(value["emission_kind"])),
        cast(str | None, value["emitted_pattern_id"]),
        bool(value["pilot_present"]),
        int(value["pilot_present_frame_count"]),
        float(value["expected_drift_rate_hz_s"]),
        float(value["expected_drift_acceleration_hz_s2"]),
        receivers,
    )


def _decode_observation(value: Mapping[str, Any]) -> DigitalTwinObservationV0_1:
    return DigitalTwinObservationV0_1(
        RadioId(str(value["radio_id"])),
        ReceiverChainId(str(value["receiver_chain_id"])),
        float(value["sample_rate_hz"]),
        int(value["frame_sample_count"]),
        int(value["frame_count"]),
        _ints(value["missing_frame_indices"]),
        _floats(value["i_samples"]),
        _floats(value["q_samples"]),
        _digest(_mapping(value["sample_values_digest"])),
    )


def _burst(value: Mapping[str, Any]) -> DigitalTwinBurstScheduleV0_1:
    return DigitalTwinBurstScheduleV0_1(
        int(value["period_frames"]), int(value["on_frames"]), int(value["phase_frames"])
    )


def _receiver(value: Mapping[str, Any]) -> DigitalTwinReceiverConfigV0_1:
    return DigitalTwinReceiverConfigV0_1(
        RadioId(str(value["radio_id"])),
        ReceiverChainId(str(value["receiver_chain_id"])),
        float(value["lnb_offset_hz"]),
        float(value["gain_linear"]),
        float(value["phase_rad"]),
        _ints(value["missing_frame_indices"]),
    )


def _tone(value: Mapping[str, Any]) -> DigitalTwinToneInterferenceV0_1:
    return DigitalTwinToneInterferenceV0_1(
        float(value["offset_hz"]),
        float(value["amplitude"]),
        float(value["drift_rate_hz_s"]),
        float(value["phase_rad"]),
    )


def _impairments(value: Mapping[str, Any]) -> DigitalTwinImpairmentConfigV0_1:
    broadband = value["broadband"]
    return DigitalTwinImpairmentConfigV0_1(
        float(value["awgn_standard_deviation"]),
        float(value["gain_variation_fraction"]),
        int(value["gain_variation_period_frames"]),
        tuple(_tone(_mapping(item)) for item in _sequence(value["stationary_tones"])),
        tuple(
            _tone(_mapping(item)) for item in _sequence(value["narrowband_interferers"])
        ),
        None
        if broadband is None
        else DigitalTwinBroadbandInterferenceV0_1(
            float(_mapping(broadband)["standard_deviation"]),
            _burst(_mapping(_mapping(broadband)["burst"])),
        ),
    )


def _schema(value: Mapping[str, Any]) -> SchemaRef:
    version = _mapping(value["version"])
    return SchemaRef(
        str(value["schema_id"]),
        type(V0_1)(int(version["major"]), int(version["minor"])),
    )


def _digest(value: Mapping[str, Any]) -> Digest:
    return Digest(DigestAlgorithm(str(value["algorithm"])), str(value["value"]))


def _artifact(value: Mapping[str, Any]) -> ArtifactRef:
    schema = value["schema"]
    return ArtifactRef(
        str(value["artifact_id"]),
        _digest(_mapping(value["digest"])),
        None if schema is None else _schema(_mapping(schema)),
    )


def _provenance(value: Mapping[str, Any]) -> Provenance:
    return Provenance(
        str(value["producer_name"]),
        str(value["producer_version"]),
        str(value["git_commit"]),
        _digest(_mapping(value["environment_digest"])),
        _digest(_mapping(value["normalized_config_digest"])),
        tuple(_digest(_mapping(item)) for item in _sequence(value["input_digests"])),
        tuple(
            _digest(_mapping(item)) for item in _sequence(value["dependency_digests"])
        ),
        UtcNs(int(value["started_utc_ns"])),
        UtcNs(int(value["completed_utc_ns"])),
        str(value["host_class"]),
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("expected JSON object")
    return cast(Mapping[str, Any], value)


def _sequence(value: Any) -> Sequence[Any]:
    if not isinstance(value, list):
        raise TypeError("expected JSON array")
    return cast(Sequence[Any], value)


def _floats(value: Any) -> tuple[float, ...]:
    return tuple(float(item) for item in _sequence(value))


def _ints(value: Any) -> tuple[int, ...]:
    return tuple(int(item) for item in _sequence(value))


def _strings(value: Any) -> tuple[str, ...]:
    return tuple(str(item) for item in _sequence(value))


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _require_keys(value: Mapping[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise ValueError("JSON object fields differ from the v0.1 schema")


__all__ = [
    "GENERATOR_ALGORITHM",
    "PRNG_ALGORITHM",
    "analyze_digital_twin_v0_1",
    "compare_digital_twin_to_real_v0_1",
    "decode_digital_twin_bundle_v0_1",
    "encode_digital_twin_bundle_v0_1",
    "generate_digital_twin_v0_1",
    "generate_surrogate_patterns_v0_1",
    "make_qin_pattern_v0_1",
]
