from __future__ import annotations

import hashlib
import json
import math
import os
import random
import resource
import time
from pathlib import Path

import numpy as np
import pytest

from leo_flow.analysis.recording.starlink_acquisition import (
    StarlinkAcquisitionConfigV0_3,
    StarlinkAcquisitionV0_3,
)
from leo_flow.analysis.recording.starlink_detector_suite import (
    StarlinkDetectorSuiteConfigV0_2,
    StarlinkDetectorSuiteV0_2,
    StarlinkInjectionCaseV0_2,
    synthesize_starlink_injection_v0_2,
)
from leo_flow.analysis.recording.starlink_pilot_constellation import (
    StarlinkPilotConstellationAnalyzerV0_1,
    StarlinkPilotConstellationConfigV0_1,
)
from leo_flow.analysis.recording.starlink_templates import (
    qin_edge_pilot_template_pair_v0_1,
)
from leo_flow.contracts.core import (
    Digest,
    ReceiverChainId,
    RecordingId,
    SegmentId,
)
from leo_flow.contracts.starlink import StarlinkEdge

from .fakes import execution_context

RATE = 2_500_000.0
SAMPLE_COUNT = 14_000
MANIFEST_PATH = Path(__file__).parent / "fixtures/retro_qam_2026_08_17_v1.json"
MANIFEST_SHA256 = "47a5c98064128cfdcebcf1350acb3b3005f2646e769d45d8c92a5f2def22ba7e"


def _templates():
    return qin_edge_pilot_template_pair_v0_1(RATE, StarlinkEdge.LOWER)


def _analyze(samples, *, profile: str = "synthetic-rx"):
    return StarlinkAcquisitionV0_3(
        StarlinkAcquisitionConfigV0_3(
            profile,
            retained_candidate_count=4,
        ),
        execution_context(),
    ).analyze_receiver(
        samples,
        recording_id=RecordingId("rec_acquisition_v03"),
        recording_identity_digest=Digest.sha256(b"acquisition-v03"),
        segment_id=SegmentId("seg_acquisition_v03"),
        receiver_chain_id=ReceiverChainId("rx_acquisition_v03"),
        templates=_templates(),
    )


def _positive(epoch: int, cfo: float, *, seed: int = 7):
    case = StarlinkInjectionCaseV0_2(
        f"epoch{epoch}_cfo{cfo:+.0f}",
        seed,
        SAMPLE_COUNT,
        2.0,
        0.1,
        epoch,
        cfo,
        0.0,
        (0, 1, 2, 3),
    )
    return synthesize_starlink_injection_v0_2(_templates(), case)


def test_synthetic_matrix_covers_every_mod64_epoch_and_full_cfo_domain() -> None:
    cfo_matrix = (
        -400_000.0,
        -300_000.0,
        -200_000.0,
        -100_000.0,
        0.0,
        100_000.0,
        200_000.0,
        300_000.0,
        400_000.0,
    )
    observed_cfos = set()
    for residue in range(64):
        cfo = cfo_matrix[residue % len(cfo_matrix)]
        observed_cfos.add(cfo)
        result = _analyze(_positive(residue, cfo, seed=residue))
        winner = result.winner
        assert winner.refined_epoch_sample == residue
        assert winner.refined_cfo_hz == pytest.approx(cfo, abs=75.0)
        assert winner.verify_minus_control_margin > 0.8
        assert result.searched_cfo_min_hz == -400_000.0
        assert result.searched_cfo_max_hz == 400_000.0
    assert observed_cfos == set(cfo_matrix)


