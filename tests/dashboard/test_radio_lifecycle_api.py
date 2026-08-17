from __future__ import annotations

import json

from leo_flow.contracts.core import V0_1, CaptureAttemptId, RadioId, SchemaRef
from leo_flow.contracts.radio_lifecycle import (
    CaptureAttemptLifecycleDashboardViewV0_1,
    RadioLifecycleConfidence,
    RadioLifecycleReason,
)
from leo_flow.dashboard import DashboardNotFound
from leo_flow.dashboard.api import DashboardJsonApplicationV5, JsonRequest, JsonResponse


class _V4:
    def handle(self, request: JsonRequest) -> JsonResponse:
        return JsonResponse(299, (), request.path.encode())


class _Lifecycle:
    def capture_attempt_radio_lifecycle(self, attempt_id: CaptureAttemptId):
        if attempt_id != CaptureAttemptId("cattempt_lifecycle_api"):
            raise DashboardNotFound("lifecycle not found")
        return CaptureAttemptLifecycleDashboardViewV0_1(
            SchemaRef(CaptureAttemptLifecycleDashboardViewV0_1.SCHEMA_ID, V0_1),
            attempt_id,
            RadioId("radio_lifecycle_api"),
            RadioLifecycleReason.RADIO_REBOOTED,
            RadioLifecycleConfidence.HIGH,
            ("boot_id_changed",),
            "41974bfd-7aa8-4d28-b1c8-57d21c3e05bb",
            "d6f89d3a-6856-441f-83db-96c71728e15b",
            100,
            10,
            True,
        )


def test_v5_lifecycle_route_is_bounded_and_delegates_older_routes() -> None:
    app = DashboardJsonApplicationV5(_V4(), _Lifecycle())  # type: ignore[arg-type]
    response = app.handle(
        JsonRequest(
            "GET",
            "/api/v5/capture-attempts/cattempt_lifecycle_api/radio-lifecycle",
            {},
        )
    )
    assert response.status == 200
    payload = json.loads(response.body)
    assert payload["reason"] == "radio_rebooted"
    assert payload["evidence_codes"] == ["boot_id_changed"]
    assert set(payload) == {
        "schema",
        "attempt_id",
        "radio_id",
        "reason",
        "confidence",
        "evidence_codes",
        "preflight_boot_id",
        "terminal_boot_id",
        "preflight_uptime_ns",
        "terminal_uptime_ns",
        "observer_available_at_terminal",
    }
    assert app.handle(JsonRequest("GET", "/api/storage-health", {})).status == 299


def test_v5_lifecycle_route_rejects_mutation_and_missing_fact() -> None:
    app = DashboardJsonApplicationV5(_V4(), _Lifecycle())  # type: ignore[arg-type]
    path = "/api/v5/capture-attempts/cattempt_lifecycle_api/radio-lifecycle"
    assert app.handle(JsonRequest("POST", path, {})).status == 405
    missing = app.handle(
        JsonRequest(
            "GET",
            "/api/v5/capture-attempts/cattempt_missing/radio-lifecycle",
            {},
        )
    )
    assert missing.status == 404
    assert b"boot_id" not in missing.body
