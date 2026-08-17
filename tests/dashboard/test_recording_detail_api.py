from __future__ import annotations

import json

from leo_flow.adapters.dashboard_recording_postgres import (
    recording_starlink_suite_view_v0_2,
)
from leo_flow.dashboard.api import (
    DashboardJsonApplicationV3,
    DashboardJsonApplicationV4,
    JsonRequest,
)
from tests.recording_analysis.test_starlink_suite_pipeline import _recording_bundle

from ._fixtures import repository
from ._recording_detail_fixtures import (
    RECORDING_ID,
    RecordingDetailFixtureQueries,
    starlink_candidates,
)


def request(path: str, method: str = "GET", *, with_starlink: bool = False):
    queries = RecordingDetailFixtureQueries(
        repository(), starlink_candidates() if with_starlink else None
    )
    return DashboardJsonApplicationV3(
        queries, queries, queries, queries, queries
    ).handle(JsonRequest(method, path, {}))


def payload(response):
    return json.loads(response.body)


def test_v3_capture_detail_exposes_manifest_facts_and_segment_tunings() -> None:
    response = request(f"/api/v3/recordings/{RECORDING_ID}")
    assert response.status == 200
    detail = payload(response)
    assert detail["schema"] == {
        "schema_id": "org.leo-flow.dashboard.recording-capture-detail",
        "version": {"major": 0, "minor": 1},
    }
    assert detail["recording_id"] == str(RECORDING_ID)
    assert detail["plan_id"] == "plan_dashboard"
    assert detail["manifest_digest"]["value"]
    assert detail["segments"] == [
        {
            "activity_id": "act_dashboard",
            "activity_kind": "scan",
            "bandwidth_hz": 5_000_000.0,
            "center_frequency_hz": 10_755_000_000.0,
            "finished_utc_ns": 110,
            "gain_db": 42.0,
            "gain_mode": "manual",
            "receiver_chain_ids": ["rx_a", "rx_b"],
            "sample_count": 50_000,
            "sample_rate_hz": 5_000_000.0,
            "segment_id": "seg_dashboard",
            "started_utc_ns": 100,
        }
    ]


def test_v3_waterfall_is_bounded_projected_json_not_a_storage_locator() -> None:
    response = request(f"/api/v3/recordings/{RECORDING_ID}/waterfall")
    assert response.status == 200
    body = payload(response)
    assert body["state"] == "complete"
    assert body["tiles"][0]["power_db"] == [
        [-82.0, -63.0, -78.0],
        [-80.0, -48.0, -74.0],
    ]
    assert body["tiles"][0]["power_reference"] == "counts-squared-per-bin"
    assert b"locator" not in response.body and b"/home/" not in response.body


def test_v3_starlink_route_exposes_candidates_but_no_detection_count_or_cas() -> None:
    response = request(
        f"/api/v3/recordings/{RECORDING_ID}/starlink", with_starlink=True
    )
    assert response.status == 200
    body = payload(response)
    assert body["decision"]["state"] == "candidates"
    assert body["decision"]["calibrated_detection_count"] is None
    assert body["candidates"][0]["exact_minus_control_margin"] == 0.41
    assert body["candidates"][0]["search_identity_digest"]["value"]
    assert b"locator" not in response.body and b"cas:" not in response.body


def test_v3_preserves_old_routes_and_redacts_failures() -> None:
    assert request("/api/storage-health").status == 200
    assert request("/api/v2/capture-batches/absent").status == 400
    assert request("/api/v3/recordings/rec_absent").status == 404
    assert request(f"/api/v3/recordings/{RECORDING_ID}", method="POST").status == 405
    assert request(f"/api/v3/recordings/{RECORDING_ID}/unknown").status == 404


def test_v4_exposes_all_report_methods_and_no_detection_count_or_cas() -> None:
    queries = RecordingDetailFixtureQueries(repository(), starlink_candidates())
    bundle, ref = _recording_bundle()
    suite_queries = type(
        "SuiteQueries",
        (),
        {
            "recording_starlink_suite": lambda self, recording_id: (
                recording_starlink_suite_view_v0_2(bundle, ref)
            )
        },
    )()
    v3 = DashboardJsonApplicationV3(queries, queries, queries, queries, queries)
    response = DashboardJsonApplicationV4(v3, suite_queries).handle(
        JsonRequest(
            "GET", f"/api/v4/recordings/{bundle.recording_id}/starlink-suite", {}
        )
    )
    assert response.status == 200
    body = payload(response)
    assert body["state"] == "candidates"
    assert body["method_count"] == 8
    assert body["calibrated_detection_count"] is None
    assert {item["method"] for item in body["methods"]} == {
        "anchor-8",
        "differential-16",
        "differential-32",
        "glrt-32",
        "glrt-64",
        "full-frame-acquire",
        "full-frame-verify",
        "full-frame-full",
    }
    assert b"locator" not in response.body and b"cas:" not in response.body
