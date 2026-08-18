"""Independent bounded operator for adaptive Qin/surrogate response products."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol, TextIO

from leo_flow.adapters.systemd_credentials import SystemdCredentialProvider


class AdaptiveCycle(Protocol):
    def run_once(self) -> bool: ...


def build_service(
    credential_directory: Path,
    *,
    worker_id: str,
    lease_ttl_s: float,
) -> AdaptiveCycle:
    from leo_flow.adapters.starlink_adaptive_qam_postgres import (
        PostgresStarlinkAdaptiveQamCatalogV0_4,
    )
    from leo_flow.adapters.starlink_adaptive_response_postgres import (
        PostgresAdaptiveResponseWorkRepositoryV0_1,
        PostgresStarlinkAdaptiveResponseCatalogV0_1,
    )
    from leo_flow.adapters.starlink_suite_postgres import (
        PostgresStarlinkSuiteCatalogV0_2,
    )
    from leo_flow.analysis.recording.starlink_adaptive_qam_persistence import (
        DurableStarlinkAdaptiveQamStoreV0_4,
    )
    from leo_flow.analysis.recording.starlink_adaptive_response_persistence import (
        DurableStarlinkAdaptiveResponseStoreV0_1,
    )
    from leo_flow.analysis.recording.starlink_suite_persistence import (
        DurableStarlinkSuiteStoreV0_2,
    )
    from leo_flow.deployments.recording_submission_v1 import analysis_connection_factory
    from leo_flow.services.starlink_adaptive_qam_analysis import (
        DurableAdaptiveQamProducerV0_4,
    )
    from leo_flow.services.starlink_adaptive_response import (
        BoundedAdaptiveResponseServiceV0_1,
        DurableAdaptiveResponseLeaseProducerV0_1,
    )
    from leo_flow.storage.filesystem import FileSystemBlobStore
    from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog
    from leo_flow.storage.recording_codec import SigMFRecordingObjectReader

    from .analysis_v1 import (
        CAS_ROOT,
        starlink_acquired_dwell_profiles_v0_3,
        starlink_full_dwell_profiles_v0_1,
    )

    credentials = SystemdCredentialProvider(credential_directory)
    connect = analysis_connection_factory(credentials.resolve("catalog-dsn"))
    blobs = FileSystemBlobStore(CAS_ROOT)
    return BoundedAdaptiveResponseServiceV0_1(
        PostgresAdaptiveResponseWorkRepositoryV0_1(connect),
        DurableAdaptiveResponseLeaseProducerV0_1(
            PostgresRecordingCatalog(connect),
            SigMFRecordingObjectReader(blobs),
            DurableStarlinkSuiteStoreV0_2(
                blobs, PostgresStarlinkSuiteCatalogV0_2(connect)
            ),
            DurableStarlinkAdaptiveResponseStoreV0_1(
                blobs, PostgresStarlinkAdaptiveResponseCatalogV0_1(connect)
            ),
            starlink_full_dwell_profiles_v0_1(),
            DurableAdaptiveQamProducerV0_4(
                SigMFRecordingObjectReader(blobs),
                DurableStarlinkAdaptiveQamStoreV0_4(
                    blobs, PostgresStarlinkAdaptiveQamCatalogV0_4(connect)
                ),
                starlink_acquired_dwell_profiles_v0_3(),
            ),
        ),
        worker_id=worker_id,
        lease_ttl_s=lease_ttl_s,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    service_builder: Callable[..., AdaptiveCycle] = build_service,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credential-directory", type=Path, required=True)
    parser.add_argument("--worker-id", default="gauss-adaptive-response-1")
    parser.add_argument("--lease-ttl-seconds", type=float, default=7200.0)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
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
        while True:
            progressed = service.run_once()
            stdout.write(
                json.dumps(
                    {
                        "event": "adaptive_response_cycle_complete",
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
        stderr.write('{"event":"adaptive_response_cycle_failed"}\n')
        stderr.flush()
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