def test_multiple_alias_basins_survive_until_held_out_adjudication() -> None:
    templates = _templates()
    rng = random.Random(18)
    scale = 0.05 / math.sqrt(2)
    samples = np.asarray(
        [complex(rng.gauss(0, scale), rng.gauss(0, scale)) for _ in range(18_000)],
        dtype=np.complex128,
    )

    def inject(
        template, epoch: int, cfo: float, amplitude: float, *, acquire_only: bool
    ) -> None:
        period = RATE / 750.0
        template = np.asarray(template)
        for frame in range(5):
            start = epoch + round(frame * period)
            symbol_sets = range(2, 302, 2) if acquire_only else (None,)
            for symbol in symbol_sets:
                indexes = (
                    np.arange(len(template))
                    if symbol is None
                    else np.arange(
                        round(symbol * RATE * 4.4e-6),
                        round((symbol + 1) * RATE * 4.4e-6),
                    )
                )
                if start + indexes[-1] >= len(samples):
                    continue
                phase = np.exp(2j * np.pi * cfo * (start + indexes) / RATE)
                samples[start + indexes] += amplitude * phase * template[indexes]

    # A stronger acquire-symbol-only interferer creates an attractive coarse
    # alias.  The weaker complete signal must remain among the retained basins
    # and win only after the untouched odd pilot symbols are adjudicated.
    inject(
        templates.exact_samples,
        811,
        -160_000.0,
        5.0,
        acquire_only=True,
    )
    inject(
        templates.exact_samples,
        127,
        200_000.0,
        1.5,
        acquire_only=False,
    )
    result = StarlinkAcquisitionV0_3(
        StarlinkAcquisitionConfigV0_3("synthetic-rx", retained_candidate_count=8),
        execution_context(),
    ).analyze_receiver(
        samples,
        recording_id=RecordingId("rec_alias"),
        recording_identity_digest=Digest.sha256(b"alias"),
        segment_id=SegmentId("seg_alias"),
        receiver_chain_id=ReceiverChainId("rx_alias"),
        templates=templates,
    )
    assert len(result.candidates) == 8
    assert result.winner.refined_epoch_sample == 127
    assert result.winner.refined_cfo_hz == pytest.approx(200_000.0, abs=75.0)
    assert result.winner.verify_minus_control_margin > 0.4
    alias = next(item for item in result.candidates if item.refined_epoch_sample == 811)
    assert alias.acquire_score > result.winner.acquire_score
    assert alias.verify_minus_control_margin < result.winner.verify_minus_control_margin


def test_noise_and_wrong_pattern_use_identical_search_without_verdicts() -> None:
    rng = random.Random(91)
    noise = tuple(
        complex(rng.gauss(0, 0.1), rng.gauss(0, 0.1)) for _ in range(SAMPLE_COUNT)
    )
    templates = _templates()
    wrong = np.asarray(noise, dtype=np.complex128)
    indexes = np.arange(len(templates.conditioned_control_samples))
    for frame in range(4):
        start = 333 + round(frame * RATE / 750.0)
        wrong[start + indexes] += (
            2.0
            * np.exp(2j * np.pi * 240_000.0 * (start + indexes) / RATE)
            * np.asarray(templates.conditioned_control_samples)
        )
    noise_result = _analyze(noise)
    wrong_result = _analyze(wrong)
    assert (
        noise_result.coarse_search_cell_count == wrong_result.coarse_search_cell_count
    )
    assert noise_result.config_ref == wrong_result.config_ref
    for result in (noise_result, wrong_result):
        assert result.candidates_only is True
        assert "whole-revised-search-calibration-required" in result.reason_codes
        assert result.winner.frame_support >= 2
        assert result.refinement_search_cell_count <= 100_000
    # A cyclic code roll can become an exact timing alias when epoch itself is
    # searched.  The high score is therefore deliberately still candidate-only;
    # this control is not misrepresented as an empirical signal-absent null.
    assert wrong_result.winner.verify_minus_control_margin > 0.8


def test_contract_rejects_domain_and_resource_drift() -> None:
    with pytest.raises(ValueError, match="cover at least"):
        StarlinkAcquisitionConfigV0_3(
            "too-narrow", cfo_min_hz=-399_999.0, cfo_max_hz=400_000.0
        )
    with pytest.raises(ValueError, match="coarse search"):
        StarlinkAcquisitionV0_3(
            StarlinkAcquisitionConfigV0_3("bounded", maximum_coarse_search_cells=10),
            execution_context(),
        ).analyze_receiver(
            _positive(7, 0.0),
            recording_id=RecordingId("rec_bound"),
            recording_identity_digest=Digest.sha256(b"bound"),
            segment_id=SegmentId("seg_bound"),
            receiver_chain_id=ReceiverChainId("rx_bound"),
            templates=_templates(),
        )
    with pytest.raises(ValueError, match="maximum_probe_samples"):
        StarlinkAcquisitionV0_3(
            StarlinkAcquisitionConfigV0_3("bounded", maximum_probe_samples=100),
            execution_context(),
        ).analyze_receiver(
            _positive(7, 0.0),
            recording_id=RecordingId("rec_bound"),
            recording_identity_digest=Digest.sha256(b"bound"),
            segment_id=SegmentId("seg_bound"),
            receiver_chain_id=ReceiverChainId("rx_bound"),
            templates=_templates(),
        )


