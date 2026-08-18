from __future__ import annotations

import json

from leo_flow.contracts.core import canonical_json_bytes
from leo_flow.contracts.dashboard_recording_analysis import (
    RecordingAnalysisProduct,
    RecordingAnalysisProductState,
)
from leo_flow.dashboard.api import (
    JsonRequest,
    JsonResponse,
    RecordingAnalysisFacadeApplication,
)
from leo_flow.dashboard.repository import DashboardNotFound


def _response(status: int, payload: object) -> JsonResponse:
    return JsonResponse(
        status,
        (("content-type", "application/json; charset=utf-8"),),
        canonical_json_bytes(payload),
    )


class _PublishedProducts:
    def __init__(self, *, unknown_recording: bool = False) -> None:
        self.unknown_recording = unknown_recording
        self.calls: list[JsonRequest] = []
        self.payloads: dict[str, object] = {}

    def handle(self, request: JsonRequest) -> JsonResponse:
        self.calls.append(request)
        if request.path == "/delegated":
            return _response(299, {"delegated": True})
        if request.path.endswith("/rec_unknown") and self.unknown_recording:
            return _response(404, {"error": "missing"})
        if request.path == "/api/v3/recordings/rec_known":
            return _response(200, {"recording_id": "rec_known", "radio_id": "radio_a"})
        payload = self.payloads.get(request.path)
        return (
            _response(200, payload)
            if payload is not None
            else _response(404, {"error": "not published"})
        )


class _Availability:
    def __init__(self) -> None:
        self.states: dict[RecordingAnalysisProduct, RecordingAnalysisProductState] = {}
        self.calls: list[RecordingAnalysisProduct] = []

    def recording_analysis_product_state(self, recording_id, product):
        assert str(recording_id) == "rec_known"
        self.calls.append(product)
        return self.states.get(product, RecordingAnalysisProductState.NOT_ANALYZED)


def _payload(response: JsonResponse) -> dict:
    return json.loads(response.body)


def _products(response: JsonResponse) -> dict[str, dict]:
    return {
        product["product"]: product
        for section in _payload(response)["sections"]
        for product in section["products"]
    }


def test_facade_returns_mixed_advanced_doppler_complete_and_qam_not_analyzed() -> None:
    published = _PublishedProducts()
    published.payloads["/api/v19/recordings/rec_known/evidence-advanced-doppler"] = {
        "state": "complete",
        "series": [{"path_digest": "abc"}],
    }
    availability = _Availability()
    app = RecordingAnalysisFacadeApplication(published, availability)

    response = app.handle(
        JsonRequest(
            "GET",
            "/api/recordings/rec_known/analysis",
            {"sections": "primary,extended"},
        )
    )

    assert response.status == 200
    products = _products(response)
    assert products["advanced_doppler"] == {
        "payload": {"series": [{"path_digest": "abc"}], "state": "complete"},
        "product": "advanced_doppler",
        "source": "advanced-doppler-v0.1",
        "state": "complete",
    }
    assert products["qam"] == {
        "payload": None,
        "product": "qam",
        "source": None,
        "state": "not_analyzed",
    }
    qam_paths = [
        call.path
        for call in published.calls
        if "adaptive-qam" in call.path or "acquired-constellation" in call.path
    ]
    assert qam_paths == [
        "/api/v25/recordings/rec_known/starlink-adaptive-qam",
        "/api/v17/recordings/rec_known/starlink-acquired-constellation",
    ]


def test_facade_prefers_adaptive_qam_then_uses_acquired_fallback() -> None:
    for route, source in (
        (
            "/api/v25/recordings/rec_known/starlink-adaptive-qam",
            "adaptive-qam-v0.4",
        ),
        (
            "/api/v17/recordings/rec_known/starlink-acquired-constellation",
            "acquired-qam-v0.3",
        ),
    ):
        published = _PublishedProducts()
        published.payloads[route] = {"recording_id": "rec_known"}
        app = RecordingAnalysisFacadeApplication(published, _Availability())
        response = app.handle(
            JsonRequest("GET", "/api/recordings/rec_known/analysis", {})
        )
        assert response.status == 200
        assert _products(response)["qam"]["source"] == source
        if source == "adaptive-qam-v0.4":
            assert not any(
                "acquired-constellation" in call.path for call in published.calls
            )


