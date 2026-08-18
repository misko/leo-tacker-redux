from __future__ import annotations

import json
import os
from dataclasses import fields, replace
from pathlib import Path

import numpy as np
import pytest

from leo_flow.analysis.recording.starlink_acquisition import (
    DEFAULT_ACQUIRE_SYMBOLS,
    normalized_frame_score_v0_3,
)
from leo_flow.analysis.recording.starlink_pattern_symmetric_qam import (
    known_pattern_qam_quality_v0_5,
)
from leo_flow.analysis.recording.starlink_receiver_agnostic_cfo import (
    ReceiverAgnosticCfoQamAnalyzerV0_6,
    ReceiverAgnosticCfoRawIqScorerV0_6,
)
from leo_flow.analysis.recording.starlink_surrogate_null import (
    precommitted_surrogate_codebook_v0_1,
    qin_exact_search_pattern_v0_1,
)
from leo_flow.analysis.recording.starlink_templates import (
    qin_edge_pilot_states_v1,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SegmentId,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_adaptive_calibration import AdaptivePatternRole
from leo_flow.contracts.starlink_receiver_agnostic_cfo import (
    ReceiverAgnosticCfoPatternV0_6,
    ReceiverAgnosticCfoSearchPlanV0_6,
    ReceiverAgnosticCfoWindowV0_6,
)

from .fakes import execution_context

FIXTURES = Path(__file__).parent / "fixtures"


def _plan() -> ReceiverAgnosticCfoSearchPlanV0_6:
    return replace(
        ReceiverAgnosticCfoSearchPlanV0_6(),
        coarse_cfo_step_hz=350_000.0,
        local_cfo_radius_hz=350_000.0,
        local_cfo_step_hz=350_000.0,
        basins_per_pattern=1,
        basin_cfo_separation_hz=350_000.0,
    )


def _window(sample_count: int = 7_500) -> ReceiverAgnosticCfoWindowV0_6:
    return ReceiverAgnosticCfoWindowV0_6(
        RecordingId("rec_cfo_qam_test"),
        Digest.sha256(b"recording-identity"),
        RadioId("radio_cfo_qam_test"),
        SegmentId("seg_cfo_qam_test"),
        ReceiverChainId("rx_cfo_qam_test"),
        StarlinkEdge.LOWER,
        2_500_000.0,
        10_000,
        10_000 + sample_count,
        ArtifactRef("source-recording", Digest.sha256(b"recording-object")),
        ArtifactRef("source-window", Digest.sha256(b"window-object")),
    )


@pytest.mark.parametrize("cfo_hz", (-700_000.0, 0.0, 700_000.0))
def test_public_raw_iq_adapter_is_exact_v03_normalized_numerics(cfo_hz: float) -> None:
    rate = 2_500_000.0
    rng = np.random.default_rng(20260818)
    samples = (
        rng.standard_normal(7_500) + 1j * rng.standard_normal(7_500)
    ).astype(np.complex128)
    template = qin_exact_search_pattern_v0_1(rate, StarlinkEdge.LOWER)
    declared = ReceiverAgnosticCfoPatternV0_6(
        0, AdaptivePatternRole.QIN, template.identity.template_ref.digest
    )
    scorer = ReceiverAgnosticCfoRawIqScorerV0_6(samples, rate, (template,))

    expected, _ = normalized_frame_score_v0_3(
        samples,
        np.asarray(template.samples),
        rate,
        63,
        cfo_hz,
        DEFAULT_ACQUIRE_SYMBOLS,
    )
    assert scorer.score(declared, 63, cfo_hz) == expected


def test_wrapper_closes_over_exact_window_receiver_artifacts_and_pattern_qam() -> None:
    window = _window()
    result = ReceiverAgnosticCfoQamAnalyzerV0_6(_plan()).analyze(
        np.zeros(window.sample_count, dtype=np.complex128),
        window,
        pattern_count=2,
        execution=execution_context(),
    )

    assert result.window is window
    assert result.provenance.input_digests == (
        window.recording_identity_digest,
        window.source_recording_ref.digest,
        window.source_window_ref.digest,
    )
    assert len(result.pattern_qam) == 2
    assert all(item.complete_frame_count == 2 for item in result.pattern_qam)
    assert tuple(item.winner for item in result.pattern_qam) == (
        result.search_receipt.winners
    )
    assert tuple(item.template_ref.digest for item in result.pattern_qam) == tuple(
        item.template_digest for item in result.search_receipt.patterns
    )
    assert result.ref.digest == result.digest
    assert result.calibrated_detection_count is None
    public_names = {item.name for item in fields(ReceiverAgnosticCfoWindowV0_6)}
    assert not any(
        token in name
        for name in public_names
        for token in ("lnb", "center", "correction", "profile")
    )


def test_raw_scorer_is_equivariant_to_surrogate_label_permutation() -> None:
    rate = 2_500_000.0
    rng = np.random.default_rng(6)
    samples = (
        rng.standard_normal(7_500) + 1j * rng.standard_normal(7_500)
    ).astype(np.complex128)
    qin = qin_exact_search_pattern_v0_1(rate, StarlinkEdge.LOWER)
    surrogates = precommitted_surrogate_codebook_v0_1(
        rate, StarlinkEdge.LOWER, count=2
    )
    scorer = ReceiverAgnosticCfoRawIqScorerV0_6(
        samples, rate, (qin, *surrogates)
    )
    left = ReceiverAgnosticCfoPatternV0_6(
        1, AdaptivePatternRole.SURROGATE, surrogates[0].identity.template_ref.digest
    )
    right = ReceiverAgnosticCfoPatternV0_6(
        1, AdaptivePatternRole.SURROGATE, surrogates[1].identity.template_ref.digest
    )
    before = {
        item.template_digest: scorer.score(item, 31, 700_000.0)
        for item in (left, right)
    }
    after = {
        item.template_digest: scorer.score(item, 31, 700_000.0)
        for item in (right, left)
    }
    assert after == before


def test_raw_iq_resource_bounds_fail_before_search() -> None:
    analyzer = ReceiverAgnosticCfoQamAnalyzerV0_6(
        _plan(), maximum_window_samples=100
    )
    with pytest.raises(ValueError, match="sample bound"):
        analyzer.analyze(
            np.zeros(101, dtype=np.complex128),
            _window(101),
            pattern_count=1,
            execution=execution_context(),
        )
    with pytest.raises(ValueError, match="pattern count"):
        ReceiverAgnosticCfoQamAnalyzerV0_6(_plan()).analyze(
            np.zeros(7_500, dtype=np.complex128),
            _window(),
            pattern_count=10,
            execution=execution_context(),
        )


def _ci16_window(
    path: Path,
    *,
    sample_count: int,
    receiver_count: int,
    start_sample: int,
    window_sample_count: int,
    receiver_index: int,
) -> np.ndarray:
    raw = np.memmap(
        path,
        dtype="<i2",
        mode="r",
        shape=(sample_count, receiver_count, 2),
    )
    values = raw[
        start_sample : start_sample + window_sample_count, receiver_index
    ]
    return np.asarray(
        (values[:, 0].astype(np.float32) + 1j * values[:, 1]) / 32768.0,
        dtype=np.complex128,
    )


def _assert_conditioned_qin_case(samples: np.ndarray, case: dict[str, object]) -> None:
    rate = 2_500_000.0
    edge = StarlinkEdge.LOWER
    template = qin_exact_search_pattern_v0_1(rate, edge)
    declared = ReceiverAgnosticCfoPatternV0_6(
        0, AdaptivePatternRole.QIN, template.identity.template_ref.digest
    )
    score = ReceiverAgnosticCfoRawIqScorerV0_6(
        samples, rate, (template,)
    ).score(declared, int(case["epoch_sample"]), float(case["cfo_hz"]))
    accuracy, evm, support = known_pattern_qam_quality_v0_5(
        samples,
        rate,
        edge,
        qin_edge_pilot_states_v1(edge),
        int(case["epoch_sample"]),
        float(case["cfo_hz"]),
    )
    assert score == pytest.approx(float(case["normalized_score_v0_3"]), abs=1e-12)
    assert accuracy == pytest.approx(
        float(case["hard_symbol_accuracy_v0_5"]), abs=1 / 2_400
    )
    assert evm == pytest.approx(float(case["rms_evm_v0_5"]), abs=1e-9)
    assert support == int(case["complete_frame_count"])


@pytest.mark.integration
def test_conditioned_retro_receivers_retain_v03_score_and_v05_qam() -> None:
    manifest = json.loads(
        (FIXTURES / "retro_qam_2026_08_17_v1.json").read_text(encoding="utf-8")
    )
    root = Path(manifest["archive"]["root"])
    path = root / manifest["iq_object"]["relative_path"]
    if not path.is_file():
        pytest.skip("read-only RETRO raw-IQ corpus is not mounted")
    expected_v05 = (
        (0.35436004461680004, 0.7433333333333333, 0.9452744753673185),
        (0.3797337195629796, 0.7891666666666667, 0.8041178439605544),
    )
    source = manifest["iq_object"]
    window = manifest["selected_window"]
    for case, (score, accuracy, evm) in zip(
        manifest["historical_conditioned_expectations"], expected_v05, strict=True
    ):
        samples = _ci16_window(
            path,
            sample_count=source["sample_count"],
            receiver_count=manifest["format"]["receiver_count"],
            start_sample=window["sample_offset"],
            window_sample_count=window["sample_count"],
            receiver_index=case["receiver_index"],
        )
        _assert_conditioned_qin_case(
            samples,
            {
                **case,
                "epoch_sample": case["winning_epoch_sample"],
                "cfo_hz": case["winning_cfo_hz"],
                "normalized_score_v0_3": score,
                "hard_symbol_accuracy_v0_5": accuracy,
                "rms_evm_v0_5": evm,
            },
        )


@pytest.mark.integration
def test_conditioned_j1_early_and_late_receivers_retain_numerics() -> None:
    manifest = json.loads(
        (FIXTURES / "j1_receiver_agnostic_cfo_qam_v0_6.json").read_text(
            encoding="utf-8"
        )
    )
    configured = os.environ.get("LEO_FLOW_J1_IQ_PATH")
    if configured is None or not Path(configured).is_file():
        pytest.skip("set LEO_FLOW_J1_IQ_PATH to the immutable J1 raw-IQ object")
    assert manifest["scope"] == {
        "conditioned_numerical_canary_only": True,
        "calibration_member": False,
        "detection_claim": False,
    }
    for case in manifest["cases"]:
        samples = _ci16_window(
            Path(configured),
            sample_count=manifest["sample_count"],
            receiver_count=manifest["receiver_count"],
            start_sample=case["start_sample"],
            window_sample_count=manifest["window_sample_count"],
            receiver_index=case["receiver_index"],
        )
        _assert_conditioned_qin_case(samples, case)
