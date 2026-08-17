from __future__ import annotations

import pytest

from leo_flow.contracts.core import (
    CaptureBatchId,
    Digest,
    JobId,
    RecordingId,
)
from leo_flow.contracts.focused_analysis import FocusedAnalysisPairScopeV0_1


def _scope() -> FocusedAnalysisPairScopeV0_1:
    return FocusedAnalysisPairScopeV0_1(
        Digest.sha256(b"definition"),
        CaptureBatchId("cbatch_focused_pair"),
        (RecordingId("rec_focused_a"), RecordingId("rec_focused_b")),
        (Digest.sha256(b"a"), Digest.sha256(b"b")),
        (JobId("job_feature_a"), JobId("job_feature_b")),
        (JobId("job_waterfall_a"), JobId("job_waterfall_b")),
        (JobId("job_suite_a"), JobId("job_suite_b")),
    )


def test_focused_pair_scope_binds_two_recordings_and_six_jobs() -> None:
    scope = _scope()
    assert len(scope.recording_ids) == 2
    assert str(scope.identity_digest).startswith("sha256:")
    assert scope.identity_digest == _scope().identity_digest


def test_focused_pair_scope_rejects_cross_lane_job_reuse() -> None:
    scope = _scope()
    with pytest.raises(ValueError, match="job identities"):
        FocusedAnalysisPairScopeV0_1(
            scope.capture_definition_digest,
            scope.batch_id,
            scope.recording_ids,
            scope.recording_identity_digests,
            scope.feature_job_ids,
            (scope.feature_job_ids[0], scope.waterfall_job_ids[1]),
            scope.starlink_suite_job_ids,
        )
