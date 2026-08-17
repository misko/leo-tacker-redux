"""Resume exact analysis for one already-terminal focused capture batch."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import time
from pathlib import Path

from leo_flow.adapters.capture_batch_sqlite import SQLiteCaptureBatchStateStore
from leo_flow.contracts.core import (
    CaptureBatchId,
    Digest,
    DigestAlgorithm,
    SchemaRef,
    UtcNs,
)
from leo_flow.contracts.focused_analysis_completion import (
    FocusedAnalysisCompletionV0_1,
    decode_focused_analysis_completion,
    encode_focused_analysis_completion,
)
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
    parser.add_argument("--completion-receipt", type=Path)
    parser.add_argument("--analysis-attempt-lock", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if (
        not args.arm
        or not args.batch_database.is_absolute()
        or ".." in args.batch_database.parts
        or not args.batch_database.is_file()
        or (
            args.completion_receipt is not None
            and (
                not args.completion_receipt.is_absolute()
                or ".." in args.completion_receipt.parts
            )
        )
        or (
            args.analysis_attempt_lock is not None
            and (
                not args.analysis_attempt_lock.is_absolute()
                or ".." in args.analysis_attempt_lock.parts
            )
        )
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
    if (
        args.capture_safe or args.completion_receipt is not None
    ) and definition_digest is None:
        return 3
    if (args.completion_receipt is None) != (args.analysis_attempt_lock is None):
        return 3
    lock_descriptor: int | None = None
    if args.analysis_attempt_lock is not None:
        args.analysis_attempt_lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_descriptor = os.open(
            args.analysis_attempt_lock,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        if not stat.S_ISREG(os.fstat(lock_descriptor).st_mode):
            os.close(lock_descriptor)
            return 3
        # Serialize the fork-to-journal crash window with any recovery child.
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
    snapshot = SQLiteCaptureBatchStateStore(args.batch_database).get(
        CaptureBatchId(args.batch_id)
    )
    if (
        snapshot is None
        or not snapshot.terminal
        or len(snapshot.successful_recordings) != 2
    ):
        if lock_descriptor is not None:
            os.close(lock_descriptor)
        return 4
    if args.completion_receipt is not None and args.completion_receipt.exists():
        try:
            existing = decode_focused_analysis_completion(
                args.completion_receipt.read_bytes()
            )
        except (OSError, ValueError):
            if lock_descriptor is not None:
                os.close(lock_descriptor)
            return 4
        ordered = tuple(
            sorted(
                snapshot.successful_recordings, key=lambda item: str(item.recording_id)
            )
        )
        if (
            existing.batch_id != snapshot.batch_id
            or existing.capture_definition_digest != definition_digest
            or existing.recording_ids != tuple(item.recording_id for item in ordered)
            or existing.recording_identity_digests
            != tuple(item.recording_object.identity_digest() for item in ordered)
        ):
            if lock_descriptor is not None:
                os.close(lock_descriptor)
            return 4
        if lock_descriptor is not None:
            os.close(lock_descriptor)
        return 0
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
    if args.completion_receipt is not None:
        assert definition_digest is not None
        by_recording = {
            item.recording_id: item.recording_object.identity_digest()
            for item in snapshot.successful_recordings
        }
        completion = FocusedAnalysisCompletionV0_1(
            SchemaRef(FocusedAnalysisCompletionV0_1.SCHEMA_ID),
            receipt.batch_id,
            definition_digest,
            receipt.recording_ids,
            tuple(by_recording[item] for item in receipt.recording_ids),  # type: ignore[arg-type]
            (
                receipt.successes[0].analysis_job_id,
                receipt.successes[1].analysis_job_id,
            ),
            (
                receipt.successes[0].result_ref.digest,
                receipt.successes[1].result_ref.digest,
            ),
            receipt.completed_utc_ns,
        )
        _write_completion(args.completion_receipt, completion)
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
    if lock_descriptor is not None:
        os.close(lock_descriptor)
    return 0


def _write_completion(path: Path, completion: FocusedAnalysisCompletionV0_1) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encode_focused_analysis_completion(completion))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            try:
                existing = decode_focused_analysis_completion(path.read_bytes())
            except (OSError, ValueError) as error:
                raise RuntimeError(
                    "existing focused completion receipt is invalid"
                ) from error
            if existing != completion:
                raise RuntimeError("focused completion receipt identity conflict")
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
