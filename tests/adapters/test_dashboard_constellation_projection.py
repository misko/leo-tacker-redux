from __future__ import annotations

import pytest

from leo_flow.adapters.dashboard_postgres import PostgresDashboardRepository
from leo_flow.analysis.recording.starlink_pilot_constellation_persistence import (
    StarlinkPilotConstellationNotFoundError,
)
from leo_flow.contracts.core import RecordingId
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_pilot_constellation_pipeline import (
    RecordingStarlinkPilotConstellationViewV0_1,
    StarlinkPilotConstellationQueryV0_1,
)
from leo_flow.dashboard import DashboardNotFound
from leo_flow.dashboard.api import (
    DashboardJsonApplicationV11,
    JsonRequest,
    JsonResponse,
)
from tests.dashboard.test_starlink_pilot_constellation_api import view


def _query() -> StarlinkPilotConstellationQueryV0_1:
    return StarlinkPilotConstellationQueryV0_1(
        RecordingId("rec_constellation_adapter"),
        edges=(StarlinkEdge.LOWER,),
        maximum_streams=1,
        maximum_points_per_stream=600,
    )


def _never_connect():
    raise AssertionError("delegation must not open an unrelated dashboard connection")


class _Projection:
    def __init__(
        self,
        result: RecordingStarlinkPilotConstellationViewV0_1 | Exception,
    ) -> None:
        self.result = result
        self.calls: list[StarlinkPilotConstellationQueryV0_1] = []

    def recording_starlink_pilot_constellation(
        self, query: StarlinkPilotConstellationQueryV0_1
    ) -> RecordingStarlinkPilotConstellationViewV0_1:
        self.calls.append(query)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_postgres_dashboard_delegates_exact_bounded_constellation_query() -> None:
    query = _query()
    expected = view(query)
    projection = _Projection(expected)
    repository = PostgresDashboardRepository(
        _never_connect,
        pilot_constellations=projection,
    )

    actual = repository.recording_starlink_pilot_constellation(query)

    assert actual is expected
    assert projection.calls == [query]


def test_postgres_dashboard_translates_durable_constellation_not_found() -> None:
    query = _query()
    projection = _Projection(
        StarlinkPilotConstellationNotFoundError("product is absent")
    )
    repository = PostgresDashboardRepository(
        _never_connect,
        pilot_constellations=projection,
    )

    with pytest.raises(DashboardNotFound, match="was not found"):
        repository.recording_starlink_pilot_constellation(query)

    assert projection.calls == [query]


def test_postgres_dashboard_without_constellation_reader_is_unavailable() -> None:
    repository = PostgresDashboardRepository(_never_connect)

    with pytest.raises(DashboardNotFound, match="is unavailable"):
        repository.recording_starlink_pilot_constellation(_query())


def test_durable_constellation_not_found_reaches_v11_as_404() -> None:
    class _V10:
        def handle(self, request: JsonRequest) -> JsonResponse:
            return JsonResponse(299, (), request.path.encode())

    projection = _Projection(
        StarlinkPilotConstellationNotFoundError("product is absent")
    )
    repository = PostgresDashboardRepository(
        _never_connect,
        pilot_constellations=projection,
    )
    application = DashboardJsonApplicationV11(_V10(), repository)  # type: ignore[arg-type]

    response = application.handle(
        JsonRequest(
            "GET",
            "/api/v11/recordings/rec_constellation_adapter/starlink-pilot-constellation",
            {"edges": "lower", "maximum_streams": "1"},
        )
    )

    assert response.status == 404
    assert b'"code":"not_found"' in response.body
