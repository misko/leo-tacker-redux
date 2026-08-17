"""Reproduce the 2026-08-13 historical QAM observation with Redux analyzers."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

from leo_flow.analysis.recording.starlink_detector_suite import (
    StarlinkDetectorSuiteV0_2,
)
from leo_flow.analysis.recording.starlink_pilot_constellation import (
    StarlinkPilotConstellationAnalyzerV0_1,
    StarlinkPilotConstellationConfigV0_1,
)
from leo_flow.analysis.recording.starlink_templates import (
    qin_edge_pilot_template_pair_v0_1,
)
from leo_flow.contracts.core import Digest, ReceiverChainId, RecordingId, SegmentId
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_detector_suite import StarlinkDetectorMethod
from leo_station.analysis_v1 import (
    _starlink_pilot_constellation_execution,
    _starlink_suite_execution,
    starlink_suite_profile_v0_2,
)

RECORDING = "ch4-lower-edge-narrow-pluto-5d4d-20260813T211014Z"
RATE = 2_500_000
CLIP_START_S = 53.5
WINDOW_START_S = 68.7
PROBE_SAMPLES = 20_000
HISTORICAL_EPOCH_SAMPLE = 2_063
HISTORICAL_CFO_HZ = (364_150.8476787003, -194_343.8743595247)


def _samples(clip: Path, receiver: int, count: int = PROBE_SAMPLES) -> np.ndarray:
    first = round((WINDOW_START_S - CLIP_START_S) * RATE)
    sample_count = clip.stat().st_size // (2 * 2 * 2)
    raw = np.memmap(clip, dtype="<i2", mode="r", shape=(sample_count, 2, 2))
    values = raw[first : first + count, receiver]
    # Match the production recording reader's complex64 contract.  The
    # constellation analyzer converts each bounded slice to complex128 before
    # applying CFO correction.
    return np.asarray(
        (values[:, 0].astype(np.float32) + 1j * values[:, 1]) / 32768.0,
        dtype=np.complex64,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _acquire(suite):
    return next(
        item
        for item in suite.methods
        if item.method is StarlinkDetectorMethod.FULL_FRAME_ACQUIRE
    )


def _run(samples: np.ndarray, receiver: int, config):
    templates = qin_edge_pilot_template_pair_v0_1(RATE, StarlinkEdge.LOWER)
    return StarlinkDetectorSuiteV0_2(
        config, _starlink_suite_execution()
    ).analyze_receiver(
        samples,
        recording_id=RecordingId(f"rec_retro_{RECORDING}"),
        recording_identity_digest=Digest.sha256(RECORDING.encode()),
        segment_id=SegmentId("seg_retro_selected_68p7s"),
        receiver_chain_id=ReceiverChainId(f"rx_retro_{receiver}"),
        templates=templates,
    )


def _evidence(samples: np.ndarray, suite):
    return StarlinkPilotConstellationAnalyzerV0_1(
        StarlinkPilotConstellationConfigV0_1(
            maximum_probe_samples=max(PROBE_SAMPLES, len(samples))
        ),
        _starlink_pilot_constellation_execution(),
    ).analyze(samples, suite)


def analyze_receiver(
    clip: Path, receiver: int
) -> tuple[dict[str, object], object, object, object]:
    samples = _samples(clip, receiver)
    production = starlink_suite_profile_v0_2(RATE).config
    production_suite = _run(samples, receiver, production)
    production_acquire = _acquire(production_suite)
    production_qam = _evidence(samples, production_suite)

    wide = replace(
        production,
        coarse_cfo_hypotheses_hz=tuple(
            float(value) for value in range(-400_000, 400_001, 20_000)
        ),
    )
    wide_suite = _run(samples, receiver, wide)
    wide_acquire = _acquire(wide_suite)

    coarse_epoch = wide_acquire.winning_epoch_sample
    coarse_cfo = round(wide_acquire.winning_coarse_cfo_hz / 1_000) * 1_000
    refined = replace(
        production,
        epoch_hypotheses_samples=tuple(
            range(max(0, coarse_epoch - 64), min(3_333, coarse_epoch + 64) + 1)
        ),
        coarse_cfo_hypotheses_hz=tuple(
            float(value)
            for value in range(
                int(coarse_cfo) - 20_000, int(coarse_cfo) + 20_001, 1_000
            )
        ),
    )
    refined_suite = _run(samples, receiver, refined)
    refined_acquire = _acquire(refined_suite)
    refined_qam = _evidence(samples, refined_suite)
    # Recreate the historical 10 ms support separately.  The deployed Redux
    # suite uses an 8 ms probe, while the oracle report used six frames in a
    # 25,000-sample window.
    historical_samples = _samples(clip, receiver, 25_000)
    historical_source_suite = _run(historical_samples, receiver, production)
    replay_methods = tuple(
        replace(
            method,
            winning_epoch_sample=HISTORICAL_EPOCH_SAMPLE,
            winning_coarse_cfo_hz=HISTORICAL_CFO_HZ[receiver],
            winning_residual_cfo_hz=0.0,
        )
        if method.method is StarlinkDetectorMethod.FULL_FRAME_ACQUIRE
        else method
        for method in historical_source_suite.methods
    )
    historical_winner_qam = _evidence(
        historical_samples, replace(historical_source_suite, methods=replay_methods)
    )

    def summary(acquire, qam) -> dict[str, object]:
        return {
            "winning_epoch_sample": acquire.winning_epoch_sample,
            "winning_coarse_cfo_hz": acquire.winning_coarse_cfo_hz,
            "winning_residual_cfo_hz": acquire.winning_residual_cfo_hz,
            "search_score": acquire.reported_score,
            "control_score": acquire.conditioned_control_score,
            "hard_symbol_accuracy": qam.hard_symbol_accuracy,
            "rms_evm": qam.rms_evm,
            "model_snr_db": qam.model_snr_db,
            "complete_frame_count": qam.complete_frame_count,
            "residual_cfo_refinement_hz": qam.residual_cfo_refinement_hz,
        }

    result = {
        "receiver": receiver,
        "production": summary(production_acquire, production_qam),
        "wide_coarse_winner": {
            "winning_epoch_sample": wide_acquire.winning_epoch_sample,
            "winning_coarse_cfo_hz": wide_acquire.winning_coarse_cfo_hz,
            "winning_residual_cfo_hz": wide_acquire.winning_residual_cfo_hz,
            "search_score": wide_acquire.reported_score,
        },
        "refined": summary(refined_acquire, refined_qam),
        "historical_winner_replay": {
            "winning_epoch_sample": HISTORICAL_EPOCH_SAMPLE,
            "winning_coarse_cfo_hz": HISTORICAL_CFO_HZ[receiver],
            "hard_symbol_accuracy": historical_winner_qam.hard_symbol_accuracy,
            "rms_evm": historical_winner_qam.rms_evm,
            "model_snr_db": historical_winner_qam.model_snr_db,
            "complete_frame_count": historical_winner_qam.complete_frame_count,
            "residual_cfo_refinement_hz": (
                historical_winner_qam.residual_cfo_refinement_hz
            ),
        },
        "searches": {
            "production_outer_cells": production.outer_search_cell_count,
            "wide_outer_cells": wide.outer_search_cell_count,
            "refined_outer_cells": refined.outer_search_cell_count,
        },
    }
    return result, production_qam, refined_qam, historical_winner_qam


def _constellation(axis, evidence, title: str) -> None:
    states = np.asarray([point.expected_state for point in evidence.points])
    x = np.asarray([point.i for point in evidence.points])
    y = np.asarray([point.q for point in evidence.points])
    colors = ("#00c2ff", "#ffbf69", "#9cff57", "#ff5da2")
    for state, color in enumerate(colors):
        selected = states == state
        axis.scatter(x[selected], y[selected], s=4, alpha=0.22, color=color)
    ideal = np.exp(0.5j * np.pi * (np.arange(4) + 0.5))
    axis.scatter(ideal.real, ideal.imag, s=90, marker="x", color="white")
    axis.axhline(0, color="#607080", lw=0.5)
    axis.axvline(0, color="#607080", lw=0.5)
    axis.set_aspect("equal", adjustable="box")
    axis.set_title(
        f"{title}\naccuracy={evidence.hard_symbol_accuracy:.3f}, EVM={evidence.rms_evm:.3f}"
    )
    axis.set_xlabel("equalized I")
    axis.set_ylabel("equalized Q")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clip", type=Path, required=True)
    parser.add_argument("--historical-json", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    args.output_directory.mkdir(parents=True, exist_ok=True)

    results = []
    evidence = []
    for receiver in range(2):
        result, production, refined, historical_winner = analyze_receiver(
            args.clip, receiver
        )
        results.append(result)
        evidence.append((production, refined, historical_winner))

    historical = json.loads(args.historical_json.read_text())
    payload = {
        "recording": RECORDING,
        "source_clip": str(args.clip),
        "clip_sha256": _sha256(args.clip),
        "selected_window_start_s": WINDOW_START_S,
        "selected_probe_duration_s": PROBE_SAMPLES / RATE,
        "historical": {
            "selected_observation": historical["selected_observation"],
            "receivers": [
                {
                    "epoch_sample": item["epoch_sample"],
                    "carrier_offset_hz": item["carrier_offset_hz"],
                    "hard_symbol_accuracy": item["pilot"]["hard_symbol_accuracy"],
                    "rms_evm": item["pilot"]["rms_evm"],
                    "frame_count": item["pilot"]["frame_count"],
                }
                for item in historical["receivers"]
            ],
            "combined": historical["combined"]["soft_dual_rx"]["pilot"],
        },
        "redux": results,
    }
    (args.output_directory / "results.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
    )

    figure, axes = plt.subplots(2, 3, figsize=(15, 10), constrained_layout=True)
    for receiver, (production, refined, historical_winner) in enumerate(evidence):
        _constellation(axes[receiver, 0], production, f"RX{receiver} production search")
        _constellation(axes[receiver, 1], refined, f"RX{receiver} widened + refined")
        _constellation(
            axes[receiver, 2],
            historical_winner,
            f"RX{receiver} historical winner replay",
        )
    figure.suptitle("Exact archived 68.7 s Starlink pilot window through Redux")
    figure.savefig(args.output_directory / "redux_production_vs_refined.png", dpi=180)
    plt.close(figure)

    labels = [
        "RX0 production",
        "RX0 refined",
        "RX0 replay",
        "RX1 production",
        "RX1 refined",
        "RX1 replay",
    ]
    accuracies = [
        results[0]["production"]["hard_symbol_accuracy"],
        results[0]["refined"]["hard_symbol_accuracy"],
        results[0]["historical_winner_replay"]["hard_symbol_accuracy"],
        results[1]["production"]["hard_symbol_accuracy"],
        results[1]["refined"]["hard_symbol_accuracy"],
        results[1]["historical_winner_replay"]["hard_symbol_accuracy"],
    ]
    figure, axis = plt.subplots(figsize=(10, 4.8), constrained_layout=True)
    bars = axis.bar(
        labels,
        accuracies,
        color=("#d95f59", "#e0a341", "#4bc47b") * 2,
    )
    axis.axhline(0.25, color="black", linestyle="--", label="random chance")
    axis.set_ylim(0, 1)
    axis.set_ylabel("Known-pilot hard-symbol accuracy")
    axis.legend()
    axis.bar_label(bars, fmt="%.3f")
    figure.savefig(args.output_directory / "redux_accuracy_comparison.png", dpi=180)
    plt.close(figure)


if __name__ == "__main__":
    main()
