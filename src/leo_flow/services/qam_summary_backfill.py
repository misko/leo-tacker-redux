"""Integration seam for one bounded QAM-summary backfill invocation."""

from __future__ import annotations

from typing import Protocol


class QamSummaryBackfillPortV0_2(Protocol):
    def backfill(self, maximum_products: int = 25) -> int: ...


def run_qam_summary_backfill_once_v0_2(
    backfill: QamSummaryBackfillPortV0_2, maximum_products: int = 25
) -> int:
    """Run one integration-owned batch; scheduling and deployment stay external."""

    if not 1 <= maximum_products <= 100:
        raise ValueError("QAM summary backfill bound is invalid")
    return backfill.backfill(maximum_products)
