from __future__ import annotations

import json

from leo_flow.capture.radio_lifecycle import (
    InMemoryRadioLifecycleFactRecorderV0_1,
    build_attempt_lifecycle_fact,
)
from leo_flow.contracts.core import (
    V0_1,
    CaptureAttemptId,
    CaptureBatchId,
    RadioId,
    SchemaRef,
    UtcNs,
)
from leo_flow.contracts.radio_lifecycle import (
    CaptureAttemptLifecycleFactV0_1,
    IiodProcessIdentityV0_1,
    RadioLifecycleObservationSource,
    RadioLifecycleObservationStatus,
    RadioLifecycleObservationV0_1,
    RadioLifecycleTrust,
    RadioTransportOutcome,
)
from leo_flow.dashboard.api import DashboardJsonApplicationV5, JsonRequest, JsonResponse


class _V4:
    def handle(self, request: JsonRequest) -> JsonResponse:
        return JsonResponse(404, (), b"")


def _observation(radio: RadioId, observed: int, boot: str, uptime: int):
    return RadioLifecycleObservationV0_1(
        SchemaRef(RadioLifecycleObservationV0_1.SCHEMA_ID, V0_1),
        radio,
        UtcNs(observed),
        RadioLifecycleObservationStatus.AVAILABLE,
        RadioLifecycleObservationSource.AUTHENTICATED_DIAGNOSTIC_V1,
        RadioLifecycleTrust.RADIO_AUTHENTICATED,
        boot,
        uptime,
        UtcNs(observed - uptime),
        1,
        IiodProcessIdentityV0_1(7, 11, 100),
    )


def test_authenticated_reboot_fact_flows_to_additive_dashboard_api() -> None:
    radio = RadioId("radio_vertical_lifecycle")
    attempt = CaptureAttemptId("cattempt_vertical_lifecycle")
    fact = build_attempt_lifecycle_fact(
        schema=SchemaRef(CaptureAttemptLifecycleFactV0_1.SCHEMA_ID, V0_1),
        batch_id=CaptureBatchId("cbatch_vertical_lifecycle"),
        attempt_id=attempt,
        radio_id=radio,
        preflight=_observation(radio, 10, "41974bfd-7aa8-4d28-b1c8-57d21c3e05bb", 9),
        terminal=_observation(radio, 20, "d6f89d3a-6856-441f-83db-96c71728e15b", 1),
        transport_outcome=RadioTransportOutcome.DISCONNECTED,
    )
    repository = InMemoryRadioLifecycleFactRecorderV0_1()
    repository.record_attempt(fact)
    response = DashboardJsonApplicationV5(
        _V4(),
        repository,  # type: ignore[arg-type]
    ).handle(
        JsonRequest(
            "GET",
            f"/api/v5/capture-attempts/{attempt}/radio-lifecycle",
            {},
        )
    )
    payload = json.loads(response.body)
    assert response.status == 200
    assert payload["reason"] == "radio_rebooted"
    assert payload["confidence"] == "high"
    assert payload["evidence_codes"] == ["boot_id_changed"]
