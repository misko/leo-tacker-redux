from __future__ import annotations

import pytest

from leo_flow.adapters.dashboard_postgres import PostgresDashboardRepository
from leo_flow.analysis.recording.starlink_surrogate_null_persistence import (
    StarlinkSurrogateNullNotFoundError,
)
from leo_flow.contracts.core import RecordingId
from leo_flow.contracts.starlink_detector_suite import StarlinkDetectorMethod
from leo_flow.contracts.starlink_surrogate_null_pipeline import (
    RecordingStarlinkSurrogateNullViewV0_1,
    StarlinkSurrogateNullQueryV0_1,
)
from leo_flow.dashboard import DashboardNotFound
from leo_flow.dashboard.api import (
    DashboardJsonApplicationV10,
    JsonRequest,
    JsonResponse,
)
from tests.dashboard.test_starlink_surrogate_null_api import view


def _query() -> StarlinkSurrogateNullQueryV0_1:
    return StarlinkSurrogateNullQueryV0_1(
        RecordingId("rec_surrogate_adapter"),
        methods=(StarlinkDetectorMethod.GLRT_32,),
        maximum_rows=8,
    )


def _never_connect():
    raise AssertionError("delegation must not open an unrelated dashboard connection")


class _Projection:
    def __init__(
        self,
        result: RecordingStarlinkSurrogateNullViewV0_1 | Exception,
    ) -> None:
        self.result = result
        self.calls: list[StarlinkSurrogateNullQueryV0_1] = []

    def recording_starlink_surrogate_null(
        self, query: StarlinkSurrogateNullQueryV0_1
    ) -> RecordingStarlinkSurrogateNullViewV0_1:
        self.calls.append(query)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_postgres_dashboard_delegates_the_exact_bounded_query() -> None:
    query = _query()
    expected = view(query)
    projection = _Projection(expected)
    repository = PostgresDashboardRepository(
        _never_connect,
        surrogate_nulls=projection,
    )

    actual = repository.recording_starlink_surrogate_null(query)

    assert actual is expected
    assert projection.calls == [query]


def test_postgres_dashboard_translates_durable_not_found_to_dashboard_404() -> None:
    query = _query()
    projection = _Projection(StarlinkSurrogateNullNotFoundError("product is absent"))
    repository = PostgresDashboardRepository(
        _never_connect,
        surrogate_nulls=projection,
    )

    with pytest.raises(DashboardNotFound, match="was not found"):
        repository.recording_starlink_surrogate_null(query)

    assert projection.calls == [query]


def test_postgres_dashboard_without_a_surrogate_reader_is_explicitly_unavailable() -> (
    None
):
    repository = PostgresDashboardRepository(_never_connect)

    with pytest.raises(DashboardNotFound, match="is unavailable"):
        repository.recording_starlink_surrogate_null(_query())


def test_durable_not_found_reaches_the_v10_http_boundary_as_404() -> None:
    class _V9:
        def handle(self, request: JsonRequest) -> JsonResponse:
            return JsonResponse(299, (), request.path.encode())

    projection = _Projection(StarlinkSurrogateNullNotFoundError("product is absent"))
    repository = PostgresDashboardRepository(
        _never_connect,
        surrogate_nulls=projection,
    )
    application = DashboardJsonApplicationV10(_V9(), repository)  # type: ignore[arg-type]

    response = application.handle(
        JsonRequest(
            "GET",
            "/api/v10/recordings/rec_surrogate_adapter/starlink-surrogate-null",
            {"methods": "glrt-32", "maximum_rows": "8"},
        )
    )

    assert response.status == 404
    assert b'"code":"not_found"' in response.body
