from __future__ import annotations

import pytest

from leo_flow.contracts.dashboard_observation import StarlinkEvidenceAggregateV0_1


def test_candidate_positive_rate_is_derived_and_detection_stays_unavailable() -> None:
    aggregate = StarlinkEvidenceAggregateV0_1(
        "method", "glrt-64", 4, 3, 0.75, None, None
    )
    assert aggregate.candidate_positive_rate == 0.75
    assert aggregate.calibrated_detection_rate is None


def test_candidate_only_aggregate_rejects_detection_claim() -> None:
    with pytest.raises(ValueError, match="candidate-only"):
        StarlinkEvidenceAggregateV0_1("lnb", "rx_lnb_a", 4, 3, 0.75, 3, 0.75)
