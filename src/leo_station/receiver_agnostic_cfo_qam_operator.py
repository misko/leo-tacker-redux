"""One-shot capture-aware operator for explicit CFO/QAM v0.6 backfills."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol, TextIO

from leo_flow.adapters.systemd_credentials import SystemdCredentialProvider
from leo_flow.analysis.recording.api import AnalysisExecutionContext
from leo_flow.contracts.core import ReceiverChainId, RecordingId, SegmentId, UtcNs
from leo_flow.contracts.optional_heavy_work_admission import (
    OptionalHeavyWorkAdmissionPortV0_1,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.services.starlink_receiver_agnostic_cfo_qam import (
    CaptureAwareReceiverAgnosticCfoQamRunnerV0_6,
    ReceiverAgnosticCfoQamProductionResultV0_6,
    ReceiverAgnosticCfoQamWindowSelectionV0_6,
)

from .optional_heavy_work_admission import build_optional_heavy_work_admission


class ReceiverAgnosticCfoQamCycleV0_6(Protocol):
    def run_once(
        self,
        recording_id: RecordingId,
        selections: tuple[ReceiverAgnosticCfoQamWindowSelectionV0_6, ...],
    ) -> tuple[str, ReceiverAgnosticCfoQamProductionResultV0_6 | None]: ...


def build_cycle(
    credential_directory: Path,
    admission: OptionalHeavyWorkAdmissionPortV0_1,
) -> ReceiverAgnosticCfoQamCycleV0_6:
    from leo_flow.adapters.starlink_receiver_agnostic_cfo_qam_postgres import (
        PostgresReceiverAgnosticCfoQamCatalogV0_6,
    )
    from leo_flow.analysis.recording.starlink_receiver_agnostic_cfo_product_persistence import (
        DurableReceiverAgnosticCfoQamStoreV0_6,
    )
    from leo_flow.deployments.recording_submission_v1 import analysis_connection_factory
    from leo_flow.services.starlink_receiver_agnostic_cfo_qam import (
        DurableReceiverAgnosticCfoQamProducerV0_6,
    )
    from leo_flow.storage.filesystem import FileSystemBlobStore
    from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog
    from leo_flow.storage.recording_codec import SigMFRecordingObjectReader

    from .analysis_v1 import (
        CAS_ROOT,
        ENVIRONMENT_REF,
        SOURCE_COMMIT,
        SOURCE_COMMIT_UTC_NS,
    )

    credentials = SystemdCredentialProvider(credential_directory)
    connect = analysis_connection_factory(credentials.resolve("catalog-dsn"))
    blobs = FileSystemBlobStore(CAS_ROOT)
    product_catalog = PostgresReceiverAgnosticCfoQamCatalogV0_6(connect)
    products = DurableReceiverAgnosticCfoQamStoreV0_6(blobs, product_catalog)
    execution = AnalysisExecutionContext(
        "leo-flow-gauss-receiver-agnostic-cfo-qam",
        "0.6.0",
        SOURCE_COMMIT,
        ENVIRONMENT_REF.digest,
        UtcNs(int(SOURCE_COMMIT_UTC_NS)),
        UtcNs(int(SOURCE_COMMIT_UTC_NS)),
        "gauss-x86_64-python31116",
    )
    producer = DurableReceiverAgnosticCfoQamProducerV0_6(
        PostgresRecordingCatalog(connect),
        SigMFRecordingObjectReader(blobs),
        products,
        product_catalog,
        execution,
    )
    return CaptureAwareReceiverAgnosticCfoQamRunnerV0_6(admission, producer)


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    cycle_builder: Callable[
        [Path, OptionalHeavyWorkAdmissionPortV0_1],
        ReceiverAgnosticCfoQamCycleV0_6,
    ] = build_cycle,
    admission_builder: Callable[..., OptionalHeavyWorkAdmissionPortV0_1 | None] = (
        build_optional_heavy_work_admission
    ),
) -> int:
    parser = argparse.ArgumentParser(
        prog="leo-gauss-receiver-agnostic-cfo-qam",
        description=(
            "Analyze one published recording outside capture using explicit "
            "segment:receiver:edge:start:count windows."
        ),
    )
    parser.add_argument("--credential-directory", type=Path, required=True)
    parser.add_argument("--capture-guard-status", type=Path, required=True)
    parser.add_argument("--recording-id", required=True)
    parser.add_argument("--window", action="append", required=True)
    parser.add_argument("--maximum-focused-backlog", type=int, default=0)
    parser.add_argument("--host-cpu-cores", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--reserved-cpu-cores", type=int, default=8)
    parser.add_argument("--estimated-claim-cpu-cores", type=int, default=1)
    parser.add_argument(
        "--minimum-memory-available-bytes", type=int, default=8 * 1024**3
    )
    parser.add_argument("--maximum-io-pressure-avg10", type=float, default=5.0)
    parser.add_argument("--maximum-optional-concurrency", type=int, default=1)
    args = parser.parse_args(argv)
    try:
        selections = tuple(_selection(item) for item in args.window)
        admission = admission_builder(
            args.capture_guard_status,
            maximum_focused_backlog=args.maximum_focused_backlog,
            host_cpu_cores=args.host_cpu_cores,
            reserved_cpu_cores=args.reserved_cpu_cores,
            estimated_claim_cpu_cores=args.estimated_claim_cpu_cores,
            minimum_memory_available_bytes=args.minimum_memory_available_bytes,
            maximum_io_pressure_avg10=args.maximum_io_pressure_avg10,
            maximum_optional_concurrency=args.maximum_optional_concurrency,
        )
        if admission is None:
            raise ValueError("capture-aware admission is required")
        cycle = cycle_builder(args.credential_directory, admission)
        reason, result = cycle.run_once(RecordingId(args.recording_id), selections)
    except (OSError, RuntimeError, TypeError, ValueError):
        stderr.write('{"event":"receiver_agnostic_cfo_qam_failed"}\n')
        stderr.flush()
        return 4
    document: dict[str, object] = {
        "event": (
            "receiver_agnostic_cfo_qam_complete"
            if result is not None
            else "receiver_agnostic_cfo_qam_paused"
        ),
        "reason": reason,
        "recording_id": args.recording_id,
    }
    if result is not None:
        document.update(
            analysis_id=result.ref.analysis_id,
            bundle_digest=str(result.ref.bundle_ref.digest),
            reused=result.reused,
        )
    stdout.write(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")
    stdout.flush()
    return 0


def _selection(value: str) -> ReceiverAgnosticCfoQamWindowSelectionV0_6:
    fields = value.split(":")
    if len(fields) != 5:
        raise ValueError("window must be segment:receiver:edge:start:count")
    segment, receiver, edge, start, count = fields
    return ReceiverAgnosticCfoQamWindowSelectionV0_6(
        SegmentId(segment),
        ReceiverChainId(receiver),
        StarlinkEdge(edge),
        int(start),
        int(count),
    )


if __name__ == "__main__":
    raise SystemExit(main())
