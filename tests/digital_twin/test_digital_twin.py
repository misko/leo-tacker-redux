from __future__ import annotations

import ast
import json
from dataclasses import replace
from itertools import pairwise
from pathlib import Path

import pytest

from leo_flow.analysis.digital_twin import (
    analyze_digital_twin_v0_1,
    compare_digital_twin_to_real_v0_1,
    decode_digital_twin_bundle_v0_1,
    encode_digital_twin_bundle_v0_1,
    generate_digital_twin_v0_1,
    generate_surrogate_patterns_v0_1,
    make_qin_pattern_v0_1,
)
from leo_flow.contracts.core import (
    V0_1,
    ArtifactRef,
    Digest,
    Provenance,
    RadioId,
    ReceiverChainId,
    SchemaRef,
    UtcNs,
)
from leo_flow.contracts.digital_twin import (
    MAX_TWIN_JSON_BYTES,
    DigitalTwinAnalyzerInputV0_1,
    DigitalTwinAnalyzerStatisticV0_1,
    DigitalTwinBroadbandInterferenceV0_1,
    DigitalTwinBurstScheduleV0_1,
    DigitalTwinCandidatePathPointV0_1,
    DigitalTwinDopplerAnalyzerOutputV0_1,
    DigitalTwinDopplerCandidateV0_1,
    DigitalTwinEmissionKind,
    DigitalTwinFloatRangeV0_1,
    DigitalTwinImpairmentConfigV0_1,
    DigitalTwinPilotAnalyzerOutputV0_1,
    DigitalTwinRealDataSummaryV0_1,
    DigitalTwinReceiverConfigV0_1,
    DigitalTwinScenarioRequestV0_1,
    DigitalTwinStatisticDistributionV0_1,
    DigitalTwinStatisticKind,
    DigitalTwinToneInterferenceV0_1,
)
from leo_flow.contracts.starlink import StarlinkEdge


def digest(label: str) -> Digest:
    return Digest.sha256(label.encode())


def exact_pattern():
    samples = tuple(
        complex(i, q)
        for i, q in (
            (1.0, 0.0),
            (0.5, 0.5),
            (0.0, 1.0),
            (-0.5, 0.5),
            (-1.0, 0.0),
            (-0.5, -0.5),
            (0.0, -1.0),
            (0.5, -0.5),
        )
    )
    return make_qin_pattern_v0_1(
        pattern_id="dtpattern_qin_exact_lower",
        source_template_ref=ArtifactRef(
            "qin-lower-edge-exact-v0.1",
            digest("qin-template-bytes"),
            SchemaRef("org.leo-flow.starlink-edge-pilot-template", V0_1),
        ),
        edge=StarlinkEdge.LOWER,
        sample_rate_hz=6_000.0,
        samples=samples,
    )


def request(
    *,
    seed: int = 42,
    emission: DigitalTwinEmissionKind = DigitalTwinEmissionKind.QIN_EXACT,
    surrogate_index: int | None = None,
) -> DigitalTwinScenarioRequestV0_1:
    exact = exact_pattern()
    generator_ref = ArtifactRef(
        "digital-twin-generator-v0.1",
        digest("digital-twin-generator-source"),
        SchemaRef("org.leo-flow.digital-twin-generator", V0_1),
    )
    provenance = Provenance(
        "digital-twin-test",
        "0.1.0",
        "abc123",
        digest("environment"),
        digest("configuration"),
        (exact.sample_values_digest,),
        (exact.source_template_ref.digest, generator_ref.digest),
        UtcNs(100),
        UtcNs(200),
        "test-host",
    )
    return DigitalTwinScenarioRequestV0_1(
        SchemaRef(DigitalTwinScenarioRequestV0_1.SCHEMA_ID, V0_1),
        "dtscenario_test",
        seed,
        exact,
        9001,
        3,
        emission,
        surrogate_index,
        10_755_000_000.0,
        6_000.0,
        8,
        DigitalTwinFloatRangeV0_1(120.0, 180.0),
        DigitalTwinFloatRangeV0_1(2_000.0, 3_000.0),
        DigitalTwinFloatRangeV0_1(-500.0, 500.0),
        DigitalTwinFloatRangeV0_1(0.8, 1.2),
        DigitalTwinBurstScheduleV0_1(4, 1, 0),
        (
            DigitalTwinReceiverConfigV0_1(
                RadioId("radio_twin_20"),
                ReceiverChainId("rx_twin_a"),
                1_000.0,
                1.0,
                0.0,
                (3,),
            ),
            DigitalTwinReceiverConfigV0_1(
                RadioId("radio_twin_21"),
                ReceiverChainId("rx_twin_b"),
                -2_000.0,
                0.9,
                0.4,
                (5,),
            ),
        ),
        DigitalTwinImpairmentConfigV0_1(
            0.01,
            0.1,
            4,
            (DigitalTwinToneInterferenceV0_1(300.0, 0.1, 0.0, 0.0),),
            (DigitalTwinToneInterferenceV0_1(-700.0, 0.08, 20.0, 0.3),),
            DigitalTwinBroadbandInterferenceV0_1(
                0.02, DigitalTwinBurstScheduleV0_1(4, 2, 1)
            ),
        ),
        generator_ref,
        provenance,
    )


