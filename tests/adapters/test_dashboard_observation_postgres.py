from __future__ import annotations

from leo_flow.adapters.dashboard_observation_postgres import (
    aggregate_observation_rows_v0_1,
)
from leo_flow.contracts.core import UtcNs
from leo_flow.contracts.dashboard import TimeRangeQuery


def test_aggregate_unions_radio_time_and_groups_candidate_evidence() -> None:
    rows = [
        {
            "recording_id": "rec_aggregate_a",
            "radio_id": "radio_a",
            "capture_view": {
                "segments": [
                    {
                        "started_utc_ns": 100,
                        "finished_utc_ns": 200,
                        "receiver_chain_ids": ["rx_lnb_a", "rx_lnb_b"],
                    },
                    {
                        "started_utc_ns": 190,
                        "finished_utc_ns": 250,
                        "receiver_chain_ids": ["rx_lnb_a", "rx_lnb_b"],
                    },
                ]
            },
            "suite_view": {
                "state": "candidates",
                "calibrated_detection_count": None,
                "methods": [
                    {
                        "receiver_chain_id": "rx_lnb_a",
                        "edge": "upper",
                        "method": "glrt-64",
                        "score": 0.4,
                        "control_score": 0.1,
                    },
                    {
                        "receiver_chain_id": "rx_lnb_b",
                        "edge": "lower",
                        "method": "glrt-64",
                        "score": 0.1,
                        "control_score": 0.2,
                    },
                ],
            },
        }
    ]
    view = aggregate_observation_rows_v0_1(TimeRangeQuery(UtcNs(0), UtcNs(1_000)), rows)
    radio = next(item for item in view.duty_cycles if item.dimension == "radio")
    assert radio.active_ns == 150
    assert radio.duty_cycle == 0.15
    method = next(
        item
        for item in view.starlink_evidence
        if item.dimension == "method" and item.identity == "glrt-64"
    )
    assert method.comparison_count == 2
    assert method.candidate_positive_count == 1
    assert method.candidate_positive_rate == 0.5
    assert method.calibrated_detection_rate is None
    assert view.recording_states[0].state == "candidates"


def test_aggregate_marks_missing_and_clipped_suite_states() -> None:
    base = {
        "radio_id": "radio_a",
        "capture_view": {
            "segments": [
                {
                    "started_utc_ns": 10,
                    "finished_utc_ns": 20,
                    "receiver_chain_ids": ["rx_lnb_a"],
                }
            ]
        },
    }
    rows = [
        {**base, "recording_id": "rec_missing", "suite_view": None},
        {
            **base,
            "recording_id": "rec_clipped",
            "suite_view": {
                "state": "not_evaluated",
                "calibrated_detection_count": None,
                "methods": [],
            },
        },
    ]
    view = aggregate_observation_rows_v0_1(TimeRangeQuery(UtcNs(0), UtcNs(100)), rows)
    assert view.unavailable_recording_count == 1
    assert view.not_evaluated_recording_count == 1
    assert view.candidate_recording_count == 0
