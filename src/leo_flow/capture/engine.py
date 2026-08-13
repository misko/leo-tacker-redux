"""Declarative capture-plan execution with no analysis knowledge."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from leo_flow.contracts.capture import (
    ActivityManifest,
    CapturePlan,
    CompletedLocalRecording,
    RecordingManifest,
)
from leo_flow.contracts.core import (
    HardwareSnapshotId,
    SchemaRef,
    StationId,
    UtcNs,
    canonical_digest,
)
from leo_flow.contracts.ports import LocalSpool, RadioDevice
from leo_flow.storage.ports import RecordingWriter, RecordingWriteSession

from .clock import CaptureClock, SystemCaptureClock
from .errors import RadioConfigurationError, WriterIdentityError

# Frozen-port amendment requested for the next contract revision:
# RecordingWriter.begin(recording_id, plan, hardware_metadata_snapshot_id, destination)
# should receive the ID allocated by LocalSpool explicitly. v0.1 requires the
# composition root to coordinate writer/session identity, checked below.


@dataclass(frozen=True)
class CaptureIdentity:
    station_id: StationId
    radio_serial: str
    clock_status: str
    hardware_metadata_snapshot_id: HardwareSnapshotId
    producer: str

    def __post_init__(self) -> None:
        if not all((self.radio_serial, self.clock_status, self.producer)):
            raise ValueError("capture identity strings cannot be empty")


class PlanCaptureEngine:
    def __init__(
        self,
        identity: CaptureIdentity,
        *,
        clock: CaptureClock | None = None,
        delay: Callable[[float], None] = time.sleep,
    ) -> None:
        self._identity = identity
        self._clock = clock or SystemCaptureClock()
        self._delay = delay

    def execute(
        self,
        plan: CapturePlan,
        hardware: RadioDevice,
        writer: RecordingWriter,
        spool: LocalSpool,
    ) -> CompletedLocalRecording:
        if hardware.radio_id != plan.radio_id:
            raise RadioConfigurationError("plan and attached radio IDs differ")

        created_utc_ns = UtcNs(self._clock.now_utc_ns())
        recording_id, destination = spool.allocate(plan.plan_id)
        session: RecordingWriteSession | None = None
        try:
            session = writer.begin(
                plan, self._identity.hardware_metadata_snapshot_id, destination
            )
            if session.recording_id != recording_id:
                raise WriterIdentityError(
                    "writer session ID differs from the local spool allocation"
                )
            capture_started_utc_ns = UtcNs(self._clock.now_utc_ns())
            activities: list[ActivityManifest] = []
            segments = []
            previous_activity_finished = int(capture_started_utc_ns)
            for activity in plan.activities:
                activity_started = UtcNs(
                    max(self._clock.now_utc_ns(), previous_activity_finished)
                )
                segment_ids = []
                for request in activity.segments:
                    self._wait_until(request.scheduled_utc_ns)
                    segment = hardware.acquire_segment(
                        request,
                        lambda data, segment_id=request.segment_id: session.append_iq(
                            segment_id, data
                        ),
                    )
                    if segment.requested != request:
                        raise RadioConfigurationError(
                            "radio returned a manifest for different requested settings"
                        )
                    session.finish_segment(segment)
                    segments.append(segment)
                    segment_ids.append(segment.segment_id)
                activity_finished = UtcNs(
                    max(self._clock.now_utc_ns(), int(activity_started) + 1)
                )
                activities.append(
                    ActivityManifest(
                        activity.activity_id,
                        activity.kind,
                        activity_started,
                        activity_finished,
                        tuple(segment_ids),
                    )
                )
                previous_activity_finished = int(activity_finished)
            capture_finished_utc_ns = UtcNs(
                max(
                    self._clock.now_utc_ns(),
                    int(capture_started_utc_ns) + 1,
                    previous_activity_finished,
                )
            )
            manifest = RecordingManifest(
                schema=SchemaRef(RecordingManifest.SCHEMA_ID),
                recording_id=recording_id,
                created_utc_ns=created_utc_ns,
                capture_started_utc_ns=capture_started_utc_ns,
                capture_finished_utc_ns=capture_finished_utc_ns,
                station_id=self._identity.station_id,
                radio_id=plan.radio_id,
                radio_serial=self._identity.radio_serial,
                receiver_chain_ids=plan.receiver_chain_ids,
                clock_status=self._identity.clock_status,
                hardware_metadata_snapshot_id=self._identity.hardware_metadata_snapshot_id,
                activities=tuple(activities),
                segments=tuple(segments),
                plan_id=plan.plan_id,
                producer=self._identity.producer,
                experiment_tags=plan.experiment_tags,
            )
            completed = session.finalize(manifest)
            if completed.recording_id != recording_id or completed.manifest != manifest:
                raise WriterIdentityError(
                    "writer changed recording identity or manifest"
                )
            if completed.manifest_digest != canonical_digest(manifest):
                raise WriterIdentityError("writer returned the wrong manifest digest")
            spool.record_complete(completed)
            return completed
        except Exception as error:
            if session is not None:
                session.abort(str(error))
            spool.record_failure(recording_id, f"{type(error).__name__}: {error}")
            raise

    def _wait_until(self, scheduled_utc_ns: UtcNs | None) -> None:
        if scheduled_utc_ns is None:
            return
        remaining_ns = int(scheduled_utc_ns) - self._clock.now_utc_ns()
        if remaining_ns > 0:
            self._delay(remaining_ns / 1_000_000_000)