def test_seeded_generation_is_byte_reproducible_and_seed_sensitive() -> None:
    first = generate_digital_twin_v0_1(request())
    second = generate_digital_twin_v0_1(request())
    different = generate_digital_twin_v0_1(request(seed=43))

    assert encode_digital_twin_bundle_v0_1(first) == encode_digital_twin_bundle_v0_1(
        second
    )
    assert first.digest == second.digest
    assert first.digest != different.digest
    assert first.scenario.cfo_hz != different.scenario.cfo_hz
    assert 120.0 <= first.scenario.cfo_hz <= 180.0
    assert 2_000.0 <= first.scenario.drift_rate_hz_s <= 3_000.0
    assert -500.0 <= first.scenario.drift_acceleration_hz_s2 <= 500.0


def test_surrogates_are_precommitted_distinct_and_energy_matched() -> None:
    exact = exact_pattern()
    first = generate_surrogate_patterns_v0_1(exact, seed=7, count=3)
    second = generate_surrogate_patterns_v0_1(exact, seed=7, count=3)

    assert first == second
    assert len({item.sample_values_digest for item in first}) == 3
    assert all(item.source_template_ref == exact.source_template_ref for item in first)
    assert all(item.energy == pytest.approx(exact.energy, rel=1e-12) for item in first)

    emitted = generate_digital_twin_v0_1(
        request(
            emission=DigitalTwinEmissionKind.SURROGATE,
            surrogate_index=1,
        )
    )
    assert emitted.truth.emission_kind is DigitalTwinEmissionKind.SURROGATE
    assert emitted.truth.emitted_pattern_id == emitted.patterns[2].pattern_id


def test_truth_covers_burst_quadratic_doppler_lnb_offsets_and_missing_frames() -> None:
    bundle = generate_digital_twin_v0_1(request())

    assert bundle.truth.pilot_present is True
    assert bundle.truth.pilot_present_frame_count == 2
    assert [point.pilot_present for point in bundle.truth.receiver_truth[0].path] == [
        True,
        False,
        False,
        False,
        True,
        False,
        False,
        False,
    ]
    first_path = bundle.truth.receiver_truth[0].path
    second_path = bundle.truth.receiver_truth[1].path
    assert all(
        first.frequency_offset_hz - second.frequency_offset_hz == pytest.approx(3_000.0)
        for first, second in zip(first_path, second_path, strict=True)
    )
    point = first_path[6]
    expected = (
        bundle.scenario.cfo_hz
        + 1_000.0
        + bundle.scenario.drift_rate_hz_s * point.midpoint_seconds
        + 0.5 * bundle.scenario.drift_acceleration_hz_s2 * point.midpoint_seconds**2
    )
    assert point.frequency_offset_hz == pytest.approx(expected)

    observation = bundle.observations[0]
    missing = slice(
        3 * observation.frame_sample_count, 4 * observation.frame_sample_count
    )
    assert set(observation.i_samples[missing]) == {0.0}
    assert set(observation.q_samples[missing]) == {0.0}


