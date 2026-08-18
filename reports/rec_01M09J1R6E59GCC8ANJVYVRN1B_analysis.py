"""Reproduce the bounded signal investigation for one immutable Redux dwell."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from urllib.request import urlopen

import matplotlib.pyplot as plt
import numpy as np

from leo_flow.analysis.recording.api import AnalysisExecutionContext
from leo_flow.analysis.recording.starlink_acquisition import (
    StarlinkAcquisitionConfigV0_3,
    StarlinkAcquisitionV0_3,
)
from leo_flow.analysis.recording.starlink_detector_suite import (
    StarlinkDetectorSuiteConfigV0_2,
    StarlinkDetectorSuiteV0_2,
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
    DigestAlgorithm,
    ReceiverChainId,
    RecordingId,
    SegmentId,
    UtcNs,
)
from leo_flow.contracts.starlink import StarlinkEdge

RECORDING_ID = "rec_01M09J1R6E59GCC8ANJVYVRN1B"
SEGMENT_ID = "seg_plan_focused_loop_00000001_18cccbd3289eb706_b_ch4_lower"
RECEIVERS = ("rx_lnb_c", "rx_lnb_d")
SAMPLE_RATE_HZ = 2_500_000.0
SAMPLE_COUNT = 150_000_000
PROBE_SAMPLES = 20_000
POWER_TILE_SAMPLES = 20_000
DATA_SHA256 = "23cceb3a5223180ff92398214125513d4c32cc541ec1ae5b7c4c28fba5bbcc8c"
METADATA_SHA256 = "87c85ff367a29685c4e679112e13680831c51ff40d628e77348c82211eb6ad2c"
IDENTITY_SHA256 = "cedc0a9083495717048254249a3fe1569c879fe8f8f624d2ac12aceaddd53c69"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iq", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--oracle-report", type=Path, required=True)
    parser.add_argument("--uncalibrated-oracle-report", type=Path, required=True)
    parser.add_argument("--retro-receipt", type=Path, required=True)
    parser.add_argument("--dashboard-url", default="http://gauss:8090")
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--git-commit", required=True)
    parser.add_argument("--maximum-v03-windows", type=int, default=3)
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fetch_json(url: str) -> dict[str, object]:
    with urlopen(url, timeout=30) as response:
        return json.load(response)


def _execution(git_commit: str) -> AnalysisExecutionContext:
    instant = UtcNs(1_787_027_377_155_644_261)
    return AnalysisExecutionContext(
        "recording-signal-report",
        "0.1.0",
        git_commit,
        Digest.sha256(b"read-only-report-environment"),
        instant,
        instant,
        "gauss-x86_64-read-only-report",
    )


def _complex_window(
    raw: np.memmap, start: int, count: int, receiver: int
) -> np.ndarray:
    selected = raw[start : start + count, receiver]
    return np.asarray(
        (selected[:, 0].astype(np.float32) + 1j * selected[:, 1]) / 32768.0,
        dtype=np.complex64,
    )


def _power_tiles(raw: np.memmap) -> np.ndarray:
    tile_count = SAMPLE_COUNT // POWER_TILE_SAMPLES
    powers = np.empty((2, tile_count), dtype=np.float64)
    for first_tile in range(0, tile_count, 100):
        stop_tile = min(tile_count, first_tile + 100)
        block = raw[first_tile * POWER_TILE_SAMPLES : stop_tile * POWER_TILE_SAMPLES]
        for receiver in range(2):
            i_values = block[:, receiver, 0].astype(np.float64)
            q_values = block[:, receiver, 1].astype(np.float64)
            reshaped = (i_values * i_values + q_values * q_values).reshape(
                stop_tile - first_tile, POWER_TILE_SAMPLES
            )
            powers[receiver, first_tile:stop_tile] = np.mean(reshaped, axis=1)
    return powers


def _oracle_margin(check: dict[str, object], receiver: int) -> float:
    receivers = check["receivers"]
    assert isinstance(receivers, list)
    evidence = receivers[receiver]
    assert isinstance(evidence, dict)
    acquisition = evidence["acquisition"]
    assert isinstance(acquisition, dict)
    return float(acquisition["match_score_margin"])


def _production_suite_responses(
    raw: np.memmap, execution: AnalysisExecutionContext
) -> list[dict[str, object]]:
    period = round(SAMPLE_RATE_HZ / 750.0)
    config = StarlinkDetectorSuiteConfigV0_2(
        tuple(range(0, period, 64)),
        tuple(float(value) for value in range(-100_000, 100_001, 20_000)),
    )
    templates = qin_edge_pilot_template_pair_v0_1(SAMPLE_RATE_HZ, StarlinkEdge.LOWER)
    starts = tuple(range(0, SAMPLE_COUNT - PROBE_SAMPLES + 1, 12_500_000))
    if starts[-1] != SAMPLE_COUNT - PROBE_SAMPLES:
        starts += (SAMPLE_COUNT - PROBE_SAMPLES,)
    output: list[dict[str, object]] = []
    for start in starts:
        for receiver, receiver_chain_id in enumerate(RECEIVERS):
            samples = _complex_window(raw, start, PROBE_SAMPLES, receiver)
            suite = StarlinkDetectorSuiteV0_2(config, execution).analyze_receiver(
                samples,
                recording_id=RecordingId(RECORDING_ID),
                recording_identity_digest=Digest(
                    DigestAlgorithm.SHA256, IDENTITY_SHA256
                ),
                segment_id=SegmentId(SEGMENT_ID),
                receiver_chain_id=ReceiverChainId(receiver_chain_id),
                templates=templates,
            )
            output.append(
                {
                    "start_sample": start,
                    "start_s": start / SAMPLE_RATE_HZ,
                    "receiver_chain_id": receiver_chain_id,
                    "probe_samples": PROBE_SAMPLES,
                    "methods": [
                        {
                            "method": item.method.value,
                            "qin_score": item.reported_score,
                            "conditioned_control_score": item.conditioned_control_score,
                            "qin_minus_control": item.exact_minus_control_margin,
                            "winning_epoch_sample": item.winning_epoch_sample,
                            "winning_coarse_cfo_hz": item.winning_coarse_cfo_hz,
                            "winning_residual_cfo_hz": item.winning_residual_cfo_hz,
                        }
                        for item in suite.methods
                    ],
                }
            )
    return output


def _qam_goodness(accuracy: float, rms_evm: float) -> float:
    chance_corrected = min(1.0, max(0.0, (accuracy - 0.25) / 0.75))
    compactness = 1.0 / (1.0 + (rms_evm / 2.0) ** 2)
    return math.sqrt(chance_corrected * compactness)


def _v03_selected_windows(
    raw: np.memmap,
    oracle: dict[str, object],
    execution: AnalysisExecutionContext,
    maximum_windows: int,
) -> list[dict[str, object]]:
    checks = oracle["exact_checks"]
    assert isinstance(checks, list)
    ranked = sorted(
        checks,
        key=lambda check: min(
            _oracle_margin(check, 0),  # type: ignore[arg-type]
            _oracle_margin(check, 1),  # type: ignore[arg-type]
        ),
        reverse=True,
    )[:maximum_windows]
    templates = qin_edge_pilot_template_pair_v0_1(SAMPLE_RATE_HZ, StarlinkEdge.LOWER)
    output: list[dict[str, object]] = []
    for check_rank, check in enumerate(ranked):
        assert isinstance(check, dict)
        start_sample = int(check["start_sample"])
        count = round(float(check["duration_s"]) * SAMPLE_RATE_HZ)
        result: dict[str, object] = {
            "selection_rank": check_rank,
            "start_sample": start_sample,
            "start_s": float(check["start_s"]),
            "sample_count": count,
            "oracle_paired_minimum_margin": min(
                _oracle_margin(check, 0), _oracle_margin(check, 1)
            ),
            "oracle_candidate": bool(check["candidate"]),
            "oracle_qualified": bool(check["qualified"]),
            "receivers": [],
        }
        receivers = result["receivers"]
        assert isinstance(receivers, list)
        for receiver, receiver_chain_id in enumerate(RECEIVERS):
            samples = _complex_window(raw, start_sample, count, receiver)
            common = {
                "recording_id": RecordingId(RECORDING_ID),
                "recording_identity_digest": Digest(
                    DigestAlgorithm.SHA256, IDENTITY_SHA256
                ),
                "segment_id": SegmentId(SEGMENT_ID),
                "receiver_chain_id": ReceiverChainId(receiver_chain_id),
                "templates": templates,
            }
            profiles = [
                (
                    "current-default",
                    StarlinkAcquisitionConfigV0_3(
                        f"pluto-19f2-{receiver_chain_id}-current-default"
                    ),
                )
            ]
            if receiver_chain_id == "rx_lnb_c":
                # Diagnostic only: the current v0.3 contract requires that the
                # interval also include -400 kHz, so this is the union of the
                # default domain and calibrated LNB-C's +400 kHz guard.  A
                # centered-residual interface is the recommended production fix.
                profiles.append(
                    (
                        "lnb-aware-union-diagnostic",
                        StarlinkAcquisitionConfigV0_3(
                            f"pluto-19f2-{receiver_chain_id}-lnb-aware-union",
                            cfo_max_hz=1_040_000.0,
                        ),
                    )
                )
            profile_results: list[dict[str, object]] = []
            for profile_name, config in profiles:
                acquisition = StarlinkAcquisitionV0_3(
                    config, execution
                ).analyze_receiver(samples, **common)
                winner = acquisition.winner
                suite = StarlinkDetectorSuiteV0_2(
                    StarlinkDetectorSuiteConfigV0_2(
                        (winner.refined_epoch_sample,),
                        (winner.refined_cfo_hz,),
                        (0.0,),
                        maximum_probe_samples=count,
                    ),
                    execution,
                ).analyze_receiver(samples, **common)
                qam = StarlinkPilotConstellationAnalyzerV0_1(
                    StarlinkPilotConstellationConfigV0_1(maximum_probe_samples=count),
                    execution,
                ).analyze(samples, suite)
                profile_results.append(
                    {
                        "profile": profile_name,
                        "searched_cfo_min_hz": acquisition.searched_cfo_min_hz,
                        "searched_cfo_max_hz": acquisition.searched_cfo_max_hz,
                        "winning_epoch_sample": winner.refined_epoch_sample,
                        "winning_cfo_hz": winner.refined_cfo_hz,
                        "acquire_score": winner.acquire_score,
                        "verify_score": winner.verify_score,
                        "conditioned_control_score": winner.conditioned_control_score,
                        "verify_minus_control_margin": (
                            winner.verify_minus_control_margin
                        ),
                        "complete_frame_count": qam.complete_frame_count,
                        "hard_symbol_accuracy": qam.hard_symbol_accuracy,
                        "rms_evm": qam.rms_evm,
                        "soft_mean_expected_probability": (
                            qam.soft_mean_expected_probability
                        ),
                        "soft_mean_entropy_bits": qam.soft_mean_entropy_bits,
                        "qam_goodness_v0_2": _qam_goodness(
                            qam.hard_symbol_accuracy, qam.rms_evm
                        ),
                        "points": [
                            {
                                "i": point.i,
                                "q": point.q,
                                "expected_state": point.expected_state,
                            }
                            for point in qam.points
                        ],
                    }
                )
            receivers.append(
                {
                    "receiver_chain_id": receiver_chain_id,
                    "profiles": profile_results,
                }
            )
        output.append(result)
    return output


def _metadata_summary(metadata: dict[str, object]) -> dict[str, object]:
    continuity = metadata["continuity"]
    assert isinstance(continuity, list) and len(continuity) == 1
    value = continuity[0]["value"]
    assert isinstance(value, dict)
    refills = value["refills"]
    assert isinstance(refills, list)
    gains = np.asarray(
        [entry["gain_db_end"] for entry in refills],
        dtype=float,  # type: ignore[index]
    )
    rssi = np.asarray(
        [entry["rssi_db_end"] for entry in refills],
        dtype=float,  # type: ignore[index]
    )
    uncertainties = np.asarray(
        [entry["time_uncertainty_ns"] for entry in refills],
        dtype=np.int64,  # type: ignore[index]
    )
    return {
        "refill_count": len(refills),
        "gap_count": len(value["gaps"]),  # type: ignore[arg-type]
        "stream_ids": sorted({int(entry["stream_id"]) for entry in refills}),  # type: ignore[index]
        "gain_db_median": np.median(gains, axis=0).tolist(),
        "gain_db_range": [
            [float(np.min(gains[:, index])), float(np.max(gains[:, index]))]
            for index in range(2)
        ],
        "raw_rssi_field_median": np.median(rssi, axis=0).tolist(),
        "raw_rssi_field_range": [
            [float(np.min(rssi[:, index])), float(np.max(rssi[:, index]))]
            for index in range(2)
        ],
        "maximum_time_uncertainty_ns": int(np.max(uncertainties)),
    }


def _plot_waterfall(v9: dict[str, object], v19: dict[str, object], path: Path) -> None:
    tiles = v9["tiles"]
    assert isinstance(tiles, list)
    series = v19["series"]
    assert isinstance(series, list)
    by_receiver = {str(item["receiver_chain_id"]): item for item in series}  # type: ignore[index]
    figure, axes = plt.subplots(2, 1, figsize=(13, 8), sharex=True, sharey=True)
    for axis, tile in zip(axes, tiles, strict=True):
        receiver = str(tile["receiver_chain_id"])
        frequencies = np.asarray(tile["frequency_bin_offsets_hz"], dtype=float) / 1000
        bins = tile["time_bins"]
        matrix = np.asarray([item["power_db"] for item in bins], dtype=float)
        times = np.asarray([item["midpoint_utc_ns"] for item in bins], dtype=np.int64)
        times_s = (times - times[0]) / 1e9
        image = axis.imshow(
            matrix.T,
            aspect="auto",
            origin="lower",
            extent=(times_s[0], times_s[-1], frequencies[0], frequencies[-1]),
            cmap="magma",
            vmin=float(np.percentile(matrix, 2)),
            vmax=float(np.percentile(matrix, 99.5)),
        )
        advanced = by_receiver[receiver]["total"]  # type: ignore[index]
        reference_frequency = float(advanced["reference_frequency_hz"])  # type: ignore[index]
        center_frequency = float(tile["center_frequency_hz"])
        reference_utc_ns = int(advanced["reference_utc_ns"])  # type: ignore[index]
        drift_rate = float(advanced["drift_rate_hz_s"])  # type: ignore[index]
        path_khz = (
            reference_frequency
            - center_frequency
            + drift_rate * (times - reference_utc_ns) / 1e9
        ) / 1000
        axis.plot(
            times_s, path_khz, color="cyan", linewidth=1.2, label="Redux advanced path"
        )
        axis.set_ylabel(f"{receiver}\nIF offset [kHz]")
        axis.legend(loc="upper right")
        figure.colorbar(image, ax=axis, label="median-residual power [dB]")
    axes[-1].set_xlabel("time from first published bin [s]")
    figure.suptitle("Redux V9 full-dwell residual waterfalls (candidate paths only)")
    figure.tight_layout()
    figure.savefig(path, dpi=150)
    plt.close(figure)


def _plot_responses(
    powers: np.ndarray, responses: list[dict[str, object]], path: Path
) -> None:
    methods = ("anchor-8", "glrt-32", "full-frame-acquire", "full-frame-verify")
    figure, axes = plt.subplots(1 + len(methods), 1, figsize=(14, 12), sharex=True)
    power_time = (
        (np.arange(powers.shape[1]) + 0.5) * POWER_TILE_SAMPLES / SAMPLE_RATE_HZ
    )
    for receiver, receiver_chain_id in enumerate(RECEIVERS):
        axes[0].plot(
            power_time,
            10 * np.log10(np.maximum(powers[receiver], 1e-30)),
            linewidth=0.7,
            label=receiver_chain_id,
        )
    axes[0].set_ylabel("mean power\n[dB counts²]")
    axes[0].legend(loc="best")
    axes[0].set_title("Every 8 ms tile (7,500 per receiver; exact 100% union coverage)")
    for axis, method in zip(axes[1:], methods, strict=True):
        for receiver_chain_id in RECEIVERS:
            selected = [
                item
                for item in responses
                if item["receiver_chain_id"] == receiver_chain_id
            ]
            x_values = [
                float(item["start_s"]) + PROBE_SAMPLES / SAMPLE_RATE_HZ / 2
                for item in selected
            ]
            method_values = [
                next(
                    value
                    for value in item["methods"]  # type: ignore[union-attr]
                    if value["method"] == method
                )
                for item in selected
            ]
            axis.plot(
                x_values,
                [float(value["qin_score"]) for value in method_values],
                "o-",
                label=f"{receiver_chain_id} Qin",
            )
            axis.plot(
                x_values,
                [float(value["conditioned_control_score"]) for value in method_values],
                "x--",
                alpha=0.7,
                label=f"{receiver_chain_id} roll-17",
            )
        axis.set_ylabel(f"{method}\nscore")
        axis.set_ylim(bottom=0)
        axis.legend(ncol=2, fontsize=8)
    axes[-1].set_xlabel("dwell time [s]")
    figure.suptitle("Complete power tiles and sparse production-v0.2 detector probes")
    figure.tight_layout()
    figure.savefig(path, dpi=150)
    plt.close(figure)


def _plot_qam(windows: list[dict[str, object]], path: Path) -> None:
    def selected_profile(receiver: dict[str, object]) -> dict[str, object]:
        profiles = receiver["profiles"]
        assert isinstance(profiles, list)
        return max(
            profiles,
            key=lambda profile: float(profile["qam_goodness_v0_2"]),
        )

    best = max(
        windows,
        key=lambda window: min(
            float(selected_profile(receiver)["qam_goodness_v0_2"])
            for receiver in window["receivers"]  # type: ignore[union-attr]
        ),
    )
    receivers = best["receivers"]
    assert isinstance(receivers, list)
    figure, axes = plt.subplots(1, 2, figsize=(10, 5))
    colors = ("#56B4E9", "#E69F00", "#009E73", "#CC79A7")
    for axis, receiver in zip(axes, receivers, strict=True):
        profile = selected_profile(receiver)
        points = profile["points"]
        assert isinstance(points, list)
        for state in range(4):
            selected = [point for point in points if point["expected_state"] == state]
            axis.scatter(
                [float(point["i"]) for point in selected],
                [float(point["q"]) for point in selected],
                s=8,
                alpha=0.45,
                color=colors[state],
                label=f"Qin state {state}",
            )
        axis.axhline(0, color="white", alpha=0.15)
        axis.axvline(0, color="white", alpha=0.15)
        axis.set_aspect("equal", adjustable="datalim")
        axis.set_title(
            f"{receiver['receiver_chain_id']} · acc={float(profile['hard_symbol_accuracy']):.3f} "
            f"EVM={float(profile['rms_evm']):.3f}\n{profile['profile']}"
        )
        axis.set_xlabel("equalized I")
        axis.set_ylabel("equalized Q")
    axes[0].legend(fontsize=8)
    figure.suptitle(
        f"Best inspected Redux v0.3 forced QAM window at {float(best['start_s']):.3f} s\n"
        "post-selected candidate diagnostic; not a calibrated detection"
    )
    figure.tight_layout()
    figure.savefig(path, dpi=150)
    plt.close(figure)


def _without_points(windows: list[dict[str, object]]) -> list[dict[str, object]]:
    compact = json.loads(json.dumps(windows))
    for window in compact:
        for receiver in window["receivers"]:
            for profile in receiver["profiles"]:
                profile.pop("points", None)
    return compact


def _oracle_summary(oracle: dict[str, object]) -> dict[str, object]:
    checks = oracle["exact_checks"]
    assert isinstance(checks, list)
    candidates = [item for item in checks if item["candidate"]]
    qualified = [item for item in checks if item["qualified"]]

    def cfo_summary(items: list[dict[str, object]], receiver: int) -> dict[str, object]:
        cfos = np.asarray(
            [
                item["receivers"][receiver]["acquisition"]["exact_match"][  # type: ignore[index]
                    "frequency_offset_hz"
                ]
                for item in items
            ],
            dtype=float,
        )
        if not len(cfos):
            return {"count": 0, "outside_current_400khz_count": 0}
        return {
            "count": len(cfos),
            "minimum_hz": float(np.min(cfos)),
            "median_hz": float(np.median(cfos)),
            "maximum_hz": float(np.max(cfos)),
            "outside_current_400khz_count": int(
                np.count_nonzero(np.abs(cfos) > 400_000)
            ),
            "outside_current_400khz_fraction": float(np.mean(np.abs(cfos) > 400_000)),
        }

    return {
        "exact_check_count": len(checks),
        "exact_window_s": oracle["analysis"]["exact_window_s"],  # type: ignore[index]
        "exact_interval_s": oracle["analysis"]["exact_interval_s"],  # type: ignore[index]
        "exact_sampled_time_s": oracle["summary"]["exact_sampled_time_s"],  # type: ignore[index]
        "exact_temporal_coverage_fraction": oracle["summary"][  # type: ignore[index]
            "exact_temporal_coverage_fraction"
        ],
        "exact_candidate_count": oracle["summary"]["exact_candidate_count"],  # type: ignore[index]
        "exact_qualified_count": oracle["summary"]["exact_qualified_count"],  # type: ignore[index]
        "single_receiver_candidate_count": oracle["summary"][  # type: ignore[index]
            "single_receiver_candidate_count"
        ],
        "single_receiver_qualified_count": oracle["summary"][  # type: ignore[index]
            "single_receiver_qualified_count"
        ],
        "doppler_track": oracle["doppler_track"],
        "candidate_cfo": {
            RECEIVERS[receiver]: cfo_summary(candidates, receiver)
            for receiver in range(2)
        },
        "qualified_cfo": {
            RECEIVERS[receiver]: cfo_summary(qualified, receiver)
            for receiver in range(2)
        },
        "match_margin_quantiles": [
            {
                "receiver_chain_id": RECEIVERS[receiver],
                "median": float(
                    np.median([_oracle_margin(item, receiver) for item in checks])
                ),
                "p95": float(
                    np.percentile(
                        [_oracle_margin(item, receiver) for item in checks], 95
                    )
                ),
                "maximum": max(_oracle_margin(item, receiver) for item in checks),
            }
            for receiver in range(2)
        ],
    }


def main() -> int:
    args = _parser().parse_args()
    if args.maximum_v03_windows <= 0:
        raise ValueError("maximum-v03-windows must be positive")
    if args.iq.stat().st_size != 1_200_000_000 or _sha256(args.iq) != DATA_SHA256:
        raise ValueError("IQ object does not match the immutable recording")
    if _sha256(args.metadata) != METADATA_SHA256:
        raise ValueError("metadata object does not match the immutable recording")
    metadata = json.loads(args.metadata.read_bytes())
    oracle = json.loads(args.oracle_report.read_bytes())
    uncalibrated_oracle = json.loads(args.uncalibrated_oracle_report.read_bytes())
    retro = json.loads(args.retro_receipt.read_bytes())
    args.output_directory.mkdir(parents=True, exist_ok=True)
    base = args.dashboard_url.rstrip("/")
    v9 = _fetch_json(
        f"{base}/api/v9/recordings/{RECORDING_ID}/doppler-visualization?layer=residual"
    )
    v19 = _fetch_json(
        f"{base}/api/v19/recordings/{RECORDING_ID}/"
        "evidence-advanced-doppler?maximum_windows=4096"
    )
    raw = np.memmap(args.iq, dtype="<i2", mode="r", shape=(SAMPLE_COUNT, 2, 2))
    execution = _execution(args.git_commit)
    powers = _power_tiles(raw)
    responses = _production_suite_responses(raw, execution)
    selected = _v03_selected_windows(raw, oracle, execution, args.maximum_v03_windows)
    _plot_waterfall(v9, v19, args.output_directory / "redux_v9_waterfall.png")
    _plot_responses(powers, responses, args.output_directory / "redux_responses.png")
    _plot_qam(selected, args.output_directory / "redux_v03_best_qam.png")
    exact_checks = oracle["exact_checks"]
    assert isinstance(exact_checks, list)
    result = {
        "recording_id": RECORDING_ID,
        "recording_identity_digest": f"sha256:{IDENTITY_SHA256}",
        "data_sha256": DATA_SHA256,
        "metadata_sha256": METADATA_SHA256,
        "metadata": _metadata_summary(metadata),
        "power_tiling": {
            "tile_samples": POWER_TILE_SAMPLES,
            "tile_duration_s": POWER_TILE_SAMPLES / SAMPLE_RATE_HZ,
            "tiles_per_receiver": powers.shape[1],
            "analyzed_sample_count_per_receiver": int(
                powers.shape[1] * POWER_TILE_SAMPLES
            ),
            "coverage_fraction": 1.0,
            "receiver_power_db": [
                {
                    "minimum": float(10 * np.log10(np.min(item))),
                    "median": float(10 * np.log10(np.median(item))),
                    "maximum": float(10 * np.log10(np.max(item))),
                }
                for item in powers
            ],
        },
        "redux_v9": {
            "candidate_only": v9["candidate_only"],
            "calibrated_detection_count": v9["calibrated_detection_count"],
            "basic_candidate_count": len(v9["candidates"]),  # type: ignore[arg-type]
            "tiles": [
                {
                    "receiver_chain_id": tile["receiver_chain_id"],
                    "coverage": tile["coverage"],
                }
                for tile in v9["tiles"]  # type: ignore[union-attr]
            ],
            "advanced_evidence": v9["advanced_evidence"],
            "advanced_window_series": [
                {"receiver_chain_id": item["receiver_chain_id"], "total": item["total"]}
                for item in v19["series"]  # type: ignore[union-attr]
                if item["receiver_chain_id"] in RECEIVERS
            ],
        },
        "redux_v02_sparse_probes": {
            "probe_samples": PROBE_SAMPLES,
            "probe_duration_s": PROBE_SAMPLES / SAMPLE_RATE_HZ,
            "starts_per_receiver": len(responses) // 2,
            "union_coverage_s_per_receiver": (
                len(responses) // 2 * PROBE_SAMPLES / SAMPLE_RATE_HZ
            ),
            "coverage_fraction": (len(responses) // 2 * PROBE_SAMPLES / SAMPLE_COUNT),
            "responses": responses,
        },
        "leo_tracker_oracle": {
            "repository_commit": "0bb80d14759fd8496b74e7d3219a690be18565a6",
            "uncalibrated": _oracle_summary(uncalibrated_oracle),
            "calibrated": _oracle_summary(oracle),
        },
        "redux_v03_postselected": _without_points(selected),
        "retro_positive": {
            "receipt_sha256": _sha256(args.retro_receipt),
            "metrics_match_oracle": retro["metrics_match_oracle"],
            "candidate_only": retro["candidate_only"],
            "calibrated_detection": retro["calibrated_detection"],
            "receivers": retro["receivers"],
            "combined": retro["combined"],
        },
        "limitations": [
            "candidate evidence only; no calibrated detection threshold",
            "v0.3 windows were post-selected by the historical detector on the same dwell",
            "roll-17 is a conditioned control, not an independent signal-absent null",
            "QAM symbols are known edge pilots, not user payload",
        ],
    }
    (args.output_directory / "results.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
