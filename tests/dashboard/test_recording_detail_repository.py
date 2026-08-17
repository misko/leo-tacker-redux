from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.dashboard import DashboardNotFound
from leo_flow.dashboard.repository import (
    InMemoryDashboardRepository,
    RecordingCaptureDetailProjection,
    RecordingWaterfallProjection,
)

from ._recording_detail_fixtures import RECORDING_ID, capture_detail, waterfall


def test_exact_recording_projections_return_only_the_latest_sequence() -> None:
    first_detail = capture_detail()
    latest_detail = replace(first_detail, analysis_state="superseded")
    first_waterfall = waterfall()
    latest_waterfall = replace(
        first_waterfall,
        recording_identity_digest=first_waterfall.recording_identity_digest,
    )
    repository = InMemoryDashboardRepository(
        recording_capture_details=(
            RecordingCaptureDetailProjection(first_detail, 1),
            RecordingCaptureDetailProjection(latest_detail, 3),
        ),
        recording_waterfalls=(
            RecordingWaterfallProjection(first_waterfall, 2),
            RecordingWaterfallProjection(latest_waterfall, 4),
        ),
    )
    assert (
        repository.recording_capture_detail(RECORDING_ID).analysis_state == "superseded"
    )
    assert repository.recording_waterfall(RECORDING_ID) == latest_waterfall


def test_missing_exact_recording_projection_is_not_inferred() -> None:
    repository = InMemoryDashboardRepository()
    with pytest.raises(DashboardNotFound, match="capture detail"):
        repository.recording_capture_detail(RECORDING_ID)
    with pytest.raises(DashboardNotFound, match="waterfall"):
        repository.recording_waterfall(RECORDING_ID)