def test_linear_and_quadratic_paths_and_optional_single_radio_view() -> None:
    original = request()
    shared_radio_receivers = (
        original.receivers[0],
        replace(original.receivers[1], radio_id=original.receivers[0].radio_id),
    )
    linear = generate_digital_twin_v0_1(
        replace(
            original,
            drift_rate_hz_s=DigitalTwinFloatRangeV0_1(2_400.0, 2_400.0),
            drift_acceleration_hz_s2=DigitalTwinFloatRangeV0_1(0.0, 0.0),
            receivers=shared_radio_receivers,
        )
    )
    quadratic = generate_digital_twin_v0_1(
        replace(
            original,
            drift_rate_hz_s=DigitalTwinFloatRangeV0_1(2_400.0, 2_400.0),
            drift_acceleration_hz_s2=DigitalTwinFloatRangeV0_1(600.0, 600.0),
            receivers=shared_radio_receivers,
        )
    )

    assert len({item.radio_id for item in linear.observations}) == 1
    linear_path = linear.truth.receiver_truth[0].path
    quadratic_path = quadratic.truth.receiver_truth[0].path
    linear_steps = [
        later.frequency_offset_hz - earlier.frequency_offset_hz
        for earlier, later in pairwise(linear_path)
    ]
    quadratic_steps = [
        later.frequency_offset_hz - earlier.frequency_offset_hz
        for earlier, later in pairwise(quadratic_path)
    ]
    assert max(linear_steps) - min(linear_steps) < 1e-9
    assert quadratic_steps[-1] > quadratic_steps[0]


def test_null_and_impaired_views_preserve_two_radio_identity_without_false_truth() -> (
    None
):
    base = request(emission=DigitalTwinEmissionKind.NULL)
    clean = replace(
        base,
        impairments=DigitalTwinImpairmentConfigV0_1(0.0, 0.0, 1, (), (), None),
    )
    clean_bundle = generate_digital_twin_v0_1(clean)
    impaired_bundle = generate_digital_twin_v0_1(base)

    assert clean_bundle.truth.pilot_present is False
    assert clean_bundle.truth.pilot_present_frame_count == 0
    assert {item.radio_id for item in clean_bundle.observations} == {
        RadioId("radio_twin_20"),
        RadioId("radio_twin_21"),
    }
    assert all(
        sample == 0
        for observation in clean_bundle.observations
        for sample in (*observation.i_samples, *observation.q_samples)
    )
    assert any(
        sample != 0
        for observation in impaired_bundle.observations
        for sample in (*observation.i_samples, *observation.q_samples)
    )


@pytest.mark.parametrize(
    "impairment", ["awgn", "stationary", "narrowband", "broadband"]
)
def test_each_noise_and_interference_path_is_deterministic_and_observable(
    impairment: str,
) -> None:
    original = request(emission=DigitalTwinEmissionKind.NULL)
    empty = DigitalTwinImpairmentConfigV0_1(0.0, 0.0, 1, (), (), None)
    configured = {
        "awgn": replace(empty, awgn_standard_deviation=0.1),
        "stationary": replace(
            empty,
            stationary_tones=(DigitalTwinToneInterferenceV0_1(300.0, 0.2, 0.0, 0.0),),
        ),
        "narrowband": replace(
            empty,
            narrowband_interferers=(
                DigitalTwinToneInterferenceV0_1(-400.0, 0.2, 30.0, 0.0),
            ),
        ),
        "broadband": replace(
            empty,
            broadband=DigitalTwinBroadbandInterferenceV0_1(
                0.1, DigitalTwinBurstScheduleV0_1(2, 1, 0)
            ),
        ),
    }[impairment]

    first = generate_digital_twin_v0_1(replace(original, impairments=configured))
    second = generate_digital_twin_v0_1(replace(original, impairments=configured))

    assert first.observations == second.observations
    assert any(
        value != 0
        for observation in first.observations
        for value in (*observation.i_samples, *observation.q_samples)
    )


