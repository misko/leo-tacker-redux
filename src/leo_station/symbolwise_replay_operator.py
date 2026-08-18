"""Capture-aware bounded operator for explicitly queued symbolwise replay."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol, TextIO

from leo_flow.adapters.systemd_credentials import SystemdCredentialProvider
from leo_flow.analysis.recording.api import AnalysisExecutionContext
from leo_flow.contracts.core import UtcNs
from leo_flow.contracts.optional_heavy_work_admission import (
    OptionalHeavyWorkAdmissionPortV0_1,
)

from .optional_heavy_work_admission import build_optional_heavy_work_admission


class SymbolwiseReplayCycle(Protocol):
    def run_once(self) -> bool: ...


def build_service(
    credential_directory: Path,
    *,
    worker_id: str,
    lease_ttl_s: float,
) -> SymbolwiseReplayCycle:
    from leo_flow.adapters.starlink_symbolwise_replay_postgres import (
        PostgresStarlinkSymbolwiseReplayRepositoryV0_1,
    )
    from leo_flow.analysis.recording.starlink_symbolwise_replay_product_persistence import (
        DurableStarlinkSymbolwiseReplayStoreV0_1,
    )
    from leo_flow.deployments.recording_submission_v1 import analysis_connection_factory
    from leo_flow.services.starlink_symbolwise_replay_product import (
        BoundedStarlinkSymbolwiseReplayServiceV0_1,
        DurableStarlinkSymbolwiseReplayLeaseProducerV0_1,
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
    repository = PostgresStarlinkSymbolwiseReplayRepositoryV0_1(connect)
    execution = AnalysisExecutionContext(
        "leo-flow-gauss-starlink-symbolwise-replay",
        "0.1.0",
        SOURCE_COMMIT,
        ENVIRONMENT_REF.digest,
        UtcNs(int(SOURCE_COMMIT_UTC_NS)),
        UtcNs(int(SOURCE_COMMIT_UTC_NS)),
        "gauss-x86_64-python31116",
    )
    return BoundedStarlinkSymbolwiseReplayServiceV0_1(
        repository,
        DurableStarlinkSymbolwiseReplayLeaseProducerV0_1(
            PostgresRecordingCatalog(connect),
            SigMFRecordingObjectReader(blobs),
            DurableStarlinkSymbolwiseReplayStoreV0_1(blobs, repository),
            execution,
        ),
        worker_id=worker_id,
        lease_ttl_s=lease_ttl_s,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    service_builder: Callable[..., SymbolwiseReplayCycle] = build_service,
    admission_builder: Callable[..., OptionalHeavyWorkAdmissionPortV0_1 | None] = (
        build_optional_heavy_work_admission
    ),
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credential-directory", type=Path, required=True)
    parser.add_argument("--worker-id", default="gauss-symbolwise-replay-1")
    parser.add_argument("--lease-ttl-seconds", type=float, default=7200.0)
    parser.add_argument("--poll-seconds", type=float, default=30.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--capture-guard-status", type=Path, required=True)
    parser.add_argument("--maximum-focused-backlog", type=int, default=0)
    parser.add_argument("--host-cpu-cores", type=int, default=0)
    parser.add_argument("--reserved-cpu-cores", type=int, default=8)
    parser.add_argument("--estimated-claim-cpu-cores", type=int, default=1)
    parser.add_argument(
        "--minimum-memory-available-bytes", type=int, default=8 * 1024**3
    )
    parser.add_argument("--maximum-io-pressure-avg10", type=float, default=5.0)
    parser.add_argument("--maximum-optional-concurrency", type=int, default=1)
    args = parser.parse_args(argv)
    if not 0.1 <= args.poll_seconds <= 300:
        parser.error("--poll-seconds must lie in [0.1,300]")
    if not 60 <= args.lease_ttl_seconds <= 28_800:
        parser.error("--lease-ttl-seconds must lie in [60,28800]")
    try:
        service = service_builder(
            args.credential_directory,
            worker_id=args.worker_id,
            lease_ttl_s=args.lease_ttl_seconds,
        )
        admission_gate = admission_builder(
            args.capture_guard_status,
            maximum_focused_backlog=args.maximum_focused_backlog,
            host_cpu_cores=args.host_cpu_cores,
            reserved_cpu_cores=args.reserved_cpu_cores,
            estimated_claim_cpu_cores=args.estimated_claim_cpu_cores,
            minimum_memory_available_bytes=args.minimum_memory_available_bytes,
            maximum_io_pressure_avg10=args.maximum_io_pressure_avg10,
            maximum_optional_concurrency=args.maximum_optional_concurrency,
        )
        while True:
            if admission_gate is not None:
                decision, permit = admission_gate.acquire()
                if not decision.admitted:
                    stdout.write(
                        json.dumps(
                            {
                                "event": "symbolwise_replay_cycle_paused",
                                "reason": decision.reason,
                            },
                            sort_keys=True,
                        )
                        + "\n"
                    )
                    stdout.flush()
                    if args.once:
                        return 0
                    sleeper(args.poll_seconds)
                    continue
            else:
                permit = None
            try:
                progressed = service.run_once()
            finally:
                if permit is not None:
                    permit.release()
            stdout.write(
                json.dumps(
                    {
                        "event": "symbolwise_replay_cycle_complete",
                        "processed": progressed,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            stdout.flush()
            if args.once:
                return 0
            if not progressed:
                sleeper(args.poll_seconds)
    except Exception:  # noqa: BLE001 - sanitized process boundary
        stderr.write('{"event":"symbolwise_replay_cycle_failed"}\n')
        stderr.flush()
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
