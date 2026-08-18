from __future__ import annotations

import json

import pytest

from leo_flow.dashboard.api import (
    PUBLIC_API_ROUTE_ALIASES,
    DashboardPublicJsonApplication,
    JsonRequest,
    JsonResponse,
)


class _CapturingVersionedApplication:
    def __init__(self) -> None:
        self.requests: list[JsonRequest] = []

    def handle(self, request: JsonRequest) -> JsonResponse:
        self.requests.append(request)
        return JsonResponse(299, (), request.path.encode())


def _concrete(template: str) -> str:
    return (
        template.replace("{capture_batch_id}", "cbatch_a")
        .replace("{capture_attempt_id}", "cattempt_a")
        .replace("{recording_id}", "rec_a")
    )


@pytest.mark.parametrize(
    ("public_template", "internal_template"), PUBLIC_API_ROUTE_ALIASES.items()
)
def test_public_route_aliases_adapt_to_immutable_versioned_handlers(
    public_template: str, internal_template: str
) -> None:
    versioned = _CapturingVersionedApplication()
    application = DashboardPublicJsonApplication(versioned)
    query = {"start_utc_ns": "1"}

    response = application.handle(JsonRequest("GET", _concrete(public_template), query))

    assert response.status == 299
    assert response.body.decode() == _concrete(internal_template)
    assert versioned.requests == [
        JsonRequest("GET", _concrete(internal_template), query)
    ]


@pytest.mark.parametrize(
    "path",
    [
        "/api/v2/capture-batches",
        "/api/v30/recordings/rec_a/receiver-agnostic-cfo-qam",
        "/api/v999/not-a-real-route",
    ],
)
def test_versioned_http_routes_are_gone_and_never_reach_internal_handlers(
    path: str,
) -> None:
    versioned = _CapturingVersionedApplication()
    response = DashboardPublicJsonApplication(versioned).handle(
        JsonRequest("GET", path, {})
    )

    assert response.status == 410
    assert json.loads(response.body) == {
        "error": {
            "code": "gone",
            "message": "versioned dashboard API routes are no longer public",
        }
    }
    assert versioned.requests == []


def test_existing_unversioned_routes_and_nonversion_names_are_delegated() -> None:
    versioned = _CapturingVersionedApplication()
    application = DashboardPublicJsonApplication(versioned)

    storage = application.handle(JsonRequest("GET", "/api/storage-health", {}))
    named = application.handle(JsonRequest("GET", "/api/version-info", {}))

    assert storage.status == 299 and storage.body == b"/api/storage-health"
    assert named.status == 299 and named.body == b"/api/version-info"
