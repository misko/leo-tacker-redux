"""Resume exact analysis for one already-terminal focused capture batch."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from leo_flow.adapters.capture_batch_sqlite import SQLiteCaptureBatchStateStore
from leo_flow.contracts.core import CaptureBatchId, Digest, DigestAlgorithm, UtcNs
from leo_flow.deployments.gauss_focused_analysis_runtime import (
    MAXIMUM_FOCUSED_COMPUTE_WORKERS,
    analyze_focused_pair,
)
from leo_flow.services.config import AnalysisServiceConfig, load_service_config

ANALYSIS_DEADLINE_NS = 3_600_000_000_000


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="leo-gauss-focused-analysis",
        description="Analyze one exact terminal focused capture without radio contact.",
    )
    parser.add_argument("--batch-database", type=Path, required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--analysis-config", type=Path, required=True)
    parser.add_argument("--analysis-credential-directory", type=Path, required=True)
    parser.add_argument("--dashboard-credential-directory", type=Path, required=True)
    parser.add_argument(
        "--compute-workers",
        type=int,
        default=MAXIMUM_FOCUSED_COMPUTE_WORKERS,
    )
    parser.add_argument("--arm", action="store_true")
    parser.add_argument("--capture-safe", action="store_true")
    parser.add_argument("--capture-definition-digest")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if (
        not args.arm
        or not args.batch_database.is_absolute()
        or ".." in args.batch_database.parts
        or not args.batch_database.is_file()
    ):
        return 3
    definition_digest: Digest | None = None
    if args.capture_definition_digest is not None:
        algorithm, separator, value = args.capture_definition_digest.partition(":")
        if not separator:
            return 3
        try:
            definition_digest = Digest(DigestAlgorithm(algorithm), value)
        except ValueError:
            return 3
    if args.capture_safe and definition_digest is None:
        return 3
    snapshot = SQLiteCaptureBatchStateStore(args.batch_database).get(
        CaptureBatchId(args.batch_id)
    )
    if (
        snapshot is None
        or not snapshot.terminal
        or len(snapshot.successful_recordings) != 2
    ):
        return 4
    config = load_service_config(args.analysis_config)
    if not isinstance(config, AnalysisServiceConfig):
        raise TypeError("focused analysis configuration is not an analysis service")
    receipt = analyze_focused_pair(
        snapshot,
        config,
        args.analysis_credential_directory,
        args.dashboard_credential_directory,
        deadline_utc_ns=UtcNs(time.time_ns() + ANALYSIS_DEADLINE_NS),
        compute_workers=args.compute_workers,
        capture_definition_digest=definition_digest,
        capture_safe=args.capture_safe,
    )
    print(
        json.dumps(
            {
                "event": "focused_analysis_complete",
                "batch_id": str(receipt.batch_id),
                "recording_ids": [str(item.recording_id) for item in receipt.successes],
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
