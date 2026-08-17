"""Typed, fenced, post-capture waterfall analysis jobs."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol, cast

from leo_flow.analysis.recording.api import (
    AnalysisConfigurationError,
    AnalysisInputError,
)
from leo_flow.contracts._validation import thaw_value
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    RecordingId,
    SchemaRef,
    SchemaVersion,
    UtcNs,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.contracts.waterfall import (
    WaterfallAnalysisRequestV0_1,
    WaterfallAnalyzerV0_1,
    WaterfallBundleV0_1,
)
from leo_flow.jobs.contracts import JobLease, JobPayload, JobType
from leo_flow.jobs.ports import JobLeaseRepository, StaleLeaseError
from leo_flow.storage.ports import RecordingObjectReader

WATERFALL_ANALYSIS_JOB_SCHEMA = SchemaRef("org.leo-flow.waterfall-analysis-job")


class WaterfallAnalysisJobError(ValueError):
    """A job is not the exact supported waterfall-analysis command."""


@dataclass(frozen=True)
class PreparedWaterfallAnalysisV0_1:
    request: WaterfallAnalysisRequestV0_1
    bundle: WaterfallBundleV0_1


class WaterfallAnalysisCommitterV0_1(Protocol):
    def commit_waterfall(
        self, lease: JobLease, prepared: PreparedWaterfallAnalysisV0_1
    ) -> ArtifactRef: ...


class WaterfallAnalysisPreparerV0_1(Protocol):
    """Narrow worker port; enhanced preparations remain a v0.1 subtype."""

    def prepare(self, lease: JobLease) -> PreparedWaterfallAnalysisV0_1: ...


class WaterfallAnalysisJobPreparerV0_1:
    def __init__(
        self, reader: RecordingObjectReader, analyzer: WaterfallAnalyzerV0_1
    ) -> None:
        self._reader = reader
        self._analyzer = analyzer

    def prepare(self, lease: JobLease) -> PreparedWaterfallAnalysisV0_1:
        if lease.job_type is not JobType.WATERFALL_ANALYSIS:
            raise WaterfallAnalysisJobError("worker accepts waterfall jobs only")
        request = decode_waterfall_analysis_payload(lease.payload)
        with self._reader.open(request.recording_object_ref) as recording:
            bundle = self._analyzer.analyze_waterfall(recording, request)
        return PreparedWaterfallAnalysisV0_1(request, bundle)


class FencedWaterfallAnalysisWorkerV0_1:
    """Retry transient failures and park bounded permanent/exhausted failures."""

    def __init__(
        self,
        jobs: JobLeaseRepository,
        preparer: WaterfallAnalysisPreparerV0_1,
        committer: WaterfallAnalysisCommitterV0_1,
        *,
        worker_id: str,
        lease_ttl_s: float,
        maximum_attempts: int = 3,
        retry_delay_s: float = 5.0,
        clock_ns: ProtocolClock | None = None,
    ) -> None:
        if (
            not worker_id
            or lease_ttl_s <= 0
            or maximum_attempts <= 0
            or retry_delay_s <= 0
        ):
            raise ValueError("waterfall worker bounds must be positive")
        self._jobs = jobs
        self._preparer = preparer
        self._committer = committer
        self._worker_id = worker_id
        self._lease_ttl_s = lease_ttl_s
        self._maximum_attempts = maximum_attempts
        self._retry_delay_ns = round(retry_delay_s * 1_000_000_000)
        self._clock_ns = clock_ns or time.time_ns

    def process_one_job(self) -> bool:
        lease = self._jobs.claim(
            (JobType.WATERFALL_ANALYSIS,), self._worker_id, self._lease_ttl_s
        )
        if lease is None:
            return False
        self.execute(lease)
        return True

    def execute(self, lease: JobLease) -> ArtifactRef | None:
        if lease.job_type is not JobType.WATERFALL_ANALYSIS:
            raise WaterfallAnalysisJobError("worker accepts waterfall jobs only")
        try:
            prepared = self._preparer.prepare(lease)
            return self._committer.commit_waterfall(lease, prepared)
        except (
            WaterfallAnalysisJobError,
            AnalysisConfigurationError,
            AnalysisInputError,
        ):
            self._park(lease, "waterfall-invalid-input")
            return None
        except Exception:  # noqa: BLE001 - bounded retry isolates worker failures
            if lease.attempt >= self._maximum_attempts:
                self._park(lease, "waterfall-attempts-exhausted")
            else:
                try:
                    self._jobs.fail(
                        lease.job_id,
                        lease.lease_token,
                        lease.lease_generation,
                        "waterfall-analysis-transient-failure",
                        UtcNs(self._clock_ns() + self._retry_delay_ns),
                    )
                except StaleLeaseError:
                    pass
            return None

    def _park(self, lease: JobLease, reason: str) -> None:
        try:
            self._jobs.park(
                lease.job_id, lease.lease_token, lease.lease_generation, reason
            )
        except StaleLeaseError:
            pass


class ProtocolClock(Protocol):
    def __call__(self) -> int: ...


def waterfall_analysis_payload(request: WaterfallAnalysisRequestV0_1) -> JobPayload:
    return JobPayload.create(
        WATERFALL_ANALYSIS_JOB_SCHEMA,
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
            "dependency_refs": {
                str(index): _artifact_document(value)
                for index, value in enumerate(request.dependency_refs)
            },
            "requested_output_schema": _schema_document(
                request.requested_output_schema
            ),
        },
    )


def decode_waterfall_analysis_payload(
    payload: JobPayload,
) -> WaterfallAnalysisRequestV0_1:
    if payload.schema != WATERFALL_ANALYSIS_JOB_SCHEMA:
        raise WaterfallAnalysisJobError("unsupported waterfall job schema")
    root = _object(thaw_value(payload.value), "payload")
    _keys(
        root,
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
        recording = _recording_ref(root["recording_object_ref"])
        dependencies = _object(root["dependency_refs"], "dependency_refs")
        if set(dependencies) != {str(i) for i in range(len(dependencies))}:
            raise WaterfallAnalysisJobError("dependency indices are invalid")
        return WaterfallAnalysisRequestV0_1(
            _schema(root["request_schema"], "request_schema"),
            RecordingId(_string(root["recording_id"], "recording_id")),
            recording,
            _artifact(root["algorithm_ref"], "algorithm_ref"),
            _artifact(root["config_ref"], "config_ref"),
            tuple(
                _artifact(dependencies[str(i)], f"dependency_refs[{i}]")
                for i in range(len(dependencies))
            ),
            _schema(root["requested_output_schema"], "requested_output_schema"),
        )
    except WaterfallAnalysisJobError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise WaterfallAnalysisJobError(str(error)) from error


def _recording_ref(value: object) -> RecordingObjectRef:
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
    count = item["byte_count"]
    if isinstance(count, bool) or not isinstance(count, int):
        raise WaterfallAnalysisJobError(f"{name}.byte_count must be an integer")
    return ObjectRef(
        _digest(item["digest"], f"{name}.digest"),
        count,
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
        raise WaterfallAnalysisJobError(f"{name} must be an object")
    return cast(dict[str, object], value)


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise WaterfallAnalysisJobError(f"{name} must be a string")
    return value


def _keys(value: dict[str, object], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise WaterfallAnalysisJobError(f"{name} fields differ from schema")
