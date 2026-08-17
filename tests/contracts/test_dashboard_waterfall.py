from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.contracts.core import (
    V0_1,
    AnalysisRunId,
    Digest,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    UtcNs,
)
from leo_flow.contracts.dashboard_waterfall import (
    RecordingWaterfallViewV0_1,
    WaterfallProjectionState,
    WaterfallTileViewV0_1,
)


def tile() -> WaterfallTileViewV0_1:
    return WaterfallTileViewV0_1(
        SegmentId("seg_a"),
        ReceiverChainId("rx_a"),
        UtcNs(1_000),
        1_000,
        10_755_000_000.0,
        5_000_000.0,
        256,
        (0, 500),
        (256, 756),
        (UtcNs(26_600), UtcNs(126_600)),
        (-1_000.0, 0.0, 1_000.0),
        ((-80.0, -60.0, -75.0), (-78.0, -50.0, -70.0)),
        "counts-squared-per-bin",
        -90.0,
        -40.0,
    )


def complete() -> RecordingWaterfallViewV0_1:
    return RecordingWaterfallViewV0_1(
        SchemaRef(RecordingWaterfallViewV0_1.SCHEMA_ID, V0_1),
        RecordingId("rec_a"),
        Digest.sha256(b"recording-identity"),
        AnalysisRunId("arun_waterfall"),
        WaterfallProjectionState.COMPLETE,
        None,
        (tile(),),
    )


def test_complete_waterfall_is_bounded_and_self_describing() -> None:
    view = complete()
    assert view.tiles[0].power_reference == "counts-squared-per-bin"
    assert len(view.tiles[0].power_db) == len(view.tiles[0].time_bin_midpoint_utc_ns)


def test_state_and_axis_invariants_reject_ambiguous_projections() -> None:
    view = complete()
    with pytest.raises(ValueError, match="complete waterfall"):
        replace(view, tiles=())
    with pytest.raises(ValueError, match="frequency axis"):
        replace(view.tiles[0], power_db=((-80.0,), (-70.0,)))
    with pytest.raises(ValueError, match="unavailable waterfall"):
        replace(
            view,
            state=WaterfallProjectionState.UNAVAILABLE,
            analysis_run_id=None,
        )
    failed = replace(
        view,
        state=WaterfallProjectionState.FAILED,
        reason_code="insufficient-contiguous-samples",
        tiles=(),
    )
    assert failed.reason_code == "insufficient-contiguous-samples"
