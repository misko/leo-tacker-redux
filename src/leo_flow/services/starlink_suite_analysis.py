"""Fenced durable jobs for the v0.2 Starlink report detector suite."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts._validation import thaw_value
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SchemaVersion,
    SegmentId,
    UtcNs,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_suite_pipeline import (
    StarlinkDetectorSuiteRecordingAnalyzerV0_2,
    StarlinkDetectorSuiteRecordingBundleV0_2,
    StarlinkDetectorSuiteRequestV0_2,
    StarlinkSuiteStreamSelectionV0_2,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.jobs.contracts import JobLease, JobPayload, JobType
from leo_flow.jobs.ports import JobLeaseRepository, StaleLeaseError
from leo_flow.storage.ports import RecordingObjectReader

STARLINK_SUITE_ANALYSIS_JOB_SCHEMA = SchemaRef(
    "org.leo-flow.starlink-detector-suite-analysis-job", SchemaVersion(0, 2)
)


class StarlinkSuiteAnalysisJobError(ValueError):
    pass


@dataclass(frozen=True)
class PreparedStarlinkSuiteAnalysisV0_2:
    request: StarlinkDetectorSuiteRequestV0_2
    bundle: StarlinkDetectorSuiteRecordingBundleV0_2


class StarlinkSuiteAnalysisCommitterV0_2(Protocol):
    def commit_starlink_suite(
        self, lease: JobLease, prepared: PreparedStarlinkSuiteAnalysisV0_2
    ) -> ArtifactRef: ...


class StarlinkSuiteAnalysisJobPreparerV0_2:
    def __init__(
        self,
        reader: RecordingObjectReader,
        analyzer: StarlinkDetectorSuiteRecordingAnalyzerV0_2,
    ) -> None:
        self._reader = reader
        self._analyzer = analyzer

    def prepare(self, lease: JobLease) -> PreparedStarlinkSuiteAnalysisV0_2:
        if lease.job_type is not JobType.STARLINK_SUITE_ANALYSIS:
            raise StarlinkSuiteAnalysisJobError(
                "worker accepts detector-suite jobs only"
            )
        request = decode_starlink_suite_analysis_payload(lease.payload)
        with self._reader.open(request.recording_object_ref) as recording:
            bundle = self._analyzer.analyze_starlink_suite(recording, request)
        return PreparedStarlinkSuiteAnalysisV0_2(request, bundle)


class FencedStarlinkSuiteAnalysisWorkerV0_2:
    def __init__(
        self,
        jobs: JobLeaseRepository,
        preparer: StarlinkSuiteAnalysisJobPreparerV0_2,
        committer: StarlinkSuiteAnalysisCommitterV0_2,
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
            raise ValueError("detector-suite worker bounds must be positive")
        self._jobs, self._preparer, self._committer = jobs, preparer, committer
        self._worker_id, self._lease_ttl_s = worker_id, lease_ttl_s
        self._maximum_attempts = maximum_attempts
        self._retry_delay_ns = round(retry_delay_s * 1_000_000_000)
        self._clock_ns = clock_ns or time.time_ns

    def process_one_job(self) -> bool:
        lease = self._jobs.claim(
            (JobType.STARLINK_SUITE_ANALYSIS,), self._worker_id, self._lease_ttl_s
        )
        if lease is None:
            return False
        self.execute(lease)
        return True

    def execute(self, lease: JobLease) -> ArtifactRef | None:
        if lease.job_type is not JobType.STARLINK_SUITE_ANALYSIS:
            raise StarlinkSuiteAnalysisJobError(
                "worker accepts detector-suite jobs only"
            )
        try:
            return self._committer.commit_starlink_suite(
                lease, self._preparer.prepare(lease)
            )
        except ValueError:
            self._park(lease, "starlink-suite-invalid-input")
        except Exception:  # noqa: BLE001
            if lease.attempt >= self._maximum_attempts:
                self._park(lease, "starlink-suite-attempts-exhausted")
            else:
                try:
                    self._jobs.fail(
                        lease.job_id,
                        lease.lease_token,
                        lease.lease_generation,
                        "starlink-suite-transient-failure",
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


def starlink_suite_analysis_payload(
    request: StarlinkDetectorSuiteRequestV0_2,
) -> JobPayload:
    return JobPayload.create(
        STARLINK_SUITE_ANALYSIS_JOB_SCHEMA,
        {
            "request_schema": _schema_doc(request.schema),
            "recording_id": str(request.recording_id),
            "recording_object_ref": _recording_doc(request.recording_object_ref),
            "algorithm_ref": _artifact_doc(request.algorithm_ref),
            "config_ref": _artifact_doc(request.config_ref),
            "stream_selections": {
                str(index): {
                    "segment_id": str(item.segment_id),
                    "receiver_chain_id": str(item.receiver_chain_id),
                    "edge": item.edge.value,
                    "exact_template_ref": _artifact_doc(item.exact_template_ref),
                    "conditioned_control_template_ref": _artifact_doc(
                        item.conditioned_control_template_ref
                    ),
                    "probe_sample_count": item.probe_sample_count,
                }
                for index, item in enumerate(request.stream_selections)
            },
            "requested_output_schema": _schema_doc(request.requested_output_schema),
            "ineligible_reason": request.ineligible_reason,
        },
    )


def decode_starlink_suite_analysis_payload(
    payload: JobPayload,
) -> StarlinkDetectorSuiteRequestV0_2:
    if payload.schema != STARLINK_SUITE_ANALYSIS_JOB_SCHEMA:
        raise StarlinkSuiteAnalysisJobError("unsupported detector-suite job schema")
    try:
        root = _map(thaw_value(payload.value))
        entries = _map(root["stream_selections"])
        if set(entries) != {str(index) for index in range(len(entries))}:
            raise StarlinkSuiteAnalysisJobError("suite selection indices are invalid")
        recording = _recording(root["recording_object_ref"])
        return StarlinkDetectorSuiteRequestV0_2(
            _schema(root["request_schema"]),
            RecordingId(_str(root["recording_id"])),
            recording,
            _artifact(root["algorithm_ref"]),
            _artifact(root["config_ref"]),
            tuple(_selection(entries[str(index)]) for index in range(len(entries))),
            _schema(root["requested_output_schema"]),
            None
            if root["ineligible_reason"] is None
            else _str(root["ineligible_reason"]),
        )
    except StarlinkSuiteAnalysisJobError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise StarlinkSuiteAnalysisJobError(str(error)) from error


def _selection(value: object) -> StarlinkSuiteStreamSelectionV0_2:
    item = _map(value)
    return StarlinkSuiteStreamSelectionV0_2(
        SegmentId(_str(item["segment_id"])),
        ReceiverChainId(_str(item["receiver_chain_id"])),
        StarlinkEdge(_str(item["edge"])),
        _artifact(item["exact_template_ref"]),
        _artifact(item["conditioned_control_template_ref"]),
        _int(item["probe_sample_count"]),
    )


def _recording(value: object) -> RecordingObjectRef:
    item = _map(value)
    return RecordingObjectRef(
        RecordingId(_str(item["recording_id"])),
        _object_ref(item["data_object"]),
        _object_ref(item["metadata_object"]),
        _digest(item["manifest_digest"]),
    )


def _recording_doc(value: RecordingObjectRef) -> dict[str, object]:
    return {
        "recording_id": str(value.recording_id),
        "data_object": _object_doc(value.data_object),
        "metadata_object": _object_doc(value.metadata_object),
        "manifest_digest": _digest_doc(value.manifest_digest),
    }


def _object_ref(value: object) -> ObjectRef:
    item = _map(value)
    return ObjectRef(
        _digest(item["digest"]),
        _int(item["byte_count"]),
        _str(item["media_type"]),
        _str(item["format_id"]),
        _str(item["locator"]),
    )


def _object_doc(value: ObjectRef) -> dict[str, object]:
    return {
        "digest": _digest_doc(value.digest),
        "byte_count": value.byte_count,
        "media_type": value.media_type,
        "format_id": value.format_id,
        "locator": value.locator,
    }


def _artifact(value: object) -> ArtifactRef:
    item = _map(value)
    return ArtifactRef(
        _str(item["artifact_id"]),
        _digest(item["digest"]),
        None if item["schema"] is None else _schema(item["schema"]),
    )


def _artifact_doc(value: ArtifactRef) -> dict[str, object]:
    return {
        "artifact_id": value.artifact_id,
        "digest": _digest_doc(value.digest),
        "schema": None if value.schema is None else _schema_doc(value.schema),
    }


def _schema(value: object) -> SchemaRef:
    item = _map(value)
    version = _map(item["version"])
    return SchemaRef(
        _str(item["schema_id"]),
        SchemaVersion(_int(version["major"]), _int(version["minor"])),
    )


def _schema_doc(value: SchemaRef) -> dict[str, object]:
    return {
        "schema_id": value.schema_id,
        "version": {"major": value.version.major, "minor": value.version.minor},
    }


def _digest(value: object) -> Digest:
    item = _map(value)
    return Digest(DigestAlgorithm(_str(item["algorithm"])), _str(item["value"]))


def _digest_doc(value: Digest) -> dict[str, str]:
    return {"algorithm": value.algorithm.value, "value": value.value}


def _map(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise StarlinkSuiteAnalysisJobError("expected object")
    return value


def _str(value: object) -> str:
    if not isinstance(value, str):
        raise StarlinkSuiteAnalysisJobError("expected string")
    return value


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StarlinkSuiteAnalysisJobError("expected integer")
    return value
