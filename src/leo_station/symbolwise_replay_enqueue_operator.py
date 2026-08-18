"""Explicit one-recording admission for durable symbolwise replay work."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol, TextIO

from leo_flow.adapters.systemd_credentials import SystemdCredentialProvider
from leo_flow.analysis.recording.starlink_symbolwise_replay_product_codec import (
    decode_starlink_symbolwise_replay_request,
)
from leo_flow.contracts.starlink_symbolwise_replay_product import (
    StarlinkSymbolwiseReplayRequestV0_1,
)
from leo_flow.services.capture_batch_analysis import PublishedRecordingCatalog


class SymbolwiseReplayQueueV0_1(Protocol):
    def enqueue(
        self,
        request: StarlinkSymbolwiseReplayRequestV0_1,
        *,
        priority: int,
        idempotency_key: str,
    ) -> str: ...


def enqueue_exact_request(
    request: StarlinkSymbolwiseReplayRequestV0_1,
    recordings: PublishedRecordingCatalog,
    work: SymbolwiseReplayQueueV0_1,
    *,
    priority: int,
) -> str:
    """Admit one request only when its complete public recording ref is current."""

    published = recordings.get(request.recording_id)
    if published is None or published.recording_object != request.recording_object_ref:
        raise ValueError("symbolwise replay request does not name the exact recording")
    return work.enqueue(
        request,
        priority=priority,
        idempotency_key=f"symbolwise-replay-explicit:{request.digest.value}",
    )


def enqueue_request(
    request: StarlinkSymbolwiseReplayRequestV0_1,
    credential_directory: Path,
    *,
    priority: int,
) -> str:
    from leo_flow.adapters.starlink_symbolwise_replay_postgres import (
        PostgresStarlinkSymbolwiseReplayRepositoryV0_1,
    )
    from leo_flow.deployments.recording_submission_v1 import analysis_connection_factory
    from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog

    credentials = SystemdCredentialProvider(credential_directory)
    connect = analysis_connection_factory(credentials.resolve("catalog-dsn"))
    repository = PostgresStarlinkSymbolwiseReplayRepositoryV0_1(connect)
    return enqueue_exact_request(
        request,
        PostgresRecordingCatalog(connect),
        repository,
        priority=priority,
    )


def _load_request(path: Path) -> StarlinkSymbolwiseReplayRequestV0_1:
    return decode_starlink_symbolwise_replay_request(path.read_bytes())


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    enqueuer: Callable[..., str] = enqueue_request,
) -> int:
    parser = argparse.ArgumentParser(
        description="Validate or explicitly enqueue one exact symbolwise replay request."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser(
        "validate-request",
        help="validate one canonical request without credentials or external access",
    )
    validate.add_argument("--request", type=Path, required=True)
    enqueue = subparsers.add_parser(
        "enqueue-request",
        help="enqueue one exact published recording; never discovers capture work",
    )
    enqueue.add_argument("--request", type=Path, required=True)
    enqueue.add_argument("--credential-directory", type=Path, required=True)
    enqueue.add_argument("--priority", type=int, default=0)
    args = parser.parse_args(argv)
    if not 0 <= getattr(args, "priority", 0) <= 100:
        parser.error("--priority must lie in [0,100]")
    try:
        request = _load_request(args.request)
    except (OSError, ValueError):
        stderr.write('{"event":"symbolwise_replay_request_invalid"}\n')
        stderr.flush()
        return 2
    identity = request.recording_object_ref.identity_digest().value
    if args.command == "validate-request":
        stdout.write(
            json.dumps(
                {
                    "event": "symbolwise_replay_request_valid",
                    "recording_id": str(request.recording_id),
                    "recording_identity_digest": identity,
                    "request_digest": request.digest.value,
                    "stream_count": len(request.stream_selections),
                },
                sort_keys=True,
            )
            + "\n"
        )
        stdout.flush()
        return 0
    try:
        work_id = enqueuer(
            request,
            args.credential_directory,
            priority=args.priority,
        )
    except Exception:  # noqa: BLE001 - sanitized operator boundary
        stderr.write('{"event":"symbolwise_replay_enqueue_failed"}\n')
        stderr.flush()
        return 3
    stdout.write(
        json.dumps(
            {
                "event": "symbolwise_replay_enqueued",
                "recording_id": str(request.recording_id),
                "recording_identity_digest": identity,
                "request_digest": request.digest.value,
                "work_id": work_id,
            },
            sort_keys=True,
        )
        + "\n"
    )
    stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