@pytest.mark.integration
def test_archived_retro_qam_winners_and_strong_constellations_are_recovered() -> None:
    assert hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest() == MANIFEST_SHA256
    document = json.loads(MANIFEST_PATH.read_bytes())
    root = Path(document["archive"]["root"])
    if not os.path.isdir(root):
        pytest.skip("read-only RETRO QAM corpus is not mounted")
    iq = document["iq_object"]
    fmt = document["format"]
    window = document["selected_window"]
    raw = np.memmap(
        root / iq["relative_path"],
        dtype="<i2",
        mode="r",
        shape=(iq["sample_count"], fmt["receiver_count"], 2),
    )
    templates = _templates()
    started = time.perf_counter()
    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    observed = []
    for expected in document["historical_conditioned_expectations"]:
        receiver = expected["receiver_index"]
        values = raw[
            window["sample_offset"] : window["sample_offset"] + window["sample_count"],
            receiver,
        ]
        samples = np.asarray(
            (values[:, 0].astype(np.float32) + 1j * values[:, 1]) / 32768.0,
            dtype=np.complex64,
        )
        common = {
            "recording_id": RecordingId("rec_retro_qam_20260813"),
            "recording_identity_digest": Digest.sha256(
                document["recording_id"].encode()
            ),
            "segment_id": SegmentId("seg_retro_qam_68p7s"),
            "receiver_chain_id": ReceiverChainId(f"rx_retro_qam_{receiver}"),
            "templates": templates,
        }
        acquisition = StarlinkAcquisitionV0_3(
            StarlinkAcquisitionConfigV0_3(f"pluto-5d4d-rx{receiver}"),
            execution_context(),
        ).analyze_receiver(samples, **common)
        winner = acquisition.winner
        assert winner.refined_epoch_sample == expected["winning_epoch_sample"]
        assert winner.refined_cfo_hz == pytest.approx(
            expected["winning_cfo_hz"], abs=35.0
        )
        assert winner.verify_minus_control_margin > 0.3

        # The published v0.2 search remains byte-for-byte unchanged.  A one-cell
        # v0.2 suite is used only to pass the new winner through the existing QAM
        # component until the integration steward wires the additive contract.
        suite = StarlinkDetectorSuiteV0_2(
            StarlinkDetectorSuiteConfigV0_2(
                (winner.refined_epoch_sample,),
                (winner.refined_cfo_hz,),
                (0.0,),
                maximum_probe_samples=len(samples),
            ),
            execution_context(),
        ).analyze_receiver(samples, **common)
        qam = StarlinkPilotConstellationAnalyzerV0_1(
            StarlinkPilotConstellationConfigV0_1(maximum_probe_samples=len(samples)),
            execution_context(),
        ).analyze(samples, suite)
        assert qam.complete_frame_count == expected["complete_frame_count"]
        assert qam.hard_symbol_accuracy == pytest.approx(
            expected["hard_symbol_accuracy"], abs=1 / 2_400
        )
        assert qam.rms_evm == pytest.approx(expected["rms_evm"], abs=1e-4)
        observed.append(
            {
                "receiver": receiver,
                "epoch_error_samples": (
                    winner.refined_epoch_sample - expected["winning_epoch_sample"]
                ),
                "cfo_error_hz": winner.refined_cfo_hz - expected["winning_cfo_hz"],
                "hard_symbol_accuracy": qam.hard_symbol_accuracy,
                "rms_evm": qam.rms_evm,
            }
        )
    elapsed = time.perf_counter() - started
    rss_delta_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - rss_before
    assert elapsed < 10.0
    assert rss_delta_kib < 64 * 1024
    assert observed == pytest.approx(
        [
            {
                "receiver": 0,
                "epoch_error_samples": 0,
                "cfo_error_hz": -16.194876406458206,
                "hard_symbol_accuracy": 0.7479166666666667,
                "rms_evm": 0.9426077037352968,
            },
            {
                "receiver": 1,
                "epoch_error_samples": 0,
                "cfo_error_hz": -29.608613572869217,
                "hard_symbol_accuracy": 0.7991666666666667,
                "rms_evm": 0.7826341713001846,
            },
        ],
        abs=1e-9,
    )
