from __future__ import annotations

import pytest

from leo_flow.contracts.core import UtcNs
from leo_flow.contracts.dashboard_score_distribution import (
    SCORE_HISTOGRAM_BIN_COUNT,
    MethodScoreDistributionV0_1,
    ScoreDistributionViewV0_1,
    ScoreHistogramBinV0_1,
)


def bins(*, count: int = 4) -> tuple[ScoreHistogramBinV0_1, ...]:
    width = 1.0 / SCORE_HISTOGRAM_BIN_COUNT
    return tuple(
        ScoreHistogramBinV0_1(
            index,
            index * width,
            (index + 1) * width,
            count if index == 0 else 0,
            1.0 / width if index == 0 else 0.0,
        )
        for index in range(SCORE_HISTOGRAM_BIN_COUNT)
    )


def test_distribution_requires_canonical_unit_area_histogram() -> None:
    method = MethodScoreDistributionV0_1(
        "anchor-8", 2, 4, 0.01, 0.0, 0.01, 0.01, bins()
    )
    view = ScoreDistributionViewV0_1(
        1,
        UtcNs(1),
        UtcNs(2),
        0.0,
        1.0,
        SCORE_HISTOGRAM_BIN_COUNT,
        "candidate-method-score-density",
        (method,),
    )
    assert view.distributions[0].score_count == 4


def test_distribution_rejects_missing_bins_and_non_unit_density() -> None:
    with pytest.raises(ValueError, match="every fixed bin"):
        MethodScoreDistributionV0_1("anchor-8", 1, 4, 0.1, 0.0, 0.1, 0.1, bins()[:-1])
    changed = list(bins())
    changed[0] = ScoreHistogramBinV0_1(0, 0.0, 0.025, 4, 1.0)
    with pytest.raises(ValueError, match="integrate to one"):
        MethodScoreDistributionV0_1(
            "anchor-8", 1, 4, 0.1, 0.0, 0.1, 0.1, tuple(changed)
        )
