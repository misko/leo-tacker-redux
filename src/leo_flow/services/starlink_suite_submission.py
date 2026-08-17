"""Exact idempotent submission for the complete Starlink detector suite."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from leo_flow.analysis.recording.starlink_templates import (
    qin_edge_pilot_template_pair_v0_1,
)
from leo_flow.contracts._validation import thaw_value
from leo_flow.contracts.capture import RecordingManifest
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    JobId,
    SchemaRef,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_detector_suite import V0_2
from leo_flow.contracts.starlink_suite_pipeline import (
    StarlinkDetectorSuiteRecordingBundleV0_2,
    StarlinkDetectorSuiteRequestV0_2,
    StarlinkSuiteStreamSelectionV0_2,
)
from leo_flow.contracts.storage import PublishedRecordingRef
from leo_flow.jobs.contracts import JobPayload, JobType

from .starlink_suite_analysis import starlink_suite_analysis_payload


class StarlinkSuiteJobEnqueuer(Protocol):
    def enqueue(
        self,
        job_id: JobId,
        job_type: JobType,
        payload: JobPayload,
        *,
        available_at_utc_ns: UtcNs | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class StarlinkSuiteAnalysisSubmissionV0_2:
    recording: PublishedRecordingRef
    manifest: RecordingManifest
    algorithm_ref: ArtifactRef
    config_ref: ArtifactRef
    probe_sample_count: int


@dataclass(frozen=True)
class SubmittedStarlinkSuiteAnalysisV0_2:
    job_id: JobId
    payload: JobPayload
    request: StarlinkDetectorSuiteRequestV0_2


class StarlinkSuiteAnalysisSubmissionServiceV0_2:
    def __init__(self, jobs: StarlinkSuiteJobEnqueuer) -> None:
        self._jobs = jobs

    def submit(
        self, submission: StarlinkSuiteAnalysisSubmissionV0_2
    ) -> SubmittedStarlinkSuiteAnalysisV0_2:
        recording = submission.recording.recording_object
        manifest = submission.manifest
        if manifest.recording_id != recording.recording_id:
            raise ValueError("suite manifest and published recording differ")
        rates = {segment.actual_sample_rate_hz for segment in manifest.segments}
        if len(rates) != 1:
            raise ValueError("suite recording mixes sample rates")
        rate = next(iter(rates))
        clipped = rate < 1_875_000.0
        selections: list[StarlinkSuiteStreamSelectionV0_2] = []
        if not clipped:
            for segment in manifest.segments:
                tags = dict(segment.requested.tags)
                if tags.get("scan_schema") != "org.leo-flow.starlink-edge-scan/v1":
                    raise ValueError("recording is not an exact Starlink edge scan")
                if tags.get("pilot_band_fits") is not True:
                    raise ValueError("full-band recording contains a clipped segment")
                if segment.sample_count < submission.probe_sample_count:
                    raise ValueError("recording segment is shorter than suite probe")
                edge = StarlinkEdge(tags["edge"])
                templates = qin_edge_pilot_template_pair_v0_1(rate, edge)
                for receiver in segment.requested.receiver_chain_ids:
                    selections.append(
                        StarlinkSuiteStreamSelectionV0_2(
                            segment.segment_id,
                            receiver,
                            edge,
                            templates.exact_ref,
                            templates.conditioned_control_ref,
                            submission.probe_sample_count,
                        )
                    )
        selections.sort(
            key=lambda item: (str(item.segment_id), str(item.receiver_chain_id))
        )
        request = StarlinkDetectorSuiteRequestV0_2(
            SchemaRef(StarlinkDetectorSuiteRequestV0_2.SCHEMA_ID, V0_2),
            recording.recording_id,
            recording,
            submission.algorithm_ref,
            submission.config_ref,
            tuple(selections),
            SchemaRef(StarlinkDetectorSuiteRecordingBundleV0_2.SCHEMA_ID, V0_2),
            "clipped-pilot-band" if clipped else None,
        )
        payload = starlink_suite_analysis_payload(request)
        digest = Digest.sha256(
            canonical_json_bytes(
                {
                    "schema": payload.schema,
                    "value": thaw_value(payload.value),
                }
            )
        )
        job_id = JobId(f"job_{digest.value}")
        self._jobs.enqueue(job_id, JobType.STARLINK_SUITE_ANALYSIS, payload)
        return SubmittedStarlinkSuiteAnalysisV0_2(job_id, payload, request)