def test_gain_variation_changes_non_null_frame_amplitudes() -> None:
    original = request()
    quiet = DigitalTwinImpairmentConfigV0_1(0.0, 0.0, 4, (), (), None)
    varying = replace(quiet, gain_variation_fraction=0.25)

    constant = generate_digital_twin_v0_1(replace(original, impairments=quiet))
    varied = generate_digital_twin_v0_1(replace(original, impairments=varying))

    assert constant.observations[1].sample_values_digest != (
        varied.observations[1].sample_values_digest
    )


class DopplerAnalyzer:
    def __init__(self) -> None:
        self.inputs: list[DigitalTwinAnalyzerInputV0_1] = []

    def analyze_doppler(
        self, analyzer_input: DigitalTwinAnalyzerInputV0_1
    ) -> DigitalTwinDopplerAnalyzerOutputV0_1:
        self.inputs.append(analyzer_input)
        observation = analyzer_input.observation
        candidate = DigitalTwinDopplerCandidateV0_1(
            1,
            2_500.0,
            100.0,
            observation.frame_count / 750.0,
            8.0,
            tuple(
                DigitalTwinCandidatePathPointV0_1(frame, 1_000.0 + frame * 2.0)
                for frame in range(observation.frame_count)
            ),
        )
        return DigitalTwinDopplerAnalyzerOutputV0_1(
            analyzer_input.scenario_request_digest,
            observation.radio_id,
            observation.receiver_chain_id,
            True,
            (candidate,),
            (
                DigitalTwinAnalyzerStatisticV0_1(
                    "blind-doppler",
                    DigitalTwinStatisticKind.DRIFT_RATE_HZ_S,
                    2_500.0,
                ),
                DigitalTwinAnalyzerStatisticV0_1(
                    "blind-doppler",
                    DigitalTwinStatisticKind.SPECTRAL_PEAK_EXCESS,
                    8.0,
                ),
            ),
        )


class PilotAnalyzer:
    def analyze_pilot(
        self, analyzer_input: DigitalTwinAnalyzerInputV0_1
    ) -> DigitalTwinPilotAnalyzerOutputV0_1:
        observation = analyzer_input.observation
        return DigitalTwinPilotAnalyzerOutputV0_1(
            analyzer_input.scenario_request_digest,
            observation.radio_id,
            observation.receiver_chain_id,
            True,
            None,
            (
                DigitalTwinAnalyzerStatisticV0_1(
                    "anchor-8", DigitalTwinStatisticKind.CANDIDATE_SCORE, 0.7
                ),
                DigitalTwinAnalyzerStatisticV0_1(
                    "anchor-8",
                    DigitalTwinStatisticKind.CONDITIONED_CONTROL_SCORE,
                    0.2,
                ),
                DigitalTwinAnalyzerStatisticV0_1(
                    "anchor-8",
                    DigitalTwinStatisticKind.CANDIDATE_CONTROL_MARGIN,
                    0.5,
                ),
            ),
        )


def test_injected_analyzers_receive_observations_not_truth_and_stay_candidate_only() -> (
    None
):
    bundle = generate_digital_twin_v0_1(request())
    doppler = DopplerAnalyzer()

    analysis = analyze_digital_twin_v0_1(
        bundle, doppler_analyzer=doppler, pilot_analyzer=PilotAnalyzer()
    )

    assert len(doppler.inputs) == 2
    assert not hasattr(doppler.inputs[0], "truth")
    assert analysis.candidate_only is True
    assert analysis.calibrated_detection_count is None
    assert all(item.candidate_only for item in analysis.doppler)
    assert all(item.calibrated_detection_count is None for item in analysis.pilot)


