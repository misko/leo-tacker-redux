"""Read-only PostgreSQL composition for authoritative V16 evidence selectors."""

from __future__ import annotations

from collections.abc import Callable

import psycopg

from leo_flow.contracts.core import (
    CaptureBatchId,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    UtcNs,
)
from leo_flow.contracts.dashboard_batch import (
    CaptureBatchDashboardQueryPortV0_1,
    DashboardCaptureState,
)
from leo_flow.contracts.dashboard_recording import (
    RecordingCaptureDetailQueryPortV0_1,
    RecordingCaptureDetailViewV0_1,
)
from leo_flow.contracts.dashboard_recording_evidence import (
    RecordingEvidenceContextViewV0_1,
    RecordingEvidenceReceiverV0_1,
    RecordingEvidenceRecordingV0_1,
    RecordingEvidenceSegmentV0_1,
)

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresRecordingEvidenceContextRepositoryV0_1:
    """Compose public projections; never infer LNB identity from receiver names."""

    def __init__(
        self,
        connect: ConnectionFactory,
        recording_details: RecordingCaptureDetailQueryPortV0_1,
        capture_batches: CaptureBatchDashboardQueryPortV0_1,
    ) -> None:
        self._connect = connect
        self._recording_details = recording_details
        self._capture_batches = capture_batches

    def recording_evidence_context(
        self, recording_id: RecordingId
    ) -> RecordingEvidenceContextViewV0_1:
        requested = self._recording_details.recording_capture_detail(recording_id)
        limitations: set[str] = set()
        batch_ids = self._batch_ids(recording_id)
        capture_batch_id: CaptureBatchId | None = None
        details = [requested]
        if len(batch_ids) == 1:
            capture_batch_id = batch_ids[0]
            batch = self._capture_batches.capture_batch(capture_batch_id)
            companion_ids = tuple(
                attempt.recording_id
                for attempt in batch.attempts
                if attempt.capture_state is DashboardCaptureState.SUCCEEDED
                and attempt.recording_id is not None
                and attempt.recording_id != recording_id
            )
            for companion_id in companion_ids:
                try:
                    details.append(
                        self._recording_details.recording_capture_detail(companion_id)
                    )
                except LookupError:
                    limitations.add("companion-recording-projection-unavailable")
        elif not batch_ids:
            limitations.add("capture-batch-context-unavailable")
        else:
            limitations.add("recording-resolves-to-multiple-capture-batches")

        details.sort(
            key=lambda item: (item.recording_id != recording_id, str(item.radio_id))
        )
        recordings = tuple(self._recording(item, recording_id) for item in details)
        receivers: list[RecordingEvidenceReceiverV0_1] = []
        segments: list[RecordingEvidenceSegmentV0_1] = []
        for detail in details:
            assignments = self._assignments(detail)
            if not assignments:
                limitations.add(f"hardware-assignment-unresolved:{detail.recording_id}")
            receivers.extend(assignments)
            segments.extend(
                RecordingEvidenceSegmentV0_1(
                    detail.recording_id, segment.segment_id, segment.receiver_chain_ids
                )
                for segment in detail.segments
            )
        return RecordingEvidenceContextViewV0_1(
            SchemaRef(RecordingEvidenceContextViewV0_1.SCHEMA_ID),
            recording_id,
            capture_batch_id,
            recordings,
            tuple(receivers),
            tuple(segments),
            True,
            None,
            (RecordingEvidenceContextViewV0_1.CANDIDATE_WARNING,),
            tuple(sorted(limitations)),
        )

    def _batch_ids(self, recording_id: RecordingId) -> tuple[CaptureBatchId, ...]:
        with self._connect() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            rows = connection.execute(
                """
                SELECT batch_id
                  FROM public.resolve_dashboard_capture_batches_for_recording(
                       %(recording_id)s)
                 ORDER BY batch_id
                """,
                {"recording_id": str(recording_id)},
            ).fetchall()
        return tuple(CaptureBatchId(str(row["batch_id"])) for row in rows)

    def _assignments(
        self, detail: RecordingCaptureDetailViewV0_1
    ) -> tuple[RecordingEvidenceReceiverV0_1, ...]:
        with self._connect() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            rows = connection.execute(
                """
                SELECT chain.receiver_chain_id, chain.radio_id,
                       chain.radio_channel, chain.lnb_id, chain.polarization,
                       chain.valid_from_utc_ns, chain.valid_until_utc_ns
                  FROM public.recording_hardware_link AS link
                  JOIN public.hardware_receiver_chain AS chain
                    ON chain.snapshot_id = link.hardware_snapshot_id
                 WHERE link.recording_id = %(recording_id)s
                   AND link.hardware_snapshot_id = %(hardware_snapshot_id)s
                   AND chain.radio_id = %(radio_id)s
                   AND chain.valid_from_utc_ns <= %(capture_started_utc_ns)s
                   AND (chain.valid_until_utc_ns IS NULL
                        OR %(capture_started_utc_ns)s < chain.valid_until_utc_ns)
                 ORDER BY chain.chain_index
                """,
                {
                    "recording_id": str(detail.recording_id),
                    "hardware_snapshot_id": str(detail.hardware_snapshot_id),
                    "radio_id": str(detail.radio_id),
                    "capture_started_utc_ns": int(detail.capture_started_utc_ns),
                },
            ).fetchall()
        return tuple(
            RecordingEvidenceReceiverV0_1(
                detail.recording_id,
                RadioId(str(row["radio_id"])),
                ReceiverChainId(str(row["receiver_chain_id"])),
                _integer(row["radio_channel"], "radio_channel"),
                str(row["lnb_id"]),
                None if row["polarization"] is None else str(row["polarization"]),
                UtcNs(_integer(row["valid_from_utc_ns"], "valid_from_utc_ns")),
                (
                    None
                    if row["valid_until_utc_ns"] is None
                    else UtcNs(
                        _integer(row["valid_until_utc_ns"], "valid_until_utc_ns")
                    )
                ),
            )
            for row in rows
        )

    @staticmethod
    def _recording(
        detail: RecordingCaptureDetailViewV0_1, requested: RecordingId
    ) -> RecordingEvidenceRecordingV0_1:
        return RecordingEvidenceRecordingV0_1(
            detail.recording_id,
            detail.radio_id,
            detail.radio_serial,
            detail.hardware_snapshot_id,
            detail.capture_started_utc_ns,
            detail.capture_finished_utc_ns,
            detail.analysis_state,
            detail.recording_id == requested,
        )


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"database {name} is not an integer")
    return value
