from __future__ import annotations

import json
from dataclasses import replace

from leo_flow.contracts.storage import ObjectRef
from leo_flow.dashboard.api import DashboardJsonApplication, JsonRequest

from ._fixtures import EVALUATION_ID, EVALUATION_RUN_ID, evaluation, repository


def request(path: str, query: dict[str, str] | None = None, method: str = "GET"):
    return DashboardJsonApplication(repository()).handle(
        JsonRequest(method, path, query or {})
    )


def payload(response):
    return json.loads(response.body)


def test_recording_and_activity_routes_emit_contract_dtos() -> None:
    recordings = request(
        "/api/recordings",
        {"start_utc_ns": "100", "stop_utc_ns": "140", "radio_ids": "radio_a"},
    )
    assert recordings.status == 200
    assert recordings.headers == (("content-type", "application/json; charset=utf-8"),)
    assert [item["recording_id"] for item in payload(recordings)["items"]] == [
        "rec_4",
        "rec_2",
    ]
    activity = request("/api/activity", {"start_utc_ns": "100", "stop_utc_ns": "130"})
    assert activity.status == 200
    assert sum(item["count"] for item in payload(activity)["counts"]) == 3


def test_detail_features_model_tracks_and_storage_routes() -> None:
    detail = payload(request("/api/recordings/rec_4"))
    assert detail["summary"]["analysis_state"] == "superseded"
    assert detail["recording_object_available"] is False
    features = payload(
        request("/api/recordings/rec_1/features", {"selector": "glrt32"})
    )
    assert [item["method_id"] for item in features["items"]] == ["glrt32", "glrt32"]
    assert payload(request("/api/models/production"))["model_snapshot_id"] == "model_a"
    tracks = payload(
        request("/api/tracks", {"start_utc_ns": "100", "stop_utc_ns": "140"})
    )
    assert [item["track_id"] for item in tracks["items"]] == ["track_3", "track_2"]
    assert payload(request("/api/storage-health")) == {
        "available": True,
        "free_bytes": 250,
        "total_bytes": 1000,
    }


def test_evaluation_route_has_stable_bounded_schema_for_both_identities() -> None:
    by_evaluation = payload(request(f"/api/evaluations/{EVALUATION_ID}"))
    by_run = payload(request(f"/api/evaluations/{EVALUATION_RUN_ID}"))

    assert by_evaluation == {
        "schema_version": 1,
        "queried_identity": str(EVALUATION_ID),
        "queried_identity_kind": "evaluation_id",
        "evaluation_id": str(EVALUATION_ID),
        "run_id": str(EVALUATION_RUN_ID),
        "method_count": 1,
        "union_window_count": 4,
        "warnings": ["fixture warning"],
        "methods": [
            {
                "method_id": "energy@1",
                "split": split,
                "threshold": 2.5,
                "score_semantics": "power",
                "coverage": {
                    "feature_set_count": 2,
                    "feature_set_present_count": 2,
                    "union_window_count": 4,
                    "present_window_count": 3,
                    "missing_window_count": 1,
                    "scored_prediction_count": 3,
                    "missing_prediction_count": 1,
                },
                "firing_count": 2,
                "confusion": {
                    "true_positive": 1,
                    "false_positive": 1,
                    "true_negative": 1,
                    "false_negative": 0,
                },
            }
            for split in ("train", "validation", "locked_test")
        ],
        "report_object": {
            "digest": {
                "algorithm": "sha256",
                "value": EVALUATION_ID.removeprefix("eval_"),
            },
            "byte_count": 123,
            "media_type": "application/json",
            "format_id": "detector-evaluation-report-v0.1",
            "locator": f"cas:sha256:{EVALUATION_ID.removeprefix('eval_')}",
        },
    }
    assert by_run == {
        **by_evaluation,
        "queried_identity": str(EVALUATION_RUN_ID),
        "queried_identity_kind": "run_id",
    }
    encoded = json.dumps(by_run)
    assert "covariance" not in encoded
    assert "/home/" not in encoded


def test_evaluation_route_validates_identity_and_reports_missing_rows() -> None:
    assert request("/api/evaluations/evaluation_bad").status == 400
    assert request("/api/evaluations/eval_bad/path").status == 404
    assert request("/api/evaluations/eval_absent").status == 404


def test_evaluation_route_never_exposes_a_non_cas_storage_locator() -> None:
    view = evaluation()
    unsafe_object = ObjectRef(
        view.ref.report_object.digest,
        view.ref.report_object.byte_count,
        view.ref.report_object.media_type,
        view.ref.report_object.format_id,
        "/home/operator/evaluation.json",
    )

    class UnsafeQueries:
        def detector_evaluation(self, identity: str):
            assert identity == str(EVALUATION_ID)
            return replace(view, ref=replace(view.ref, report_object=unsafe_object))

    response = DashboardJsonApplication(UnsafeQueries()).handle(
        JsonRequest("GET", f"/api/evaluations/{EVALUATION_ID}", {})
    )
    assert response.status == 500
    assert b"/home/operator" not in response.body
    assert payload(response)["error"] == {
        "code": "internal_error",
        "message": "dashboard query failed",
    }


def test_errors_are_deterministic_json_and_do_not_leak_internal_exceptions() -> None:
    missing_bounds = request("/api/recordings")
    assert missing_bounds.status == 400
    assert payload(missing_bounds) == {
        "error": {
            "code": "invalid_request",
            "message": "missing query parameter start_utc_ns",
        }
    }
    assert request("/api/recordings/rec_1/features").status == 400
    assert request("/api/recordings/rec_absent").status == 404
    assert request("/api/no-such-route").status == 404
    wrong_method = request("/api/storage-health", method="POST")
    assert wrong_method.status == 405
    assert payload(wrong_method)["error"]["code"] == "method_not_allowed"


def test_bad_cursor_and_radio_filter_are_client_errors() -> None:
    bad_cursor = request(
        "/api/recordings",
        {"start_utc_ns": "100", "stop_utc_ns": "140", "cursor": "not-base64"},
    )
    assert bad_cursor.status == 400
    duplicate_radio = request(
        "/api/activity",
        {
            "start_utc_ns": "100",
            "stop_utc_ns": "140",
            "radio_ids": "radio_a,radio_a",
        },
    )
    assert duplicate_radio.status == 400


def test_unexpected_repository_error_is_redacted() -> None:
    class BrokenQueries:
        def storage_health(self):
            raise RuntimeError("database password secret")

    response = DashboardJsonApplication(BrokenQueries()).handle(
        JsonRequest("GET", "/api/storage-health", {})
    )
    assert response.status == 500
    assert b"secret" not in response.body
    assert payload(response)["error"] == {
        "code": "internal_error",
        "message": "dashboard query failed",
    }
