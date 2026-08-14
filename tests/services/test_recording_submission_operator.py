from __future__ import annotations

import json
from dataclasses import replace
from io import StringIO
from pathlib import Path

import pytest

import leo_flow.deployments.recording_submission_v1 as deployment
from leo_flow.contracts.core import ArtifactRef, RecordingId, canonical_json_bytes
from leo_flow.contracts.storage import PublishedRecordingRef
from leo_flow.jobs import InMemoryJobLeaseRepository, JobState
from leo_flow.services.recording_submission import (
    RecordingAnalysisSubmission,
    RecordingAnalysisSubmissionService,
)
from leo_flow.services.recording_submission_operator import (
    ExactRecordingAnalysisSelection,
    RecordingAnalysisSubmissionOperator,
    RecordingSubmissionOperatorError,
    load_recording_submission_config,
)
from tests.recording_analysis.test_feature_persistence import _fixture


class _Catalog:
    def __init__(self, publication: PublishedRecordingRef | None) -> None:
        self.publication = publication
        self.lookups: list[RecordingId] = []

    def get(self, recording_id):
        self.lookups.append(recording_id)
        return self.publication


def _document() -> dict[str, object]:
    request, _ = _fixture()
    return {
        "schema": "org.leo-flow.recording-analysis-submission",
        "version": "0.1",
        "recording_id": str(request.recording_id),
        "analysis": {
            "algorithm_ref": _artifact(request.algorithm_ref),
            "config_ref": _artifact(request.config_ref),
            "dependency_refs": [_artifact(ref) for ref in request.dependency_refs],
            "requested_output_schema": {
                "schema_id": request.requested_output_schema.schema_id,
                "version": str(request.requested_output_schema.version),
            },
        },
        "database_dsn": {
            "provider": "systemd-credential",
            "name": "catalog-dsn",
        },
    }


def _artifact(ref: ArtifactRef) -> dict[str, object]:
    return {
        "artifact_id": ref.artifact_id,
        "digest": {
            "algorithm": ref.digest.algorithm.value,
            "value": ref.digest.value,
        },
        "schema": (
            None
            if ref.schema is None
            else {
                "schema_id": ref.schema.schema_id,
                "version": str(ref.schema.version),
            }
        ),
    }


def _write(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "submission.json"
    path.write_bytes(canonical_json_bytes(document))
    return path


def test_strict_config_loads_every_exact_identity_without_a_secret(
    tmp_path: Path,
) -> None:
    request, _ = _fixture()

    config = load_recording_submission_config(_write(tmp_path, _document()))

    assert config.dsn_credential_name == "catalog-dsn"
    assert config.selection == ExactRecordingAnalysisSelection(
        request.recording_id,
        request.algorithm_ref,
        request.config_ref,
        request.dependency_refs,
        request.requested_output_schema,
    )


def test_config_rejects_defaults_unknown_fields_duplicates_and_raw_dsn(
    tmp_path: Path,
) -> None:
    document = _document()
    analysis = document["analysis"]
    assert isinstance(analysis, dict)
    del analysis["config_ref"]
    with pytest.raises(RecordingSubmissionOperatorError, match="fields"):
        load_recording_submission_config(_write(tmp_path, document))

    path = tmp_path / "duplicate.json"
    path.write_text('{"schema":"one","schema":"two"}', encoding="utf-8")
    with pytest.raises(RecordingSubmissionOperatorError, match="duplicate"):
        load_recording_submission_config(path)

    document = _document()
    document["database_dsn"] = {
        "provider": "systemd-credential",
        "name": "catalog-dsn",
        "value": "postgresql://user:password@database/catalog",
    }
    with pytest.raises(RecordingSubmissionOperatorError, match="fields"):
        load_recording_submission_config(_write(tmp_path, document))


def test_operator_resolves_publication_then_submits_one_idempotent_job() -> None:
    request, _ = _fixture()
    publication = PublishedRecordingRef(request.recording_object_ref)
    catalog = _Catalog(publication)
    jobs = InMemoryJobLeaseRepository(now_utc_ns=lambda: 100)
    operator = RecordingAnalysisSubmissionOperator(recordings=catalog, jobs=jobs)
    selection = ExactRecordingAnalysisSelection(
        request.recording_id,
        request.algorithm_ref,
        request.config_ref,
        request.dependency_refs,
        request.requested_output_schema,
    )

    first = operator.submit(selection)
    second = operator.submit(selection)

    assert first == second
    assert first.request == request
    assert jobs.snapshot(first.job_id).state is JobState.READY
    assert catalog.lookups == [request.recording_id, request.recording_id]


def test_missing_or_substituted_publication_fails_before_enqueue() -> None:
    request, _ = _fixture()
    jobs = InMemoryJobLeaseRepository(now_utc_ns=lambda: 100)
    selection = ExactRecordingAnalysisSelection(
        request.recording_id,
        request.algorithm_ref,
        request.config_ref,
        request.dependency_refs,
        request.requested_output_schema,
    )
    missing = RecordingAnalysisSubmissionOperator(recordings=_Catalog(None), jobs=jobs)
    with pytest.raises(RecordingSubmissionOperatorError, match="not present"):
        missing.submit(selection)

    substituted_ref = replace(
        request.recording_object_ref,
        recording_id=type(request.recording_id)("rec_substituted"),
    )
    substituted = RecordingAnalysisSubmissionOperator(
        recordings=_Catalog(PublishedRecordingRef(substituted_ref)), jobs=jobs
    )
    with pytest.raises(RecordingSubmissionOperatorError, match="substituted"):
        substituted.submit(selection)


def test_cli_reports_only_durable_identity_and_sanitizes_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, _ = _fixture()
    submitted = RecordingAnalysisSubmissionService(
        InMemoryJobLeaseRepository(now_utc_ns=lambda: 100)
    ).submit(
        RecordingAnalysisSubmission(
            PublishedRecordingRef(request.recording_object_ref),
            request.algorithm_ref,
            request.config_ref,
            request.dependency_refs,
            request.requested_output_schema,
        )
    )
    monkeypatch.setattr(
        deployment, "submit_recording_analysis", lambda _config: submitted
    )
    stdout, stderr = StringIO(), StringIO()

    assert (
        deployment.main(
            ["--config", str(_write(tmp_path, _document()))],
            stdout=stdout,
            stderr=stderr,
        )
        == 0
    )
    result = json.loads(stdout.getvalue())
    assert result == {
        "event": "recording_analysis_submitted",
        "job_id": str(submitted.job_id),
        "recording_id": str(request.recording_id),
        "request_schema_id": request.schema.schema_id,
        "request_schema_version": str(request.schema.version),
    }
    assert stderr.getvalue() == ""

    secret_named_path = tmp_path / "postgresql-password-do-not-leak.json"
    secret_named_path.write_text("not json", encoding="utf-8")
    stdout, stderr = StringIO(), StringIO()
    assert (
        deployment.main(
            ["--config", str(secret_named_path)], stdout=stdout, stderr=stderr
        )
        == 3
    )
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == ('{"event":"recording_analysis_submission_failed"}\n')
    assert "password" not in stderr.getvalue()
