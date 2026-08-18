"""Independent bounded service loop for V15 full-dwell products."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol, TextIO

from leo_flow.adapters.systemd_credentials import SystemdCredentialProvider
from leo_flow.services.starlink_full_dwell_producer import (
    BoundedFullDwellProducerServiceV0_1,
    FullDwellAdmissionResultV0_1,
)


class ProducerCycle(Protocol):
    def run_once(self) -> tuple[FullDwellAdmissionResultV0_1, bool]: ...


def build_service(
    credential_directory: Path,
    *,
    worker_id: str,
    maximum_active: int,
    maximum_admissions_per_cycle: int,
) -> BoundedFullDwellProducerServiceV0_1:
    from leo_flow.adapters.starlink_full_dwell_postgres import (
        PostgresStarlinkFullDwellCatalogV0_1,
    )
    from leo_flow.adapters.starlink_full_dwell_work_postgres import (
        PostgresFullDwellWorkRepositoryV0_1,
    )
    from leo_flow.adapters.starlink_suite_postgres import (
        PostgresStarlinkSuiteCatalogV0_2,
    )
    from leo_flow.analysis.recording.starlink_full_dwell_response_persistence import (
        DurableStarlinkFullDwellStoreV0_1,
    )
    from leo_flow.analysis.recording.starlink_suite_persistence import (
        DurableStarlinkSuiteStoreV0_2,
    )
    from leo_flow.deployments.recording_submission_v1 import analysis_connection_factory
    from leo_flow.services.starlink_full_dwell_pipeline import (
        DurableFullDwellLeaseProducerV0_1,
    )
    from leo_flow.storage.filesystem import FileSystemBlobStore
    from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog
    from leo_flow.storage.recording_codec import SigMFRecordingObjectReader

    from .analysis_v1 import CAS_ROOT, starlink_full_dwell_profiles_v0_1

    credentials = SystemdCredentialProvider(credential_directory)
    connect = analysis_connection_factory(credentials.resolve("catalog-dsn"))
    blobs = FileSystemBlobStore(CAS_ROOT)
    return BoundedFullDwellProducerServiceV0_1(
        PostgresFullDwellWorkRepositoryV0_1(connect),
        DurableFullDwellLeaseProducerV0_1(
            PostgresRecordingCatalog(connect),
            SigMFRecordingObjectReader(blobs),
            DurableStarlinkSuiteStoreV0_2(
                blobs, PostgresStarlinkSuiteCatalogV0_2(connect)
            ),
            DurableStarlinkFullDwellStoreV0_1(
                blobs, PostgresStarlinkFullDwellCatalogV0_1(connect)
            ),
            starlink_full_dwell_profiles_v0_1(),
        ),
        worker_id=worker_id,
        maximum_active=maximum_active,
        maximum_admissions_per_cycle=maximum_admissions_per_cycle,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    service_builder: Callable[..., ProducerCycle] = build_service,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credential-directory", type=Path, required=True)
    parser.add_argument("--worker-id", default="gauss-full-dwell-1")
    parser.add_argument("--maximum-active", type=int, default=8)
    parser.add_argument("--maximum-admissions-per-cycle", type=int, default=2)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if not 0.1 <= args.poll_seconds <= 300:
        parser.error("--poll-seconds must lie in [0.1,300]")
    try:
        service = service_builder(
            args.credential_directory,
            worker_id=args.worker_id,
            maximum_active=args.maximum_active,
            maximum_admissions_per_cycle=args.maximum_admissions_per_cycle,
        )
        while True:
            admission, progressed = service.run_once()
            stdout.write(
                json.dumps(
                    {
                        "event": "full_dwell_cycle_complete",
                        "admitted": admission.admitted,
                        "active_backlog": admission.active_backlog,
                        "saturated": admission.saturated,
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
        stderr.write('{"event":"full_dwell_cycle_failed"}\n')
        stderr.flush()
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
