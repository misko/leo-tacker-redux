"""Exact idempotent submission of one recording for Starlink candidate search."""

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
from leo_flow.contracts.starlink import StarlinkEdge, StarlinkPilotAnalysisBundleV0_1
from leo_flow.contracts.starlink_pipeline import (
    StarlinkPilotAnalysisRequestV0_1,
    StarlinkStreamSelectionV0_1,
)
from leo_flow.contracts.storage import PublishedRecordingRef
from leo_flow.jobs.contracts import JobPayload, JobType

from .starlink_analysis import starlink_analysis_payload


class StarlinkJobEnqueuer(Protocol):
    def enqueue(
        self,
        job_id: JobId,
        job_type: JobType,
        payload: JobPayload,
        *,
        available_at_utc_ns: UtcNs | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class StarlinkAnalysisSubmissionV0_1:
    recording: PublishedRecordingRef
    algorithm_ref: ArtifactRef
    config_ref: ArtifactRef
    stream_selections: tuple[StarlinkStreamSelectionV0_1, ...]


@dataclass(frozen=True)
class SubmittedStarlinkAnalysisV0_1:
    job_id: JobId
    payload: JobPayload
    request: StarlinkPilotAnalysisRequestV0_1


class StarlinkAnalysisSubmissionServiceV0_1:
    def __init__(self, jobs: StarlinkJobEnqueuer) -> None:
        self._jobs = jobs

    def submit(
        self, submission: StarlinkAnalysisSubmissionV0_1
    ) -> SubmittedStarlinkAnalysisV0_1:
        recording = submission.recording.recording_object
        selections = tuple(
            sorted(
                submission.stream_selections,
                key=lambda item: (str(item.segment_id), str(item.receiver_chain_id)),
            )
        )
        request = StarlinkPilotAnalysisRequestV0_1(
            SchemaRef(StarlinkPilotAnalysisRequestV0_1.SCHEMA_ID),
            recording.recording_id,
            recording,
            submission.algorithm_ref,
            submission.config_ref,
            selections,
            SchemaRef(StarlinkPilotAnalysisBundleV0_1.SCHEMA_ID),
        )
        payload = starlink_analysis_payload(request)
        job_id = starlink_analysis_job_id(payload)
        self._jobs.enqueue(job_id, JobType.STARLINK_ANALYSIS, payload)
        return SubmittedStarlinkAnalysisV0_1(job_id, payload, request)


def select_qin_starlink_streams_v0_1(
    manifest: RecordingManifest,
    *,
    sample_rate_hz: float,
    probe_sample_count: int,
) -> tuple[StarlinkStreamSelectionV0_1, ...]:
    """Select every receiver from one exact full-pilot edge-scan recording."""

    if sample_rate_hz <= 0 or probe_sample_count <= 0:
        raise ValueError("Starlink selection bounds must be positive")
    selections: list[StarlinkStreamSelectionV0_1] = []
    for segment in manifest.segments:
        tags = dict(segment.requested.tags)
        if tags.get("scan_schema") != "org.leo-flow.starlink-edge-scan/v1":
            raise ValueError("recording is not an exact Starlink edge scan")
        if tags.get("pilot_band_fits") is not True:
            raise ValueError("clipped-pilot segments are ineligible for Qin search")
        if segment.actual_sample_rate_hz != sample_rate_hz:
            raise ValueError("recording sample rate has no approved Qin search grid")
        if segment.sample_count < probe_sample_count:
            raise ValueError("recording segment is shorter than the approved probe")
        try:
            edge = StarlinkEdge(tags["edge"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("recording segment has no exact Starlink edge") from error
        templates = qin_edge_pilot_template_pair_v0_1(sample_rate_hz, edge)
        selections.extend(
            StarlinkStreamSelectionV0_1(
                segment.segment_id,
                receiver_chain_id,
                edge,
                templates.exact_ref,
                templates.conditioned_control_ref,
                probe_sample_count,
            )
            for receiver_chain_id in segment.requested.receiver_chain_ids
        )
    selections.sort(
        key=lambda item: (str(item.segment_id), str(item.receiver_chain_id))
    )
    if not selections:
        raise ValueError("recording contains no Starlink streams")
    if len(selections) > 64:
        raise ValueError("recording exceeds the Starlink stream bound")
    return tuple(selections)


def starlink_analysis_job_id(payload: JobPayload) -> JobId:
    digest = Digest.sha256(
        canonical_json_bytes(
            {
                "schema_id": payload.schema.schema_id,
                "schema_version": str(payload.schema.version),
                "value": thaw_value(payload.value),
            }
        )
    )
    return JobId(f"job_{digest.value}")
