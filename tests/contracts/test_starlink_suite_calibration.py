from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.contracts.core import (
    V0_1,
    Digest,
    RadioId,
    ReceiverChainId,
    SchemaRef,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_detector_suite import StarlinkDetectorMethod
from leo_flow.contracts.starlink_suite_calibration import (
    StarlinkSuiteCalibrationCellPlanV0_1,
    StarlinkSuitePositiveGateV0_1,
)


def _digest(label: str) -> Digest:
    return Digest.sha256(label.encode())


def _plan() -> StarlinkSuiteCalibrationCellPlanV0_1:
    return StarlinkSuiteCalibrationCellPlanV0_1(
        SchemaRef(StarlinkSuiteCalibrationCellPlanV0_1.SCHEMA_ID, V0_1),
        "slsuitecalcell_r20_a_ch1_lower_2m5_8ms_anchor8",
        RadioId("radio_pluto_5d4d"),
        ReceiverChainId("rx_lnb_a"),
        1,
        StarlinkEdge.LOWER,
        2_500_000.0,
        20_000,
        StarlinkDetectorMethod.ANCHOR_8,
        _digest("hardware"),
        _digest("tuning"),
        _digest("algorithm"),
        _digest("config"),
        _digest("exact"),
        _digest("roll17"),
        _digest("whole-search"),
        "whole-search-reported-score",
        "strict-greater-than",
        0.2,
        0.8,
        10,
        10,
        (StarlinkSuitePositiveGateV0_1(-6.0, 10, 0.7, 0.8),),
    )


def test_suite_calibration_cell_is_exact_and_non_poolable() -> None:
    plan = _plan()
    assert plan.method is StarlinkDetectorMethod.ANCHOR_8
    assert plan.sample_rate_hz == 2_500_000
    assert plan.probe_sample_count == 20_000
    assert plan.threshold_comparison == "strict-greater-than"

    with pytest.raises(ValueError, match="clipped pilot-band"):
        replace(plan, sample_rate_hz=1_250_000)
    with pytest.raises(ValueError, match="sorted unique"):
        replace(
            plan,
            positive_gates=(
                StarlinkSuitePositiveGateV0_1(-3.0, 10, 0.7, 0.8),
                StarlinkSuitePositiveGateV0_1(-6.0, 10, 0.7, 0.8),
            ),
        )


def test_suite_calibration_rejects_margin_or_non_strict_threshold_semantics() -> None:
    plan = _plan()
    with pytest.raises(ValueError, match="statistic"):
        replace(plan, statistic="exact-minus-control-margin")
    with pytest.raises(ValueError, match="score > threshold"):
        replace(plan, threshold_comparison="greater-than-or-equal")
