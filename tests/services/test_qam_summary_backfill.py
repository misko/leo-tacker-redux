from __future__ import annotations

import pytest

from leo_flow.services.qam_summary_backfill import (
    run_qam_summary_backfill_once_v0_2,
)


class _Backfill:
    def __init__(self) -> None:
        self.bounds: list[int] = []

    def backfill(self, maximum_products: int = 25) -> int:
        self.bounds.append(maximum_products)
        return 7


def test_one_shot_seam_delegates_exactly_one_bounded_batch() -> None:
    backfill = _Backfill()

    assert run_qam_summary_backfill_once_v0_2(backfill, 19) == 7
    assert backfill.bounds == [19]


@pytest.mark.parametrize("bound", [0, 101])
def test_one_shot_seam_rejects_unbounded_work_before_delegating(bound: int) -> None:
    backfill = _Backfill()

    with pytest.raises(ValueError, match="bound"):
        run_qam_summary_backfill_once_v0_2(backfill, bound)
    assert backfill.bounds == []
