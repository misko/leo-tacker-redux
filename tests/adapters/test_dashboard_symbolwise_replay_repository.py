from __future__ import annotations

from typing import Any

import pytest

from leo_flow.adapters.dashboard_postgres import PostgresDashboardRepository
from leo_flow.contracts.core import RecordingId
from leo_flow.contracts.dashboard_symbolwise_replay import (
    RecordingSymbolwiseReplayDashboardQueryV0_1,
)
from leo_flow.dashboard import DashboardNotFound
from leo_flow.dashboard.symbolwise_replay import (
    RecordingSymbolwiseReplayDashboardProjectionV0_1,
)


def _never_connect() -> Any:
    raise AssertionError("focused repository test must not connect to PostgreSQL")


class _DurableSource:
    def recording_starlink_symbolwise_replay(self, query):  # type: ignore[no-untyped-def]
        del query
        raise LookupError("durable product absent")


def test_repository_composes_durable_replay_with_its_authoritative_context() -> None:
    source = _DurableSource()
    repository = PostgresDashboardRepository(
        _never_connect, symbolwise_replays=source
    )

    projection = repository._symbolwise_replay
    assert isinstance(projection, RecordingSymbolwiseReplayDashboardProjectionV0_1)
    assert projection._replay is source
    assert projection._evidence_context is repository._recording_evidence


def test_repository_maps_absent_and_lookup_failures_to_dashboard_not_found() -> None:
    query = RecordingSymbolwiseReplayDashboardQueryV0_1(
        RecordingId("rec_symbolwise_repository")
    )
    unavailable = PostgresDashboardRepository(_never_connect)
    with pytest.raises(DashboardNotFound, match="is unavailable"):
        unavailable.recording_symbolwise_replay_dashboard(query)

    class MissingProjection:
        def recording_symbolwise_replay_dashboard(self, observed):  # type: ignore[no-untyped-def]
            assert observed is query
            raise LookupError("catalog has no product")

    unavailable._symbolwise_replay = MissingProjection()
    with pytest.raises(DashboardNotFound, match="was not found") as raised:
        unavailable.recording_symbolwise_replay_dashboard(query)
    assert isinstance(raised.value.__cause__, LookupError)
