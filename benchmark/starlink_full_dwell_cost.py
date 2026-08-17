"""Read-only cost probe for the full-dwell Starlink response plan.

The command never writes beside the recording.  It reports exhaustive power
prescreen throughput and one exact all-method Qin-plus-surrogate window, then
uses the measured exact-window cost only as a transparent linear estimate.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import numpy as np

from leo_flow.analysis.recording.api import AnalysisExecutionContext
from leo_flow.analysis.recording.starlink_detector_suite import (
    StarlinkDetectorSuiteConfigV0_2,
)
from leo_flow.analysis.recording.starlink_surrogate_null import (
    ReportMethodStarlinkDetectorV0_1,
    StarlinkPairedSurrogateAnalyzerV0_1,
    radio_signal_v0_1,
)
from leo_flow.contracts.core import (
    Digest,
    ReceiverChainId,
    RecordingId,
    SegmentId,
    UtcNs,
)
from leo_flow.contracts.starlink import StarlinkEdge


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--sample-rate-hz", type=float, default=2_500_000.0)
    parser.add_argument("--receiver-count", type=int, default=2)
    parser.add_argument("--receiver", type=int, default=0)
    parser.add_argument("--exact-offset", type=int, default=38_000_000)
    parser.add_argument("--fine-window-samples", type=int, default=20_000)
    parser.add_argument("--coarse-window-samples", type=int, default=5_000_000)
    parser.add_argument("--surrogates", type=int, default=4)
    args = parser.parse_args()
    scalar_count = args.path.stat().st_size // 2
    sample_count = scalar_count // (args.receiver_count * 2)
    raw = np.memmap(
        args.path,
        dtype="<i2",
        mode="r",
        shape=(sample_count, args.receiver_count, 2),
    )
    starts = tuple(
        range(0, sample_count - args.fine_window_samples + 1, args.fine_window_samples)
    )
    if starts[-1] != sample_count - args.fine_window_samples:
        starts += (sample_count - args.fine_window_samples,)
    prescreen_started = time.perf_counter()
    powers = tuple(
        float(
            np.mean(
                raw[start : start + args.fine_window_samples, args.receiver, 0].astype(
                    np.float64
                )
                ** 2
                + raw[
                    start : start + args.fine_window_samples, args.receiver, 1
                ].astype(np.float64)
                ** 2
            )
        )
        for start in starts
    )
    prescreen_seconds = time.perf_counter() - prescreen_started

    epoch_stride = 64
    period = round(args.sample_rate_hz / 750.0)
    config = StarlinkDetectorSuiteConfigV0_2(
        tuple(range(0, period, epoch_stride)),
        tuple(float(value) for value in range(-100_000, 100_001, 20_000)),
    )
    window = raw[
        args.exact_offset : args.exact_offset + args.fine_window_samples,
        args.receiver,
    ]
    samples = tuple(complex(float(i) / 32768.0, float(q) / 32768.0) for i, q in window)
    now = UtcNs(1_800_000_000_000_000_000)
    execution = AnalysisExecutionContext(
        "full-dwell-cost-probe",
        "0.1.0",
        "benchmark",
        Digest.sha256(b"benchmark-environment"),
        now,
        UtcNs(int(now) + 1),
        "benchmark-host",
    )
    paired = StarlinkPairedSurrogateAnalyzerV0_1(
        ReportMethodStarlinkDetectorV0_1(execution), config
    )
    exact_started = time.perf_counter()
    evidence = paired.analyze(
        radio_signal_v0_1(
            samples,
            recording_id=RecordingId("rec_read_only_benchmark"),
            recording_identity_digest=Digest.sha256(str(args.path).encode()),
            segment_id=SegmentId("seg_read_only_benchmark"),
            receiver_chain_id=ReceiverChainId(f"rx_{args.receiver}"),
            edge=StarlinkEdge.LOWER,
            sample_rate_hz=args.sample_rate_hz,
        ),
        surrogate_count=args.surrogates,
    )
    exact_seconds = time.perf_counter() - exact_started
    fine_exact_windows = math.ceil(sample_count / args.fine_window_samples)
    coarse_exact_windows = math.ceil(sample_count / args.coarse_window_samples)
    print(
        json.dumps(
            {
                "path": str(args.path),
                "bytes": args.path.stat().st_size,
                "sample_count": sample_count,
                "duration_seconds": sample_count / args.sample_rate_hz,
                "prescreen_windows": len(starts),
                "prescreen_seconds": prescreen_seconds,
                "prescreen_megasamples_per_second": sample_count
                / prescreen_seconds
                / 1e6,
                "maximum_prescreen_power": max(powers),
                "exact_benchmark_window_samples": args.fine_window_samples,
                "exact_all_methods_pattern_count": 1 + len(evidence.surrogates),
                "exact_seconds": exact_seconds,
                "fine_exhaustive_exact_window_count": fine_exact_windows,
                "fine_exhaustive_linear_estimate_seconds": fine_exact_windows
                * exact_seconds,
                "frozen_multiresolution_coarse_window_samples": args.coarse_window_samples,
                "frozen_multiresolution_coarse_window_count": coarse_exact_windows,
                "coarse_linear_estimate_seconds": coarse_exact_windows
                * exact_seconds
                * (args.coarse_window_samples / args.fine_window_samples),
                "estimate_warning": "linear operation-count estimate; not a wall-clock claim for large windows",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