def test_optional_products_use_explicit_durable_states_and_never_404() -> None:
    published = _PublishedProducts()
    availability = _Availability()
    availability.states.update(
        {
            RecordingAnalysisProduct.QAM: RecordingAnalysisProductState.PENDING,
            RecordingAnalysisProduct.EVIDENCE_CONTEXT: RecordingAnalysisProductState.FAILED,
            RecordingAnalysisProduct.ADAPTIVE_DETECTOR_RESPONSE: RecordingAnalysisProductState.NO_CANDIDATE,
        }
    )
    response = RecordingAnalysisFacadeApplication(published, availability).handle(
        JsonRequest("GET", "/api/recordings/rec_known/analysis", {})
    )

    assert response.status == 200
    products = _products(response)
    assert products["qam"]["state"] == "pending"
    assert products["evidence_context"]["state"] == "failed"
    assert products["adaptive_detector_response"]["state"] == "no_candidate"
    assert products["doppler_summary"]["state"] == "not_analyzed"


def test_incomplete_legacy_catalog_views_defer_to_durable_availability() -> None:
    published = _PublishedProducts()
    published.payloads["/api/v19/recordings/rec_known/evidence-advanced-doppler"] = {
        "state": "missing",
        "series": [],
    }
    availability = _Availability()
    availability.states[RecordingAnalysisProduct.ADVANCED_DOPPLER] = (
        RecordingAnalysisProductState.PENDING
    )

    response = RecordingAnalysisFacadeApplication(published, availability).handle(
        JsonRequest(
            "GET",
            "/api/recordings/rec_known/analysis",
            {"sections": "extended"},
        )
    )

    assert response.status == 200
    assert _products(response)["advanced_doppler"] == {
        "payload": None,
        "product": "advanced_doppler",
        "source": None,
        "state": "pending",
    }


def test_only_unknown_recording_returns_404_and_other_routes_delegate() -> None:
    published = _PublishedProducts(unknown_recording=True)
    app = RecordingAnalysisFacadeApplication(published, _Availability())
    assert (
        app.handle(
            JsonRequest("GET", "/api/recordings/rec_unknown/analysis", {})
        ).status
        == 404
    )
    assert app.handle(JsonRequest("GET", "/delegated", {})).status == 299
    assert (
        app.handle(JsonRequest("POST", "/api/recordings/rec_known/analysis", {})).status
        == 405
    )


def test_optional_availability_failure_cannot_masquerade_as_unknown_recording() -> None:
    class _BrokenAvailability:
        def recording_analysis_product_state(self, recording_id, product):
            raise DashboardNotFound("optional status row is absent")

    response = RecordingAnalysisFacadeApplication(
        _PublishedProducts(), _BrokenAvailability()
    ).handle(JsonRequest("GET", "/api/recordings/rec_known/analysis", {}))

    assert response.status == 500


def test_facade_forwards_bounded_selectors_to_each_published_product() -> None:
    published = _PublishedProducts()
    app = RecordingAnalysisFacadeApplication(published, _Availability())
    response = app.handle(
        JsonRequest(
            "GET",
            "/api/recordings/rec_known/analysis",
            {
                "sections": "primary,extended",
                "mode": "windows",
                "radio_ids": "radio_a",
                "lnb_ids": "lnb_a",
                "receiver_chain_ids": "rx_a",
                "edges": "lower",
                "qam_maximum_windows": "12",
                "doppler_maximum_windows": "24",
                "maximum_points": "128",
            },
        )
    )

    assert response.status == 200
    calls = {call.path: call.query for call in published.calls}
    assert (
        calls["/api/v25/recordings/rec_known/starlink-adaptive-qam"][
            "maximum_windows_per_stream"
        ]
        == "12"
    )
    assert (
        calls["/api/v19/recordings/rec_known/evidence-advanced-doppler"][
            "maximum_windows"
        ]
        == "24"
    )
    assert (
        calls["/api/v28/recordings/rec_known/starlink-pilot-refinement"][
            "maximum_points"
        ]
        == "128"
    )


def test_facade_rejects_unknown_duplicate_and_unbounded_selectors() -> None:
    app = RecordingAnalysisFacadeApplication(_PublishedProducts(), _Availability())
    path = "/api/recordings/rec_known/analysis"
    for query in (
        {"sections": "primary,primary"},
        {"sections": "everything"},
        {"radio_ids": ",".join(f"radio_{index}" for index in range(17))},
        {"qam_maximum_windows": "33"},
        {"locator": "/private"},
    ):
        assert app.handle(JsonRequest("GET", path, query)).status == 400
