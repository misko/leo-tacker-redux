from __future__ import annotations

import psycopg
import pytest

import leo_flow.services.model_submission as submission_module
from leo_flow.analysis.dataset import DatasetSnapshotRef
from leo_flow.analysis.model import (
    AssembledModelInputs,
    EphemerisLinkRequirement,
    ReceiverQualityAggregateConfig,
)
from leo_flow.contracts.core import ArtifactRef, Digest, UtcNs
from leo_flow.contracts.ephemeris import (
    EphemerisSelectionPolicy,
    EphemerisSource,
)
from leo_flow.jobs import JobType
from leo_flow.jobs.postgres_repository import (
    PostgresJobLeaseRepository,
    connection_factory,
)
from leo_flow.services import (
    ModelAnalysisSubmission,
    ModelAnalysisSubmissionService,
)
from tests.model_analysis.fakes import (
    dataset,
    ephemeris_ref,
    feature_set,
    hardware_snapshot,
)
from tests.model_analysis.fakes import (
    request as model_request,
)


class _DatasetReader:
    def get(self, ref: DatasetSnapshotRef) -> object:
        del ref
        return object()


@pytest.mark.integration
def test_exact_duplicate_submission_is_one_postgres_job(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    feature_ref, _feature = feature_set(0, ())
    snapshot = dataset((feature_ref,))
    hardware_ref, _hardware = hardware_snapshot()
    request = model_request(
        snapshot,
        ReceiverQualityAggregateConfig(),
        (hardware_ref,),
        (ephemeris_ref(),),
    )
    durable_ref = DatasetSnapshotRef(
        request.dataset_snapshot_ref.snapshot_id,
        request.dataset_snapshot_ref.membership_digest,
        Digest.sha256(b"durable-dataset"),
    )
    assembled = AssembledModelInputs(request, (), ())
    monkeypatch.setattr(
        submission_module,
        "assemble_model_inputs",
        lambda **_arguments: assembled,
    )
    jobs = PostgresJobLeaseRepository(connection_factory(postgres_dsn))
    service = ModelAnalysisSubmissionService(
        datasets=_DatasetReader(),  # type: ignore[arg-type]
        features=object(),  # type: ignore[arg-type]
        recordings=object(),  # type: ignore[arg-type]
        hardware_links=object(),  # type: ignore[arg-type]
        ephemeris_links=object(),  # type: ignore[arg-type]
        jobs=jobs,
    )
    submission = ModelAnalysisSubmission(
        durable_ref,
        EphemerisLinkRequirement(
            EphemerisSource.SPACE_TRACK,
            "leo",
            EphemerisSelectionPolicy.AVAILABLE_THEN,
            ArtifactRef("policy", Digest.sha256(b"policy")),
            UtcNs(1),
        ),
        request.model_config_ref,
        request.algorithm_ref,
    )

    first = service.submit(submission)
    second = service.submit(submission)

    assert first.job_id == second.job_id
    with psycopg.connect(postgres_dsn) as connection:
        row = connection.execute(
            "SELECT count(*), min(job_type) FROM job WHERE job_id = %s",
            (str(first.job_id),),
        ).fetchone()
    assert row == (1, JobType.MODEL_ANALYSIS.value)
