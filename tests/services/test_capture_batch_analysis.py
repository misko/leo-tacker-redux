from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.contracts.capture_batch import (
    CaptureAttemptOutcome,
    CaptureAttemptState,
    CaptureBatchDefinition,
    CaptureBatchMode,
    CaptureBatchSnapshot,
    ExpectedCaptureAttempt,
    PairedAnalysisEligibility,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    CaptureAttemptId,
    CaptureBatchId,
    Digest,
    PlanId,
    RadioId,
    RecordingId,
    SchemaRef,
    UtcNs,
)
from leo_flow.contracts.features import FeatureSetBundle
from leo_flow.contracts.storage import (
    ObjectRef,
    PublishedRecordingRef,
    RecordingObjectRef,
)
from leo_flow.jobs.contracts import JobType
from leo_flow.jobs.memory import InMemoryJobLeaseRepository
from leo_flow.services.capture_batch_analysis import (
    ClosedBatchAnalysisSelection,
    ClosedBatchAnalysisSubmissionError,
    ClosedBatchAnalysisSubmissionService,
)


def _recording(name: str) -> PublishedRecordingRef:
    data = Digest.sha256(f"{name}:data".encode())
    metadata = Digest.sha256(f"{name}:metadata".encode())
    return PublishedRecordingRef(
        RecordingObjectRef(
            RecordingId(f"rec_{name}"),
            ObjectRef(
                data,
                10,
                "application/octet-stream",
                "recording-data-v1",
                f"cas:sha256:{data.value}",
            ),
            ObjectRef(
                metadata,
                20,
                "application/json",
                "recording-metadata-v1",
                f"cas:sha256:{metadata.value}",
            ),
            Digest.sha256(f"{name}:manifest".encode()),
        )
    )


def _definition() -> CaptureBatchDefinition:
    return CaptureBatchDefinition(
        SchemaRef(CaptureBatchDefinition.SCHEMA_ID),
        CaptureBatchId("cbatch_analysis_test"),
        CaptureBatchMode.INDEPENDENT,
        (
            ExpectedCaptureAttempt(
                CaptureAttemptId("cattempt_a"),
                RadioId("radio_a"),
                PlanId("plan_a"),
                UtcNs(1_000),
            ),
            ExpectedCaptureAttempt(
                CaptureAttemptId("cattempt_b"),
                RadioId("radio_b"),
                PlanId("plan_b"),
                UtcNs(2_000),
            ),
        ),
    )


def _outcome(
    definition: CaptureBatchDefinition,
    index: int,
    state: CaptureAttemptState,
) -> CaptureAttemptOutcome:
    attempt = definition.expected_attempts[index]
    recording = _recording(str(attempt.attempt_id))
    return CaptureAttemptOutcome(
        SchemaRef(CaptureAttemptOutcome.SCHEMA_ID),
        definition.batch_id,
        attempt.attempt_id,
        attempt.radio_id,
        attempt.plan_id,
        state,
        UtcNs(3_000 + index),
        UtcNs(2_500 + index) if state is CaptureAttemptState.SUCCEEDED else None,
        recording if state is CaptureAttemptState.SUCCEEDED else None,
        None if state is CaptureAttemptState.SUCCEEDED else "peer_failed",
    )


def _snapshot(*states: CaptureAttemptState) -> CaptureBatchSnapshot:
    definition = _definition()
    outcomes = tuple(
        _outcome(definition, index, state) for index, state in enumerate(states)
    )
    return CaptureBatchSnapshot(
        SchemaRef(CaptureBatchSnapshot.SCHEMA_ID),
        definition,
        outcomes,
        len(outcomes),
    )


def _selection() -> ClosedBatchAnalysisSelection:
    return ClosedBatchAnalysisSelection(
        ArtifactRef("algorithm", Digest.sha256(b"algorithm")),
        ArtifactRef("configuration", Digest.sha256(b"configuration")),
        (ArtifactRef("dependency", Digest.sha256(b"dependency")),),
        SchemaRef(FeatureSetBundle.SCHEMA_ID),
    )


class _Catalog:
    def __init__(self, recordings: tuple[PublishedRecordingRef, ...]) -> None:
        self._recordings = {item.recording_id: item for item in recordings}
        self.calls: list[RecordingId] = []

    def get(self, recording_id: RecordingId) -> PublishedRecordingRef | None:
        self.calls.append(recording_id)
        return self._recordings.get(recording_id)


def test_incomplete_batch_is_rejected_before_catalog_or_queue() -> None:
    snapshot = _snapshot(CaptureAttemptState.SUCCEEDED)
    catalog = _Catalog(snapshot.successful_recordings)
    jobs = InMemoryJobLeaseRepository()
    service = ClosedBatchAnalysisSubmissionService(catalog, jobs)

    with pytest.raises(ClosedBatchAnalysisSubmissionError, match="terminal"):
        service.submit(snapshot, _selection())
    assert catalog.calls == []


def test_mutated_catalog_publication_is_rejected_before_any_enqueue() -> None:
    snapshot = _snapshot(CaptureAttemptState.SUCCEEDED, CaptureAttemptState.FAILED)
    claimed = snapshot.successful_recordings[0]
    mutated = PublishedRecordingRef(
        replace(
            claimed.recording_object,
            manifest_digest=Digest.sha256(b"mutated"),
        )
    )
    jobs = InMemoryJobLeaseRepository()
    service = ClosedBatchAnalysisSubmissionService(_Catalog((mutated,)), jobs)

    with pytest.raises(ClosedBatchAnalysisSubmissionError, match="differs"):
        service.submit(snapshot, _selection())
    assert jobs.claim((JobType.RECORDING_ANALYSIS,), "test", 1.0) is None


def test_exact_replay_enqueues_stable_jobs_in_canonical_attempt_order() -> None:
    snapshot = _snapshot(
        CaptureAttemptState.SUCCEEDED,
        CaptureAttemptState.SUCCEEDED,
    )
    catalog = _Catalog(tuple(reversed(snapshot.successful_recordings)))
    jobs = InMemoryJobLeaseRepository()
    service = ClosedBatchAnalysisSubmissionService(catalog, jobs)

    first = service.submit(snapshot, _selection())
    second = service.submit(snapshot, _selection())

    assert first.recording_jobs == second.recording_jobs
    assert tuple(item.request.recording_id for item in first.recording_jobs) == tuple(
        item.recording_id for item in snapshot.successful_recordings
    )
    assert first.paired_analysis_eligibility is PairedAnalysisEligibility.ELIGIBLE
    assert catalog.calls == [
        *(item.recording_id for item in snapshot.successful_recordings),
        *(item.recording_id for item in snapshot.successful_recordings),
    ]


def test_success_after_peer_failure_is_retained_without_paired_science() -> None:
    snapshot = _snapshot(CaptureAttemptState.SUCCEEDED, CaptureAttemptState.FAILED)
    service = ClosedBatchAnalysisSubmissionService(
        _Catalog(snapshot.successful_recordings),
        InMemoryJobLeaseRepository(),
    )

    result = service.submit(snapshot, _selection())

    assert len(result.recording_jobs) == 1
    assert result.recording_jobs[0].request.recording_id == (
        snapshot.successful_recordings[0].recording_id
    )
    assert result.paired_analysis_eligibility is PairedAnalysisEligibility.INELIGIBLE
