"""Typed, single-recording preparation boundary for analysis jobs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, cast

from leo_flow.contracts._validation import thaw_value
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    SchemaRef,
    SchemaVersion,
)
from leo_flow.contracts.features import (
    FeatureSetBundle,
    RecordingAnalysisRequest,
)
from leo_flow.contracts.ports import RecordingAnalyzer
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.jobs.contracts import JobLease, JobPayload, JobType
from leo_flow.jobs.ports import JobLeaseRepository, StaleLeaseError
from leo_flow.storage.ports import RecordingObjectReader

RECORDING_ANALYSIS_JOB_SCHEMA = SchemaRef("org.leo-flow.recording-analysis-job")


class RecordingAnalysisJobError(ValueError):
    """A job is not the exact supported recording-analysis command."""


@dataclass(frozen=True)
class PreparedRecordingAnalysis:
    request: RecordingAnalysisRequest
    bundle: FeatureSetBundle


class RecordingAnalysisPreparer(Protocol):
    def prepare(self, lease: JobLease) -> PreparedRecordingAnalysis: ...


class RecordingAnalysisCommitter(Protocol):
    def commit(
        self, lease: JobLease, prepared: PreparedRecordingAnalysis
    ) -> ArtifactRef: ...


class RecordingAnalysisJobPreparer:
    """Decode one typed payload, open its exact recording, and run analysis."""

    def __init__(
        self, reader: RecordingObjectReader, analyzer: RecordingAnalyzer
    ) -> None:
        self._reader = reader
        self._analyzer = analyzer

    def prepare(self, lease: JobLease) -> PreparedRecordingAnalysis:
        if lease.job_type is not JobType.RECORDING_ANALYSIS:
            raise RecordingAnalysisJobError(
                "worker accepts recording-analysis jobs only"
            )
        request = decode_recording_analysis_payload(lease.payload)
        with self._reader.open(request.recording_object_ref) as recording:
            bundle = self._analyzer.analyze(recording, request)
        return PreparedRecordingAnalysis(request, bundle)


class FencedRecordingAnalysisWorker:
    """Claim and process only recording-analysis jobs through an atomic committer."""

    def __init__(
        self,
        jobs: JobLeaseRepository,
        preparer: RecordingAnalysisPreparer,
        committer: RecordingAnalysisCommitter,
        *,
        worker_id: str,
        lease_ttl_s: float,
    ) -> None:
        if not worker_id or lease_ttl_s <= 0:
            raise ValueError("worker identity and positive lease TTL are required")
        self._jobs = jobs
        self._preparer = preparer
        self._committer = committer
        self._worker_id = worker_id
        self._lease_ttl_s = lease_ttl_s

    def process_one_job(self) -> bool:
        lease = self._jobs.claim(
            (JobType.RECORDING_ANALYSIS,), self._worker_id, self._lease_ttl_s
        )
        if lease is None:
            return False
        try:
            prepared = self._preparer.prepare(lease)
            self._committer.commit(lease, prepared)
        except Exception as error:
            try:
                self._jobs.fail(
                    lease.job_id,
                    lease.lease_token,
                    lease.lease_generation,
                    f"{type(error).__name__}: recording analysis failed",
                    None,
                )
            except StaleLeaseError:
                pass
            raise
        return True


def recording_analysis_payload(request: RecordingAnalysisRequest) -> JobPayload:
    return JobPayload.create(
        RECORDING_ANALYSIS_JOB_SCHEMA,
        {
            "request_schema": _schema_document(request.schema),
            "recording_id": str(request.recording_id),
            "recording_object_ref": {
                "recording_id": str(request.recording_object_ref.recording_id),
                "data_object": _object_document(
                    request.recording_object_ref.data_object
                ),
                "metadata_object": _object_document(
                    request.recording_object_ref.metadata_object
                ),
                "manifest_digest": _digest_document(
                    request.recording_object_ref.manifest_digest
                ),
            },
            "algorithm_ref": _artifact_document(request.algorithm_ref),
            "config_ref": _artifact_document(request.config_ref),
            "dependency_refs": [
                _artifact_document(value) for value in request.dependency_refs
            ],
            "requested_output_schema": _schema_document(
                request.requested_output_schema
            ),
        },
    )


def decode_recording_analysis_payload(payload: JobPayload) -> RecordingAnalysisRequest:
    if payload.schema != RECORDING_ANALYSIS_JOB_SCHEMA:
        raise RecordingAnalysisJobError("unsupported recording-analysis job schema")
    document = cast(dict[str, object], thaw_value(payload.value))
    _keys(
        document,
        {
            "request_schema",
            "recording_id",
            "recording_object_ref",
            "algorithm_ref",
            "config_ref",
            "dependency_refs",
            "requested_output_schema",
        },
        "payload",
    )
    try:
        recording = _recording_ref(document["recording_object_ref"])
        dependencies = document["dependency_refs"]
        if not isinstance(dependencies, list):
            raise RecordingAnalysisJobError("dependency_refs must be an array")
        return RecordingAnalysisRequest(
            schema=_schema(document["request_schema"], "request_schema"),
            recording_id=type(recording.recording_id)(
                _string(document["recording_id"], "recording_id")
            ),
            recording_object_ref=recording,
            algorithm_ref=_artifact(document["algorithm_ref"], "algorithm_ref"),
            config_ref=_artifact(document["config_ref"], "config_ref"),
            dependency_refs=tuple(
                _artifact(value, f"dependency_refs[{index}]")
                for index, value in enumerate(dependencies)
            ),
            requested_output_schema=_schema(
                document["requested_output_schema"], "requested_output_schema"
            ),
        )
    except RecordingAnalysisJobError:
        raise
    except (TypeError, ValueError) as error:
        raise RecordingAnalysisJobError(str(error)) from error


def _recording_ref(value: object) -> RecordingObjectRef:
    from leo_flow.contracts.core import RecordingId

    item = _object(value, "recording_object_ref")
    _keys(
        item,
        {"recording_id", "data_object", "metadata_object", "manifest_digest"},
        "recording_object_ref",
    )
    return RecordingObjectRef(
        RecordingId(_string(item["recording_id"], "recording_id")),
        _object_ref(item["data_object"], "data_object"),
        _object_ref(item["metadata_object"], "metadata_object"),
        _digest(item["manifest_digest"], "manifest_digest"),
    )


def _artifact(value: object, name: str) -> ArtifactRef:
    item = _object(value, name)
    _keys(item, {"artifact_id", "digest", "schema"}, name)
    return ArtifactRef(
        _string(item["artifact_id"], f"{name}.artifact_id"),
        _digest(item["digest"], f"{name}.digest"),
        None if item["schema"] is None else _schema(item["schema"], f"{name}.schema"),
    )


def _object_ref(value: object, name: str) -> ObjectRef:
    item = _object(value, name)
    _keys(item, {"digest", "byte_count", "media_type", "format_id", "locator"}, name)
    byte_count = item["byte_count"]
    if isinstance(byte_count, bool) or not isinstance(byte_count, int):
        raise RecordingAnalysisJobError(f"{name}.byte_count must be an integer")
    return ObjectRef(
        _digest(item["digest"], f"{name}.digest"),
        byte_count,
        _string(item["media_type"], f"{name}.media_type"),
        _string(item["format_id"], f"{name}.format_id"),
        _string(item["locator"], f"{name}.locator"),
    )


def _schema(value: object, name: str) -> SchemaRef:
    item = _object(value, name)
    _keys(item, {"schema_id", "version"}, name)
    return SchemaRef(
        _string(item["schema_id"], f"{name}.schema_id"),
        SchemaVersion.parse(_string(item["version"], f"{name}.version")),
    )


def _digest(value: object, name: str) -> Digest:
    item = _object(value, name)
    _keys(item, {"algorithm", "value"}, name)
    return Digest(
        DigestAlgorithm(_string(item["algorithm"], f"{name}.algorithm")),
        _string(item["value"], f"{name}.value"),
    )


def _schema_document(value: SchemaRef) -> dict[str, object]:
    return {"schema_id": value.schema_id, "version": str(value.version)}


def _digest_document(value: Digest) -> dict[str, object]:
    return {"algorithm": value.algorithm.value, "value": value.value}


def _object_document(value: ObjectRef) -> dict[str, object]:
    return {
        "digest": _digest_document(value.digest),
        "byte_count": value.byte_count,
        "media_type": value.media_type,
        "format_id": value.format_id,
        "locator": value.locator,
    }


def _artifact_document(value: ArtifactRef) -> dict[str, object]:
    return {
        "artifact_id": value.artifact_id,
        "digest": _digest_document(value.digest),
        "schema": None if value.schema is None else _schema_document(value.schema),
    }


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RecordingAnalysisJobError(f"{name} must be an object")
    return cast(dict[str, object], value)


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise RecordingAnalysisJobError(f"{name} must be a string")
    return value


def _keys(value: dict[str, object], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise RecordingAnalysisJobError(f"{name} fields differ from the schema")
