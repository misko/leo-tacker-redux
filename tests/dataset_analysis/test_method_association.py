from __future__ import annotations

import pytest

from leo_flow.analysis.dataset import method_firing_association
from leo_flow.contracts.core import SegmentId
from leo_flow.contracts.features import MethodScore


def score(method: str, window: int, value: float) -> MethodScore:
    return MethodScore(
        method_id=method,
        method_version="1",
        segment_id=SegmentId("seg_1"),
        receiver_key="rx-a",
        window_start_sample=window * 100,
        window_stop_sample=(window + 1) * 100,
        score=value,
        score_semantics="unitless-test-score",
    )


def test_covariance_between_firings_uses_shared_samples_and_reports_missingness() -> None:
    scores = [
        score("a", 0, 1),
        score("b", 0, 1),
        score("a", 1, 1),
        score("b", 1, 0),
        score("a", 2, 0),
        score("b", 2, 1),
        score("a", 3, 0),
        score("b", 3, 0),
        score("a", 4, 1),  # b is deliberately missing, not a non-firing.
    ]
    report = method_firing_association(scores, {"a@1": 0.5, "b@1": 0.5})

    assert report.method_ids == ("a@1", "b@1")
    assert report.firing_covariance[0][0] == pytest.approx(0.24)
    assert report.firing_covariance[0][1] == pytest.approx(0.0)
    assert report.firing_covariance[1][1] == pytest.approx(0.25)
    assert report.phi[0][1] == pytest.approx(0.0)
    assert report.shared_window_count == ((5, 4), (4, 4))
    assert report.shared_sample_count == ((500, 400), (400, 400))
    assert report.method_present_window_count == (5, 4)
    assert report.union_window_count == 5
    assert report.missing_window_count == (0, 1)


def test_constant_firing_has_covariance_but_undefined_phi() -> None:
    report = method_firing_association(
        [score("a", 0, 1), score("b", 0, 1)],
        {"a@1": 0.5, "b@1": 0.5},
    )
    assert report.firing_covariance == ((0.0, 0.0), (0.0, 0.0))
    assert report.phi == ((None, None), (None, None))


def test_thresholds_are_versioned_and_duplicates_are_refused() -> None:
    with pytest.raises(ValueError, match="no firing threshold"):
        method_firing_association([score("a", 0, 1)], {"a@2": 0.5})
    duplicate = score("a", 0, 2)
    with pytest.raises(ValueError, match="duplicate method score"):
        method_firing_association(
            [score("a", 0, 1), duplicate], {"a@1": 0.5}
        )
