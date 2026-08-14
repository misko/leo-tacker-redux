from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.analysis.model.tracking_input_codec import encode_tracking_input
from leo_flow.analysis.model.tracking_input_persistence import (
    DurableTrackingInputView,
    TrackingInputNotFoundError,
)
from leo_flow.contracts.core import ArtifactRef, Digest, SchemaRef
from leo_flow.contracts.storage import ObjectRef
from leo_flow.contracts.tracking_input import (
    TRACKING_INPUT_FORMAT_ID,
    TRACKING_INPUT_MEDIA_TYPE,
    TrackingInputSnapshotIdentity,
    TrackingInputSnapshotRef,
)
from leo_flow.jobs import InMemoryJobLeaseRepository, JobType
from leo_flow.services.model_analysis import decode_tracking_model_analysis_payload
from leo_flow.services.model_submission import (
    TrackingModelAnalysisSubmission,
    TrackingModelAnalysisSubmissionService,
    TrackingModelSubmissionError,
)
from tests.model_analysis.test_tracking_input_contract_codec import _snapshot


def _resolved(locator: str = "cas://first") -> DurableTrackingInputView:
    snapshot = _snapshot()
    payload = encode_tracking_input(snapshot)
    ref = TrackingInputSnapshotRef(
        snapshot.snapshot_id,
        snapshot.snapshot_digest,
        snapshot.membership_digest,
        ObjectRef(
            Digest.sha256(payload),
            len(payload),
            TRACKING_INPUT_MEDIA_TYPE,
            TRACKING_INPUT_FORMAT_ID,
            locator,
        ),
    )
    return DurableTrackingInputView(ref, snapshot)


def _artifact(name: str, schema: str) -> ArtifactRef:
    return ArtifactRef(name, Digest.sha256(name.encode()), SchemaRef(schema))


def _submission(
    resolved: DurableTrackingInputView,
) -> TrackingModelAnalysisSubmission:
    return TrackingModelAnalysisSubmission(
        resolved.ref.identity(),
        _artifact("tracking-config", "org.leo-flow.tracking-model-config"),
        _artifact("tracking-algorithm", "org.leo-flow.model-algorithm"),
    )


class _Authority:
    def __init__(self, values: list[DurableTrackingInputView | Exception]) -> None:
        self.values = values
        self.calls: list[TrackingInputSnapshotIdentity] = []

    def get_by_identity(
        self, identity: TrackingInputSnapshotIdentity
    ) -> DurableTrackingInputView:
        self.calls.append(identity)
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class _CountingJobs(InMemoryJobLeaseRepository):
    def __init__(self) -> None:
        super().__init__()
        self.enqueue_calls = 0

    def enqueue(self, *args, **kwargs) -> None:
        self.enqueue_calls += 1
        super().enqueue(*args, **kwargs)


def test_exact_duplicate_tracking_submission_is_restart_safe() -> None:
    resolved = _resolved()
    authority = _Authority([resolved, resolved])
    jobs = _CountingJobs()
    service = TrackingModelAnalysisSubmissionService(
        tracking_inputs=authority, jobs=jobs
    )

    first = service.submit(_submission(resolved))
    second = service.submit(_submission(resolved))

    assert first.job_id == second.job_id
    assert first.payload == second.payload
    assert jobs.enqueue_calls == 2
    lease = jobs.claim((JobType.MODEL_ANALYSIS,), "tracking-worker", 10.0)
    assert lease is not None and lease.job_id == first.job_id
    assert jobs.claim((JobType.MODEL_ANALYSIS,), "other", 10.0) is None
    assert decode_tracking_model_analysis_payload(lease.payload) == first.request


def test_relocation_changes_diagnostic_ref_but_not_request_payload_or_job_id() -> None:
    first_ref = _resolved("file:///old")
    relocated = replace(
        first_ref,
        ref=replace(
            first_ref.ref,
            bundle_ref=replace(first_ref.ref.bundle_ref, locator="s3://new"),
        ),
    )
    jobs = _CountingJobs()
    service = TrackingModelAnalysisSubmissionService(
        tracking_inputs=_Authority([first_ref, relocated]), jobs=jobs
    )
    submission = _submission(first_ref)

    first = service.submit(submission)
    second = service.submit(submission)

    assert first.job_id == second.job_id
    assert first.payload == second.payload
    assert first.request == second.request
    assert first.resolved_tracking_input_ref != second.resolved_tracking_input_ref


def test_missing_tracking_identity_preserves_authority_failure_before_enqueue() -> None:
    requested = _resolved()
    jobs = _CountingJobs()
    service = TrackingModelAnalysisSubmissionService(
        tracking_inputs=_Authority(
            [TrackingInputNotFoundError("no exact tracking input")]
        ),
        jobs=jobs,
    )

    with pytest.raises(TrackingInputNotFoundError, match="no exact"):
        service.submit(_submission(requested))
    assert jobs.enqueue_calls == 0


def test_substituted_tracking_identity_fails_before_enqueue() -> None:
    requested = _resolved()
    substituted_ref = replace(
        requested.ref,
        bundle_ref=replace(
            requested.ref.bundle_ref,
            digest=Digest.sha256(b"substituted-bundle"),
        ),
    )
    substituted = replace(requested, ref=substituted_ref)
    jobs = _CountingJobs()
    service = TrackingModelAnalysisSubmissionService(
        tracking_inputs=_Authority([substituted]), jobs=jobs
    )

    with pytest.raises(TrackingModelSubmissionError, match="substituted"):
        service.submit(_submission(requested))
    assert jobs.enqueue_calls == 0
