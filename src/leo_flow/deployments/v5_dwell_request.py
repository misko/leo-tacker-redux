"""One-shot scheduler for a durable, capture-gated V5 dwell request."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from leo_flow.capture.publication import PublicationReconciler
from leo_flow.capture.spool import SpoolState, SQLiteLocalSpool
from leo_flow.contracts.capture import ActivityKind, CapturePlan
from leo_flow.contracts.core import Digest, PlanId, RecordingId, UtcNs, canonical_digest
from leo_flow.contracts.dwell import DwellRequest
from leo_flow.contracts.ports import CaptureEngine, DwellRequestGatePort, RadioDevice
from leo_flow.storage.ports import RecordingWriter


class _RadioProvider(Protocol):
    def open(self) -> RadioDevice: ...


class DwellCaptureScheduleError(RuntimeError):
    """A gated dwell could not make safe, durable forward progress."""


@dataclass(frozen=True)
class DwellCaptureReceipt:
    plan_id: PlanId
    plan_digest: Digest
    recording_id: RecordingId
    captured_now: bool
    spool_state: SpoolState
    published_now: int
    cleaned_now: int


class OneShotDwellCaptureScheduler:
    """Gate, capture, publish, and safely replay one request without a new queue."""

    def __init__(
        self,
        gate: DwellRequestGatePort,
        radio_provider: _RadioProvider,
        engine: CaptureEngine,
        writer: RecordingWriter,
        spool: SQLiteLocalSpool,
        reconciler: PublicationReconciler,
    ) -> None:
        self._gate = gate
        self._radio_provider = radio_provider
        self._engine = engine
        self._writer = writer
        self._spool = spool
        self._reconciler = reconciler

    def run(self, request: DwellRequest, now_utc_ns: UtcNs) -> DwellCaptureReceipt:
        plan = self._gate.accept(request, now_utc_ns)
        _require_receive_only_dwell(plan)
        durable = self._spool.durable_recording_for_plan(plan.plan_id)
        captured_now = False
        if durable is None:
            radio = self._radio_provider.open()
            try:
                completed = self._engine.execute(plan, radio, self._writer, self._spool)
            finally:
                close = getattr(radio, "close", None)
                if callable(close):
                    close()
            captured_now = True
            recording_id = completed.recording_id
        else:
            recording_id = durable.recording_id

        result = self._reconciler.reconcile()
        if result.deferred:
            kinds = ",".join(
                sorted({item.split(":", 2)[1] for item in result.errors if ":" in item})
            )
            raise DwellCaptureScheduleError(
                f"dwell publication remains deferred ({kinds or 'unknown'})"
            )
        final = self._spool.durable_recording_for_plan(plan.plan_id)
        if final is None or final.recording_id != recording_id:
            raise DwellCaptureScheduleError(
                "dwell durable recording receipt disappeared"
            )
        return DwellCaptureReceipt(
            plan.plan_id,
            canonical_digest(plan),
            recording_id,
            captured_now,
            final.state,
            result.published,
            result.cleaned,
        )


def _require_receive_only_dwell(plan: CapturePlan) -> None:
    if len(plan.activities) != 1 or plan.activities[0].kind is not ActivityKind.DWELL:
        raise DwellCaptureScheduleError("accepted plan is not one dwell activity")
    segments = plan.activities[0].segments
    if len(segments) != 1:
        raise DwellCaptureScheduleError("accepted dwell is not one bounded segment")
    segment = segments[0]
    if (
        segment.sample_count is None
        or segment.duration_s is not None
        or segment.scheduled_utc_ns is not None
        or segment.hardware_controls
        or dict(segment.tags).get("tx") != "prohibited"
    ):
        raise DwellCaptureScheduleError("accepted dwell is not bounded receive-only")
