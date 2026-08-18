from unittest.mock import Mock

import pytest

from leo_flow.adapters.dashboard_postgres import PostgresDashboardRepository
from leo_flow.contracts.core import RecordingId
from leo_flow.contracts.starlink_full_dwell_response import StarlinkFullDwellQueryV0_1
from leo_flow.dashboard import DashboardNotFound


def _never_connect():
    raise AssertionError("delegation must not open an unrelated dashboard connection")


def test_postgres_dashboard_delegates_exact_full_dwell_query() -> None:
    port = Mock()
    query = StarlinkFullDwellQueryV0_1(RecordingId("rec_fd"), maximum_points=32)
    expected = object()
    port.recording_starlink_full_dwell.return_value = expected
    repository = PostgresDashboardRepository(_never_connect, full_dwell=port)
    assert repository.recording_starlink_full_dwell(query) is expected
    port.recording_starlink_full_dwell.assert_called_once_with(query)


def test_postgres_dashboard_translates_missing_full_dwell_product() -> None:
    port = Mock()
    port.recording_starlink_full_dwell.side_effect = LookupError("missing")
    repository = PostgresDashboardRepository(_never_connect, full_dwell=port)
    with pytest.raises(DashboardNotFound, match="was not found"):
        repository.recording_starlink_full_dwell(
            StarlinkFullDwellQueryV0_1(RecordingId("rec_fd"))
        )