def test_dashboard_ready_comparison_reports_distributions_without_calibration() -> None:
    bundle = generate_digital_twin_v0_1(request())
    analysis = analyze_digital_twin_v0_1(
        bundle, doppler_analyzer=DopplerAnalyzer(), pilot_analyzer=PilotAnalyzer()
    )
    real = DigitalTwinRealDataSummaryV0_1(
        SchemaRef(DigitalTwinRealDataSummaryV0_1.SCHEMA_ID, V0_1),
        digest("real-window-summary"),
        "last-24-hours",
        True,
        None,
        (
            DigitalTwinStatisticDistributionV0_1(
                "anchor-8", DigitalTwinStatisticKind.CANDIDATE_SCORE, (0.6, 0.8)
            ),
            DigitalTwinStatisticDistributionV0_1(
                "anchor-8",
                DigitalTwinStatisticKind.CONDITIONED_CONTROL_SCORE,
                (0.1, 0.3),
            ),
            DigitalTwinStatisticDistributionV0_1(
                "anchor-8",
                DigitalTwinStatisticKind.CANDIDATE_CONTROL_MARGIN,
                (0.4, 0.6),
            ),
            DigitalTwinStatisticDistributionV0_1(
                "real-only", DigitalTwinStatisticKind.CANDIDATE_SCORE, (0.1,)
            ),
        ),
        ("real-summary-is-candidate-only",),
    )

    comparison = compare_digital_twin_to_real_v0_1((analysis,), real)

    assert comparison.candidate_only is True
    assert comparison.calibrated_detection_count is None
    assert len(comparison.comparisons) == 3
    candidate = next(
        item
        for item in comparison.comparisons
        if item.statistic is DigitalTwinStatisticKind.CANDIDATE_SCORE
    )
    assert candidate.twin.count == 2
    assert candidate.twin.mean == pytest.approx(0.7)
    assert candidate.real.mean == pytest.approx(0.7)
    assert 0 <= candidate.empirical_ks_distance <= 1
    assert "blind-doppler:drift-rate-hz-s" in comparison.twin_only_statistics
    assert "real-only:candidate-score" in comparison.real_only_statistics
    assert comparison.REQUIRED_WARNING in comparison.warnings


def test_canonical_codec_round_trips_and_rejects_noncanonical_or_oversized_json() -> (
    None
):
    bundle = generate_digital_twin_v0_1(request())
    encoded = encode_digital_twin_bundle_v0_1(bundle)

    assert decode_digital_twin_bundle_v0_1(encoded) == bundle
    noncanonical = json.dumps(json.loads(encoded), indent=2).encode()
    with pytest.raises(ValueError, match="not canonical"):
        decode_digital_twin_bundle_v0_1(noncanonical)
    with pytest.raises(ValueError, match="JSON byte bound"):
        decode_digital_twin_bundle_v0_1(b" " * (MAX_TWIN_JSON_BYTES + 1))


def test_contracts_fail_closed_on_resource_and_provenance_mismatch() -> None:
    original = request()
    with pytest.raises(ValueError, match="two to four receiver"):
        replace(original, receivers=original.receivers[:1])
    with pytest.raises(ValueError, match="exact Qin sample digest"):
        replace(
            original,
            provenance=replace(original.provenance, input_digests=(digest("wrong"),)),
        )
    with pytest.raises(ValueError, match="strictly positive"):
        replace(original, amplitude=DigitalTwinFloatRangeV0_1(0.0, 1.0))
    with pytest.raises(ValueError, match="frame count is outside"):
        replace(original, frame_count=513)
    large_pattern = make_qin_pattern_v0_1(
        pattern_id="dtpattern_qin_exact_large",
        source_template_ref=original.exact_qin_pattern.source_template_ref,
        edge=StarlinkEdge.LOWER,
        sample_rate_hz=768_750.0,
        samples=(1 + 0j,) * 1_025,
    )
    large_provenance = replace(
        original.provenance,
        input_digests=(large_pattern.sample_values_digest,),
    )
    with pytest.raises(ValueError, match="sample count exceeds"):
        replace(
            original,
            exact_qin_pattern=large_pattern,
            sample_rate_hz=768_750.0,
            frame_count=512,
            provenance=large_provenance,
        )


def test_component_has_no_reference_runtime_or_live_system_dependencies() -> None:
    root = Path(__file__).resolve().parents[2]
    files = (
        root / "src/leo_flow/contracts/digital_twin.py",
        root / "src/leo_flow/analysis/digital_twin.py",
    )
    forbidden = (
        "leo_tracker",
        "psycopg",
        "sqlalchemy",
        "leo_flow.storage",
        "leo_flow.services",
        "leo_flow.deployments",
        "socket",
        "subprocess",
    )
    for path in files:
        source = path.read_text(encoding="utf-8")
        modules = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
        assert not any(
            token in module.casefold() for module in modules for token in forbidden
        ), path
