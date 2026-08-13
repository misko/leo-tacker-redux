from __future__ import annotations

import ast
import json
import math
import struct
from dataclasses import replace
from pathlib import Path

import pytest

from benchmark.synthetic_iq import generate_case
from leo_flow.analysis.recording import (
    AnalysisConfigurationError,
    AnalysisInputError,
    QualityPsdAnalyzer,
    QualityPsdConfig,
    quality_psd_algorithm_ref,
)
from leo_flow.analysis.recording.psd import compact_psd, radix2_fft
from leo_flow.contracts.core import ArtifactRef, Digest, RecordingId, SchemaRef
from leo_flow.contracts.features import FeatureSetBundle

from .fakes import SegmentFixture, execution_context, make_request, make_view

ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC_SPEC = ROOT / "benchmark/specs/synthetic-iq-v1.json"


def synthetic_cases() -> dict[str, dict[str, object]]:
    value = json.loads(SYNTHETIC_SPEC.read_text(encoding="utf-8"))
    return {case["case_id"]: case for case in value["cases"]}


def fixture(case_id: str) -> tuple[dict[str, object], SegmentFixture]:
    case = synthetic_cases()[case_id]
    data, truth = generate_case(case)
    assert truth == case["expected_truth"]
    return case, SegmentFixture(data, int(case["sample_rate_hz"]))


def diagnostics(observation: object) -> dict[str, object]:
    return dict(observation.diagnostics)


def analyzer_and_request(
    segment: SegmentFixture,
    *,
    config: QualityPsdConfig | None = None,
    read_chunk_samples: int = 257,
) -> tuple[QualityPsdAnalyzer, object, object]:
    config = config or QualityPsdConfig(psd_window_samples=256, psd_stride_samples=512)
    view, recording_ref = make_view(segment)
    analyzer = QualityPsdAnalyzer(
        config, execution_context(), read_chunk_samples=read_chunk_samples
    )
    return analyzer, view, make_request(recording_ref, config)


def test_clean_synthetic_frequency_is_recovered_within_one_bin() -> None:
    case, segment = fixture("02-clean-chirp-two-receiver")
    analyzer, view, request = analyzer_and_request(segment)
    bundle = analyzer.analyze(view, request)
    psd = [item for item in bundle.observations if item.method_id == "compact-psd"]
    assert psd
    expected = float(case["signal_truth"]["frequency_start_hz"])
    bin_width = segment.sample_rate_hz / 256
    assert all(abs(item.frequency_offset_hz - expected) <= bin_width for item in psd)
    assert all(item.snr_db > 10.0 for item in psd)


def test_quality_counts_rms_dc_and_clipping_match_independent_ci16_math() -> None:
    _, segment = fixture("01-clipped-chirp-two-receiver")
    config = QualityPsdConfig(psd_window_samples=256, psd_stride_samples=4096)
    analyzer, view, request = analyzer_and_request(
        segment, config=config, read_chunk_samples=113
    )
    bundle = analyzer.analyze(view, request)
    quality = [
        item for item in bundle.observations if item.method_id == "sample-quality"
    ]
    unpacked = list(struct.iter_unpack("<hhhh", segment.data))
    for receiver_index, observation in enumerate(quality):
        pairs = [
            (row[receiver_index * 2], row[receiver_index * 2 + 1]) for row in unpacked
        ]
        rms = math.sqrt(sum(i * i + q * q for i, q in pairs) / len(pairs))
        dc_i = sum(i for i, _ in pairs) / len(pairs)
        dc_q = sum(q for _, q in pairs) / len(pairs)
        clipped = sum(
            abs(value) >= config.clip_threshold_abs for pair in pairs for value in pair
        )
        details = diagnostics(observation)
        assert details["sample_count"] == len(pairs)
        assert details["component_count"] == len(pairs) * 2
        assert details["rms_magnitude_counts"] == pytest.approx(rms, abs=1e-12)
        assert details["dc_i_counts"] == pytest.approx(dc_i, abs=1e-12)
        assert details["dc_q_counts"] == pytest.approx(dc_q, abs=1e-12)
        assert details["clipped_component_count"] == clipped
        assert "clipping_detected" in observation.quality_flags


def test_io_chunk_size_is_not_scientific_and_results_are_bitwise_invariant() -> None:
    _, segment = fixture("02-clean-chirp-two-receiver")
    config = QualityPsdConfig(psd_window_samples=256, psd_stride_samples=512)
    view_a, recording_ref = make_view(segment)
    view_b, _ = make_view(segment)
    request = make_request(recording_ref, config)
    a = QualityPsdAnalyzer(config, execution_context(), read_chunk_samples=1)
    b = QualityPsdAnalyzer(config, execution_context(), read_chunk_samples=1000)
    assert a.analyze(view_a, request) == b.analyze(view_b, request)


