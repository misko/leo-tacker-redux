from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.contracts.core import ArtifactRef
from leo_flow.jobs import InMemoryJobLeaseRepository, JobType
from leo_flow.services import (
    ModelAnalysisSubmission,
    ModelAnalysisSubmissionService,
)
from leo_flow.services.model_analysis import decode_model_analysis_payload
from tests.model_analysis.test_model_input_assembly import (
    REQUIREMENT,
    _digest,
    _fixture,
)


class _DatasetReader:
    def __init__(self, value) -> None:
        self.value = value
        self.calls = []

    def get(self, ref):
        self.calls.append(ref)
        return self.value


class _CountingJobs(InMemoryJobLeaseRepository):
    def __init__(self) -> None:
        super().__init__()
        self.enqueue_calls = 0

    def enqueue(self, *args, **kwargs) -> None:
        self.enqueue_calls += 1
        super().enqueue(*args, **kwargs)


def _service(fixture, datasets, jobs):
    return ModelAnalysisSubmissionService(
        datasets=datasets,
        features=fixture.features,
        recordings=fixture.recordings,
        hardware_links=fixture.hardware,
        ephemeris_links=fixture.ephemeris,
        jobs=jobs,
    )


def _submission(fixture) -> ModelAnalysisSubmission:
    return ModelAnalysisSubmission(
        dataset_ref=fixture.dataset.ref,
        ephemeris_requirement=REQUIREMENT,
        model_config_ref=ArtifactRef("model-config", _digest("config")),
        algorithm_ref=ArtifactRef("model-algorithm", _digest("algorithm")),
    )


def test_duplicate_exact_submissions_create_one_restart_safe_job() -> None:
    fixture = _fixture()
    datasets = _DatasetReader(fixture.dataset)
    jobs = _CountingJobs()
    service = _service(fixture, datasets, jobs)

    first = service.submit(_submission(fixture))
    second = service.submit(_submission(fixture))

    assert first.job_id == second.job_id
    assert first.payload == second.payload
    assert jobs.enqueue_calls == 2
    lease = jobs.claim((JobType.MODEL_ANALYSIS,), "worker", 10.0)
    assert lease is not None
    assert lease.job_id == first.job_id
    assert jobs.claim((JobType.MODEL_ANALYSIS,), "other", 10.0) is None
    request, durable_ref = decode_model_analysis_payload(lease.payload)
    assert durable_ref == fixture.dataset.ref
    assert request == first.assembled_inputs.request
    assert request.model_config_ref == _submission(fixture).model_config_ref
    assert request.algorithm_ref == _submission(fixture).algorithm_ref


def test_dataset_substitution_fails_before_enqueue() -> None:
    fixture = _fixture()
    substituted_ref = replace(
        fixture.dataset.ref,
        snapshot_digest=_digest("substituted-dataset-bundle"),
    )
    jobs = _CountingJobs()
    service = _service(fixture, _DatasetReader(fixture.dataset), jobs)
    submission = replace(_submission(fixture), dataset_ref=substituted_ref)

    with pytest.raises(ValueError, match="dataset snapshot was substituted"):
        service.submit(submission)

    assert jobs.enqueue_calls == 0


def test_missing_exact_ephemeris_regime_fails_before_enqueue() -> None:
    fixture = _fixture()
    jobs = _CountingJobs()
    service = _service(fixture, _DatasetReader(fixture.dataset), jobs)
    submission = replace(
        _submission(fixture),
        ephemeris_requirement=replace(REQUIREMENT, scope="different-scope"),
    )

    with pytest.raises(ValueError, match="no exact authoritative ephemeris link"):
        service.submit(submission)

    assert jobs.enqueue_calls == 0
