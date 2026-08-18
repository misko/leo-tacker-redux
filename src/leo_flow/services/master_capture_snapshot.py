"""Application service for the immutable dashboard capture snapshot."""

from __future__ import annotations

from leo_flow.contracts.dashboard_master_capture import (
    MasterCaptureSnapshotQueryPortV0_1,
    MasterCaptureSnapshotQueryV0_1,
    MasterCaptureSnapshotV0_1,
)


class MasterCaptureSnapshotQueryServiceV0_1:
    """Keep the HTTP boundary dependent on one narrow snapshot port."""

    def __init__(self, repository: MasterCaptureSnapshotQueryPortV0_1) -> None:
        self._repository = repository

    def master_capture_snapshot(
        self, query: MasterCaptureSnapshotQueryV0_1, cursor: str | None = None
    ) -> MasterCaptureSnapshotV0_1:
        view = self._repository.master_capture_snapshot(query, cursor)
        if (
            view.start_utc_ns != query.start_utc_ns
            or view.stop_utc_ns != query.stop_utc_ns
        ):
            raise RuntimeError("master capture repository returned another interval")
        return view