def test_reads_are_bounded_and_never_cross_segment_or_window_bounds() -> None:
    _, segment = fixture("02-clean-chirp-two-receiver")
    analyzer, view, request = analyzer_and_request(segment, read_chunk_samples=127)
    bundle = analyzer.analyze(view, request)
    sample_count = len(segment.data) // 8
    assert view.calls
    assert all(0 <= start < stop <= sample_count for _, start, stop in view.calls)
    assert all(
        stop - start <= analyzer.maximum_read_samples for _, start, stop in view.calls
    )
    for score in bundle.method_scores:
        assert 0 <= score.window_start_sample < score.window_stop_sample <= sample_count


def test_method_score_windows_are_aligned_across_receivers() -> None:
    _, segment = fixture("02-clean-chirp-two-receiver")
    analyzer, view, request = analyzer_and_request(segment)
    bundle = analyzer.analyze(view, request)
    by_receiver: dict[str, list[tuple[int, int]]] = {}
    for score in bundle.method_scores:
        by_receiver.setdefault(score.receiver_key, []).append(
            (score.window_start_sample, score.window_stop_sample)
        )
    assert set(by_receiver) == {"rx_0", "rx_1"}
    assert by_receiver["rx_0"] == by_receiver["rx_1"]
    assert by_receiver["rx_0"][-1][1] == len(segment.data) // 8


def test_multi_segment_results_keep_exact_segment_bounds() -> None:
    _, clean = fixture("02-clean-chirp-two-receiver")
    _, weak = fixture("03-weak-static-two-receiver")
    config = QualityPsdConfig(psd_window_samples=256, psd_stride_samples=1024)
    view, recording_ref = make_view(clean, weak)
    bundle = QualityPsdAnalyzer(
        config, execution_context(), read_chunk_samples=333
    ).analyze(view, make_request(recording_ref, config))
    counts = {
        segment.segment_id: segment.sample_count for segment in view.manifest.segments
    }
    assert {item.segment_id for item in bundle.observations} == set(counts)
    assert all(
        item.window_stop_sample <= counts[item.segment_id]
        for item in bundle.observations
    )


