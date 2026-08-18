"""Bounded optional operator for prompt complete-IQ timeline products."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol, TextIO

from leo_flow.adapters.full_dwell_timeline_postgres import (
    PostgresFullDwellRefinementDispatchV0_1,
    PostgresFullDwellTimelineCatalogV0_1,
    PostgresFullDwellTimelineWorkRepositoryV0_1,
)
from leo_flow.adapters.systemd_credentials import SystemdCredentialProvider
from leo_flow.analysis.recording.api import AnalysisExecutionContext
from leo_flow.analysis.recording.starlink_full_dwell_timeline_persistence import (
    DurableFullDwellTimelineStoreV0_1,
)
from leo_flow.contracts.core import RecordingId, UtcNs, canonical_digest
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_full_dwell_timeline_product import (
    FullDwellTimelinePlanV0_1,
    FullDwellTimelineStreamSelectionV0_1,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.services.full_dwell_timeline import (
    IndependentFullDwellTimelineServiceV0_1,
)
from leo_flow.services.full_dwell_timeline_pipeline import (
    DurableFullDwellTimelineLeaseProducerV0_1,
)
from leo_flow.storage.filesystem import FileSystemBlobStore
from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog
from leo_flow.storage.recording_codec import SigMFRecordingObjectReader


class TimelineCycle(Protocol):
    def run_cycle(self) -> tuple[int, bool]: ...


class BoundedTimelineCycleV0_1:
    def __init__(
        self,
        work: PostgresFullDwellTimelineWorkRepositoryV0_1,
        recordings: PostgresRecordingCatalog,
        reader: SigMFRecordingObjectReader,
        service: IndependentFullDwellTimelineServiceV0_1,
        *,
        maximum_admissions: int,
        tile_sample_count: int,
        maximum_refinements_per_stream: int,
        recording_ids: tuple[RecordingId, ...] = (),
    ) -> None:
        if not 1 <= maximum_admissions <= 64:
            raise ValueError("timeline admission bound is invalid")
        self._work, self._recordings, self._reader, self._service = (
            work,
            recordings,
            reader,
            service,
        )
        self._maximum_admissions = maximum_admissions
        if len(recording_ids) > 64 or len(recording_ids) != len(set(recording_ids)):
            raise ValueError("timeline recording targets are duplicate or unbounded")
        self._recording_ids = recording_ids
        self._plan = FullDwellTimelinePlanV0_1(
            tile_sample_count,
            maximum_refinements_per_stream=maximum_refinements_per_stream,
        )

    def run_cycle(self) -> tuple[int, bool]:
        admitted = 0
        candidates = (
            self._recording_ids
            if self._recording_ids
            else self._work.newest_candidate_ids(self._maximum_admissions)
        )
        for recording_id in candidates[: self._maximum_admissions]:
            published = self._recordings.get(recording_id)
            if published is None:
                continue
            recording_ref = published.recording_object
            with self._reader.open(recording_ref) as recording:
                capture_started_utc_ns = int(recording.manifest.capture_started_utc_ns)
                hardware = self._work.receiver_lnbs(
                    recording_id, capture_started_utc_ns
                )
                streams: list[FullDwellTimelineStreamSelectionV0_1] = []
                for segment in recording.manifest.segments:
                    tags = dict(segment.requested.tags)
                    channel = tags.get("channel")
                    edge = tags.get("edge")
                    if channel not in (1, 2, 3, 4) or edge not in ("lower", "upper"):
                        continue
                    for receiver in segment.requested.receiver_chain_ids:
                        radio_lnb = hardware.get(receiver)
                        if (
                            radio_lnb is None
                            or radio_lnb[0] != recording.manifest.radio_id
                        ):
                            raise ValueError(
                                "recording manifest and hardware mapping differ"
                            )
                        streams.append(
                            FullDwellTimelineStreamSelectionV0_1(
                                recording.manifest.radio_id,
                                radio_lnb[1],
                                segment.segment_id,
                                receiver,
                                int(channel),
                                StarlinkEdge(str(edge)),
                                segment.actual_sample_rate_hz,
                                segment.sample_count,
                            )
                        )
            streams.sort(key=lambda item: item.identity)
            if not streams:
                continue
            if any(
                math.ceil(stream.segment_sample_count / self._plan.tile_sample_count)
                > self._plan.maximum_window_count_per_stream
                for stream in streams
            ):
                raise ValueError("recording exceeds prompt timeline geometry bound")
            request = _work_request(
                recording_ref,
                capture_started_utc_ns,
                self._plan,
                tuple(streams),
            )
            admitted += int(self._work.admit(recording_ref, request))
        return admitted, self._service.run_once()


def _work_request(
    recording_ref: RecordingObjectRef,
    capture_started_utc_ns: int,
    plan: FullDwellTimelinePlanV0_1,
    streams: tuple[FullDwellTimelineStreamSelectionV0_1, ...],
) -> dict[str, object]:
    recording = {
        "recording_id": str(recording_ref.recording_id),
        "data": _object_json(recording_ref.data_object),
        "metadata": _object_json(recording_ref.metadata_object),
        "manifest_digest": recording_ref.manifest_digest.value,
    }
    body: dict[str, object] = {
        "recording_id": str(recording_ref.recording_id),
        "capture_started_utc_ns": capture_started_utc_ns,
        "recording_ref": recording,
        "plan": {
            "tile_sample_count": plan.tile_sample_count,
            "maximum_window_count_per_stream": plan.maximum_window_count_per_stream,
            "maximum_refinements_per_stream": plan.maximum_refinements_per_stream,
        },
        "streams": [
            {
                "radio_id": str(stream.radio_id),
                "lnb_id": stream.lnb_id,
                "segment_id": str(stream.segment_id),
                "receiver_chain_id": str(stream.receiver_chain_id),
                "channel_number": stream.channel_number,
                "edge": stream.edge.value,
                "sample_rate_hz": stream.sample_rate_hz,
                "segment_sample_count": stream.segment_sample_count,
            }
            for stream in streams
        ],
    }
    body["work_id"] = f"fdtlw_{canonical_digest(body).value[:32]}"
    return body


def _object_json(ref: ObjectRef) -> dict[str, object]:
    return {
        "digest": ref.digest.value,
        "byte_count": ref.byte_count,
        "media_type": ref.media_type,
        "format_id": ref.format_id,
        "locator": ref.locator,
    }


def build_cycle(
    credential_directory: Path,
    *,
    worker_id: str,
    maximum_admissions: int,
    tile_sample_count: int,
    maximum_refinements_per_stream: int,
    recording_ids: tuple[RecordingId, ...] = (),
) -> BoundedTimelineCycleV0_1:
    from leo_flow.deployments.recording_submission_v1 import analysis_connection_factory
    from leo_station.analysis_v1 import (
        CAS_ROOT,
        ENVIRONMENT_REF,
        SOURCE_COMMIT,
        SOURCE_COMMIT_UTC_NS,
    )

    credentials = SystemdCredentialProvider(credential_directory)
    connect = analysis_connection_factory(credentials.resolve("catalog-dsn"))
    blobs = FileSystemBlobStore(CAS_ROOT)
    recordings = PostgresRecordingCatalog(connect)
    reader = SigMFRecordingObjectReader(blobs)
    work = PostgresFullDwellTimelineWorkRepositoryV0_1(connect)
    execution = AnalysisExecutionContext(
        "leo-flow-gauss-prompt-full-dwell-timeline",
        "0.1.0",
        SOURCE_COMMIT,
        ENVIRONMENT_REF.digest,
        UtcNs(int(SOURCE_COMMIT_UTC_NS)),
        UtcNs(int(SOURCE_COMMIT_UTC_NS)),
        "gauss-x86_64-python31116",
    )
    catalog = PostgresFullDwellTimelineCatalogV0_1(connect)
    service = IndependentFullDwellTimelineServiceV0_1(
        work,
        DurableFullDwellTimelineLeaseProducerV0_1(
            reader,
            DurableFullDwellTimelineStoreV0_1(blobs, catalog),
            execution,
        ),
        PostgresFullDwellRefinementDispatchV0_1(connect),
        worker_id=worker_id,
    )
    return BoundedTimelineCycleV0_1(
        work,
        recordings,
        reader,
        service,
        maximum_admissions=maximum_admissions,
        tile_sample_count=tile_sample_count,
        maximum_refinements_per_stream=maximum_refinements_per_stream,
        recording_ids=recording_ids,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    cycle_builder: Callable[..., TimelineCycle] = build_cycle,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credential-directory", type=Path, required=True)
    parser.add_argument("--worker-id", default="gauss-prompt-timeline-1")
    parser.add_argument("--maximum-admissions", type=int, default=2)
    parser.add_argument("--tile-sample-count", type=int, default=20_000)
    parser.add_argument("--maximum-refinements-per-stream", type=int, default=32)
    parser.add_argument("--recording-id", action="append", default=[])
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if not 0.1 <= args.poll_seconds <= 300:
        parser.error("--poll-seconds must lie in [0.1,300]")
    if len(args.recording_id) > 64 or len(args.recording_id) != len(
        set(args.recording_id)
    ):
        parser.error("--recording-id targets are duplicate or unbounded")
    try:
        cycle = cycle_builder(
            args.credential_directory,
            worker_id=args.worker_id,
            maximum_admissions=args.maximum_admissions,
            tile_sample_count=args.tile_sample_count,
            maximum_refinements_per_stream=args.maximum_refinements_per_stream,
            recording_ids=tuple(RecordingId(value) for value in args.recording_id),
        )
        while True:
            admitted, processed = cycle.run_cycle()
            stdout.write(
                json.dumps(
                    {
                        "event": "prompt_full_dwell_timeline_cycle_complete",
                        "admitted": admitted,
                        "processed": processed,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            stdout.flush()
            if args.once:
                return 0
            if not processed:
                sleeper(args.poll_seconds)
    except Exception:  # noqa: BLE001 - sanitized process boundary
        stderr.write('{"event":"prompt_full_dwell_timeline_cycle_failed"}\n')
        stderr.flush()
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
