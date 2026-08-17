from __future__ import annotations

import math

from leo_flow.adapters.dashboard_score_distribution_postgres import (
    score_distribution_view_v0_1,
)
from leo_flow.contracts.core import UtcNs
from leo_flow.contracts.dashboard import TimeRangeQuery


def test_rows_become_fixed_unit_area_histograms_with_raw_summary() -> None:
    query = TimeRangeQuery(UtcNs(10), UtcNs(20))
    rows = [
        {
            "method": "anchor-8",
            "recording_count": 2,
            "score_count": 4,
            "mean": 0.25625,
            "standard_deviation": 0.1,
            "minimum": 0.01,
            "maximum": 1.0,
            "bins": {"0": 1, "20": 2, "39": 1},
        }
    ]
    view = score_distribution_view_v0_1(query, rows)
    distribution = view.distributions[0]
    assert distribution.recording_count == 2
    assert distribution.score_count == 4
    assert distribution.bins[0].count == 1
    assert distribution.bins[20].count == 2
    assert distribution.bins[39].count == 1
    assert math.isclose(
        sum(item.density * (item.upper - item.lower) for item in distribution.bins),
        1.0,
    )


def test_empty_rows_are_an_explicit_empty_distribution_view() -> None:
    view = score_distribution_view_v0_1(TimeRangeQuery(UtcNs(10), UtcNs(20)), [])
    assert view.distributions == ()
    assert view.score_domain_lower == 0.0
    assert view.score_domain_upper == 1.0