def test_short_segment_has_quality_but_explicitly_no_psd() -> None:
    data = b"".join(struct.pack("<hhhh", n, -n, 2 * n, -2 * n) for n in range(16))
    config = QualityPsdConfig(psd_window_samples=32, psd_stride_samples=32)
    view, recording_ref = make_view(SegmentFixture(data, 1_000_000))
    bundle = QualityPsdAnalyzer(config, execution_context()).analyze(
        view, make_request(recording_ref, config)
    )
    assert len(bundle.observations) == 2
    assert not bundle.method_scores
    assert "seg_00:segment-too-short-for-psd" in bundle.warnings
    assert "psd-skipped-short-segment" in bundle.reason_codes


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"psd_window_samples": 12}, "power of two"),
        ({"psd_window_samples": True}, "power of two"),
        ({"psd_stride_samples": 0}, "stride"),
        ({"psd_stride_samples": 1.5}, "stride"),
        ({"clip_threshold_abs": 0}, "clip_threshold"),
        ({"clip_threshold_abs": True}, "clip_threshold"),
        ({"dc_warning_fraction": float("nan")}, "dc_warning"),
        ({"noise_floor_epsilon": float("inf")}, "noise_floor"),
    ],
)
def test_configuration_rejects_invalid_numeric_values(
    values: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        QualityPsdConfig(**values)


@pytest.mark.parametrize("mutable", [False, True])
def test_truncated_or_mutable_reader_output_fails_explicitly(mutable: bool) -> None:
    _, segment = fixture("02-clean-chirp-two-receiver")
    config = QualityPsdConfig(psd_window_samples=256, psd_stride_samples=512)
    view, recording_ref = make_view(segment)
    view.truncate_reads = not mutable
    view.mutable_result = mutable
    analyzer = QualityPsdAnalyzer(config, execution_context())
    with pytest.raises(AnalysisInputError, match="expected|immutable bytes"):
        analyzer.analyze(view, make_request(recording_ref, config))


def test_request_identity_and_configuration_are_checked_before_iq_read() -> None:
    _, segment = fixture("02-clean-chirp-two-receiver")
    config = QualityPsdConfig(psd_window_samples=256, psd_stride_samples=512)
    analyzer, view, request = analyzer_and_request(segment, config=config)
    wrong_config = replace(
        request,
        config_ref=ArtifactRef(
            request.config_ref.artifact_id,
            Digest.sha256(b"wrong"),
            request.config_ref.schema,
        ),
    )
    with pytest.raises(AnalysisConfigurationError, match="config_ref"):
        analyzer.analyze(view, wrong_config)
    wrong_algorithm = replace(
        request,
        algorithm_ref=ArtifactRef(
            quality_psd_algorithm_ref().artifact_id,
            Digest.sha256(b"wrong-algorithm"),
            quality_psd_algorithm_ref().schema,
        ),
    )
    with pytest.raises(AnalysisConfigurationError, match="algorithm_ref"):
        analyzer.analyze(view, wrong_algorithm)
    assert not view.calls


def test_recording_id_mismatch_fails_before_iq_read() -> None:
    _, segment = fixture("02-clean-chirp-two-receiver")
    config = QualityPsdConfig(psd_window_samples=256, psd_stride_samples=512)
    view, recording_ref = make_view(segment, recording_id=RecordingId("rec_view"))
    foreign_ref = replace(recording_ref, recording_id=RecordingId("rec_foreign"))
    request = make_request(foreign_ref, config)
    with pytest.raises(AnalysisInputError, match="IDs differ"):
        QualityPsdAnalyzer(config, execution_context()).analyze(view, request)
    assert not view.calls


def test_explicit_provenance_closes_over_algorithm_config_input_and_dependencies() -> (
    None
):
    _, segment = fixture("02-clean-chirp-two-receiver")
    config = QualityPsdConfig(psd_window_samples=256, psd_stride_samples=512)
    view, recording_ref = make_view(segment)
    alpha = ArtifactRef("alpha", Digest.sha256(b"alpha"), SchemaRef("fixture-alpha"))
    beta = ArtifactRef("beta", Digest.sha256(b"beta"), SchemaRef("fixture-beta"))
    request = make_request(recording_ref, config, dependencies=(beta, alpha))
    bundle = QualityPsdAnalyzer(config, execution_context()).analyze(view, request)
    assert bundle.input_recording_identity_digest == recording_ref.identity_digest()
    assert bundle.provenance.input_digests == (recording_ref.identity_digest(),)
    assert bundle.provenance.normalized_config_digest == request.config_ref.digest
    assert bundle.provenance.dependency_digests == (
        request.algorithm_ref.digest,
        alpha.digest,
        beta.digest,
    )


def test_dependency_order_does_not_change_scientific_identity_or_result() -> None:
    _, segment = fixture("02-clean-chirp-two-receiver")
    config = QualityPsdConfig(psd_window_samples=256, psd_stride_samples=1024)
    alpha = ArtifactRef("alpha", Digest.sha256(b"alpha"))
    beta = ArtifactRef("beta", Digest.sha256(b"beta"))
    view_a, recording_ref = make_view(segment)
    view_b, _ = make_view(segment)
    analyzer = QualityPsdAnalyzer(config, execution_context())
    first = analyzer.analyze(
        view_a, make_request(recording_ref, config, dependencies=(alpha, beta))
    )
    second = analyzer.analyze(
        view_b, make_request(recording_ref, config, dependencies=(beta, alpha))
    )
    assert first == second


def test_duplicate_dependencies_are_rejected() -> None:
    _, segment = fixture("02-clean-chirp-two-receiver")
    config = QualityPsdConfig(psd_window_samples=256, psd_stride_samples=1024)
    dependency = ArtifactRef("duplicate", Digest.sha256(b"same"))
    view, recording_ref = make_view(segment)
    request = make_request(recording_ref, config, dependencies=(dependency, dependency))
    with pytest.raises(AnalysisConfigurationError, match="duplicates"):
        QualityPsdAnalyzer(config, execution_context()).analyze(view, request)


def test_fft_matches_small_direct_dft_and_psd_tie_break_is_deterministic() -> None:
    values = [complex(index, -index / 2) for index in range(8)]
    observed = radix2_fft(values)
    expected = [
        sum(
            value
            * complex(
                math.cos(-2 * math.pi * k * n / 8), math.sin(-2 * math.pi * k * n / 8)
            )
            for n, value in enumerate(values)
        )
        for k in range(8)
    ]
    assert observed == pytest.approx(expected, abs=1e-12)
    zero = compact_psd([0j] * 8, sample_rate_hz=8.0, noise_floor_epsilon=1e-12)
    assert zero.peak_bin == 0
    assert zero.peak_to_median_ratio == 0.0


def test_bundle_is_contract_valid_and_deterministic() -> None:
    _, segment = fixture("03-weak-static-two-receiver")
    analyzer, view_a, request = analyzer_and_request(segment)
    view_b, _ = make_view(segment)
    first = analyzer.analyze(view_a, request)
    second = analyzer.analyze(view_b, request)
    assert isinstance(first, FeatureSetBundle)
    assert first == second
    assert len({item.feature_id for item in first.observations}) == len(
        first.observations
    )


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_production_analyzer_has_only_one_recording_and_no_external_capabilities() -> (
    None
):
    source = ROOT / "src/leo_flow/analysis/recording"
    forbidden_prefixes = (
        "asyncio",
        "http",
        "requests",
        "socket",
        "subprocess",
        "urllib",
        "leo_tracker",
        "leo_flow.capture",
        "leo_flow.dashboard",
        "leo_flow.analysis.ephemeris",
        "leo_flow.analysis.model",
        "leo_flow.jobs",
        "psycopg",
        "sqlalchemy",
    )
    for path in source.rglob("*.py"):
        modules = imported_modules(path)
        assert not any(
            module == prefix or module.startswith(prefix + ".")
            for module in modules
            for prefix in forbidden_prefixes
        ), path
