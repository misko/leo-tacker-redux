"""Hardware-free executable rehearsal of the V5 canary restart state machine."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Sequence
from contextlib import nullcontext
from pathlib import Path
from typing import Any, TextIO

from leo_flow.capture.fake_radio import FakeV5PairedRadio, V5Refill
from leo_flow.contracts.capture import CompletedLocalRecording, LocalObjectRef
from leo_flow.contracts.continuity import CaptureProvenance, RefillMetadata
from leo_flow.contracts.core import Digest, RecordingId
from leo_flow.contracts.storage import (
    ObjectRef,
    PublishedRecordingRef,
    RecordingObjectRef,
)
from leo_flow.storage.local_recording import RootedSigMFRecordingStore
from leo_flow.storage.recording_codec import SigMFRecordingWriter

from . import v5_canary


class _DryRunPublisher:
    def __init__(self) -> None:
        self.calls: list[tuple[RecordingId, str]] = []

    def preflight(self) -> None:
        return

    def publish(
        self, recording: CompletedLocalRecording, *, idempotency_key: str
    ) -> PublishedRecordingRef:
        self.calls.append((recording.recording_id, idempotency_key))
        return PublishedRecordingRef(
            RecordingObjectRef(
                recording.recording_id,
                _published(recording.data_object, "leo-recording-data-v1"),
                _published(recording.metadata_object, "leo-recording-metadata-v1"),
                recording.manifest_digest,
            )
        )


class _PublicationProvider:
    def __init__(self, publisher: _DryRunPublisher) -> None:
        self._publisher = publisher

    def build(self, local: RootedSigMFRecordingStore) -> _DryRunPublisher:
        del local
        return self._publisher


class _RadioProvider:
    def __init__(self, radio: FakeV5PairedRadio | None) -> None:
        self._radio = radio
        self.opens = 0

    def open(self) -> Any:
        self.opens += 1
        if self._radio is None:
            raise RuntimeError("dry-run restart attempted to reopen the radio")
        return self._radio


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    parser = argparse.ArgumentParser(
        prog="leo-v5-canary-dry-run",
        description="Rehearse V5 capture durability with synthetic IQ and no external I/O.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        help="empty scratch directory to retain for inspection (default: temporary)",
    )
    args = parser.parse_args(argv)
    owner = (
        tempfile.TemporaryDirectory(prefix="leo-v5-canary-dry-run-")
        if args.root is None
        else nullcontext(str(args.root))
    )
    try:
        with owner as supplied:
            root = Path(supplied)
            _require_empty_root(root)
            result = _rehearse(root)
    except Exception as error:  # noqa: BLE001 - dry-run process boundary
        stderr.write(
            json.dumps(
                {
                    "event": "v5_canary_dry_run",
                    "status": "failed",
                    "detail": type(error).__name__,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        return 1
    stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


def _rehearse(root: Path) -> dict[str, object]:
    state = root / "state"
    recordings = state / "recordings"
    spool = v5_canary._SpoolSpec(state / "spool.sqlite3", recordings)
    publisher = _DryRunPublisher()
    radio_provider = _RadioProvider(_radio())
    first = _cycle(root, spool, radio_provider, publisher)
    first.preflight()
    if not first.capture_and_publish_once():
        raise RuntimeError("dry-run capture completed no unit")
    first.close(1.0)

    restart_provider = _RadioProvider(None)
    restart = _cycle(root, spool, restart_provider, publisher)
    restart.preflight()
    if restart.capture_and_publish_once():
        raise RuntimeError("dry-run restart repeated durable work")
    restart.close(1.0)
    if radio_provider.opens != 1 or restart_provider.opens != 0:
        raise RuntimeError("dry-run radio admission count differs")
    if len(publisher.calls) != 1:
        raise RuntimeError("dry-run publication count differs")
    return {
        "event": "v5_canary_dry_run",
        "status": "pass",
        "activity_kind": v5_canary.CANARY_PLAN.activities[0].kind.value,
        "continuity_policy": v5_canary.RADIO_CONFIG.continuity_policy.value,
        "plan_digest": str(v5_canary.CANARY_PLAN_DIGEST),
        "capture_admissions": radio_provider.opens,
        "restart_capture_admissions": restart_provider.opens,
        "publications": len(publisher.calls),
    }


def _cycle(
    root: Path,
    spool: v5_canary._SpoolSpec,
    radio: _RadioProvider,
    publisher: _DryRunPublisher,
) -> v5_canary.OneShotV5CanaryCycle:
    guard = v5_canary.CaptureHostGuard(
        root / "run" / "instance.lock",
        (spool.recording_root, root / "cas"),
        1,
    )
    return v5_canary.OneShotV5CanaryCycle(
        v5_canary.ExactCanaryPlanSource(),
        radio,
        guard,
        SigMFRecordingWriter(),
        spool,
        _PublicationProvider(publisher),
    )


def _radio() -> FakeV5PairedRadio:
    request = v5_canary.CANARY_PLAN.activities[0].segments[0]
    target_samples = request.sample_count
    if target_samples is None:
        raise RuntimeError("dry-run plan must have an exact sample count")
    block_samples = v5_canary.RADIO_CONFIG.block_samples
    if target_samples % block_samples:
        raise RuntimeError("dry-run plan must contain whole V5 refills")
    payload = bytes(block_samples * 8)
    refills = tuple(
        V5Refill(
            payload,
            RefillMetadata(
                refill_index=index,
                segment_sample_offset=index * block_samples,
                sample_count=block_samples,
                stream_id=1,
                buffer_sequence=index + 1,
                first_sample_sequence=1 + index * block_samples,
                monotonic_start_ns=1 + index * 125_829_181,
                monotonic_end_ns=2 + index * 125_829_181,
                utc_start_ns=1_700_000_000_000_000_000 + index * 125_829_181,
                utc_end_ns=1_700_000_000_000_000_001 + index * 125_829_181,
                time_uncertainty_ns=1,
                gain_db_start=(0.0, 0.0),
                gain_db_end=(0.0, 0.0),
                rssi_db_start=(-1.0, -1.0),
                rssi_db_end=(-1.0, -1.0),
            ),
        )
        for index in range(target_samples // block_samples)
    )
    return FakeV5PairedRadio(
        v5_canary.RADIO_ID,
        v5_canary.RECEIVER_CHAINS,
        {request.segment_id: refills},
        CaptureProvenance(
            "dry-run-v5", "dry-run", "0.25", "spf-radio-metadata-v3", "metadata=1"
        ),
        continuity_policy=v5_canary.RADIO_CONFIG.continuity_policy,
    )


def _published(local: LocalObjectRef, format_id: str) -> ObjectRef:
    return ObjectRef(
        Digest(local.digest.algorithm, local.digest.value),
        local.byte_count,
        "application/octet-stream",
        format_id,
        f"dry-run:sha256:{local.digest.value}",
    )


def _require_empty_root(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or not root.is_dir() or any(root.iterdir()):
        raise ValueError("dry-run root must be an empty real directory")


if __name__ == "__main__":
    raise SystemExit(main())
