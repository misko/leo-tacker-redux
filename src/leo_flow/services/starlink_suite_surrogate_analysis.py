"""Compose paired-surrogate controls inside the existing suite job boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from leo_flow.analysis.recording.quality import decode_ci16
from leo_flow.contracts.core import ArtifactRef, SchemaRef, canonical_digest
from leo_flow.contracts.starlink_detector_suite import (
    StarlinkDetectorSuiteBundleV0_2,
)
from leo_flow.contracts.starlink_pilot_constellation import (
    StarlinkPilotConstellationEvidenceV0_1,
)
from leo_flow.contracts.starlink_pilot_constellation_pipeline import (
    StarlinkPilotConstellationRecordingBundleV0_1,
    StarlinkPilotConstellationRequestV0_1,
)
from leo_flow.contracts.starlink_suite_pipeline import (
    StarlinkDetectorSuiteRecordingAnalyzerV0_2,
    StarlinkDetectorSuiteRecordingBundleV0_2,
    StarlinkDetectorSuiteRequestV0_2,
    StarlinkSuiteRecordingState,
)
from leo_flow.jobs.contracts import JobLease, JobType
from leo_flow.storage.ports import RecordingObjectReader, RecordingView

from .starlink_suite_analysis import (
    PreparedStarlinkSuiteAnalysisV0_2,
    StarlinkSuiteAnalysisJobError,
    decode_starlink_suite_analysis_payload,
)
from .starlink_surrogate_null_analysis import (
    PreparedStarlinkSurrogateNullAnalysisV0_1,
    StarlinkSurrogateNullAnalysisPreparerV0_1,
)


@dataclass(frozen=True)
class PreparedCombinedStarlinkSuiteAnalysisV0_2(PreparedStarlinkSuiteAnalysisV0_2):
    surrogate_null: PreparedStarlinkSurrogateNullAnalysisV0_1
    pilot_constellation: PreparedStarlinkPilotConstellationAnalysisV0_1 | None


@dataclass(frozen=True)
class PreparedStarlinkPilotConstellationAnalysisV0_1:
    request: StarlinkPilotConstellationRequestV0_1
    bundle: StarlinkPilotConstellationRecordingBundleV0_1


class StarlinkPilotConstellationStreamAnalyzerV0_1(Protocol):
    def analyze(
        self,
        samples: tuple[complex, ...],
        suite: StarlinkDetectorSuiteBundleV0_2,
    ) -> StarlinkPilotConstellationEvidenceV0_1: ...


class CombinedStarlinkSuiteAnalysisJobPreparerV0_2:
    """Run the suite and its exact-profile controls from one recording view."""

    def __init__(
        self,
        reader: RecordingObjectReader,
        suite_analyzer: StarlinkDetectorSuiteRecordingAnalyzerV0_2,
        surrogate_preparers: tuple[
            tuple[ArtifactRef, StarlinkSurrogateNullAnalysisPreparerV0_1], ...
        ],
        constellation_analyzer: StarlinkPilotConstellationStreamAnalyzerV0_1,
    ) -> None:
        if not surrogate_preparers:
            raise ValueError("combined suite analysis requires surrogate profiles")
        refs = tuple(ref for ref, _preparer in surrogate_preparers)
        if len(refs) != len(set(refs)):
            raise ValueError("surrogate profile config refs must be unique")
        self._reader = reader
        self._suite_analyzer = suite_analyzer
        self._surrogate_preparers = surrogate_preparers
        self._constellation_analyzer = constellation_analyzer

    def prepare(self, lease: JobLease) -> PreparedCombinedStarlinkSuiteAnalysisV0_2:
        if lease.job_type is not JobType.STARLINK_SUITE_ANALYSIS:
            raise StarlinkSuiteAnalysisJobError(
                "worker accepts detector-suite jobs only"
            )
        request = decode_starlink_suite_analysis_payload(lease.payload)
        matches = tuple(
            preparer
            for config_ref, preparer in self._surrogate_preparers
            if config_ref == request.config_ref
        )
        if len(matches) != 1:
            raise StarlinkSuiteAnalysisJobError(
                "detector-suite request has no exact surrogate profile"
            )
        with self._reader.open(request.recording_object_ref) as recording:
            bundle = self._suite_analyzer.analyze_starlink_suite(recording, request)
            surrogate = matches[0].prepare_from_open_recording(
                recording, request, bundle
            )
            constellation = _prepare_constellation(
                recording, request, bundle, self._constellation_analyzer
            )
        return PreparedCombinedStarlinkSuiteAnalysisV0_2(
            request, bundle, surrogate, constellation
        )


def _prepare_constellation(
    recording: RecordingView,
    source_request: StarlinkDetectorSuiteRequestV0_2,
    source_bundle: StarlinkDetectorSuiteRecordingBundleV0_2,
    analyzer: StarlinkPilotConstellationStreamAnalyzerV0_1,
) -> PreparedStarlinkPilotConstellationAnalysisV0_1 | None:
    if source_bundle.state is StarlinkSuiteRecordingState.NOT_EVALUATED:
        if (
            source_bundle.suites
            or source_request.ineligible_reason != "clipped-pilot-band"
        ):
            raise ValueError("ineligible suite is not valid constellation input")
        return None
    source_ref = ArtifactRef(
        source_bundle.analysis_id, source_bundle.digest, source_bundle.schema
    )
    source_request_digest = canonical_digest(source_request)
    keys = tuple(
        (suite.segment_id, suite.receiver_chain_id, suite.edge)
        for suite in source_bundle.suites
    )
    request = StarlinkPilotConstellationRequestV0_1(
        SchemaRef(StarlinkPilotConstellationRequestV0_1.SCHEMA_ID),
        source_request.recording_id,
        source_request.recording_object_ref,
        source_ref,
        source_request_digest,
        keys,
        SchemaRef(StarlinkPilotConstellationRecordingBundleV0_1.SCHEMA_ID),
    )
    segments = {segment.segment_id: segment for segment in recording.manifest.segments}
    evidence: list[StarlinkPilotConstellationEvidenceV0_1] = []
    for suite in source_bundle.suites:
        try:
            segment = segments[suite.segment_id]
            receiver_index = segment.requested.receiver_chain_ids.index(
                suite.receiver_chain_id
            )
        except (KeyError, ValueError) as error:
            raise ValueError("constellation suite stream is unavailable") from error
        raw = recording.read_iq_bytes(suite.segment_id, 0, suite.probe_sample_count)
        values, count = decode_ci16(raw, len(segment.requested.receiver_chain_ids))
        if count != suite.probe_sample_count:
            raise ValueError("constellation reader returned another interval")
        stride = len(segment.requested.receiver_chain_ids) * 2
        offset = receiver_index * 2
        samples = tuple(
            complex(values[position], values[position + 1])
            for position in range(offset, count * stride, stride)
        )
        evidence.append(analyzer.analyze(samples, suite))
    evidence.sort(
        key=lambda item: (
            str(item.segment_id),
            str(item.receiver_chain_id),
            item.edge.value,
        )
    )
    if (
        tuple((item.segment_id, item.receiver_chain_id, item.edge) for item in evidence)
        != request.stream_keys
    ):
        raise ValueError("constellation evidence membership differs from suite")
    token = canonical_digest(
        {
            "request": request,
            "evidence_digests": tuple(str(item.digest) for item in evidence),
        }
    ).value
    bundle = StarlinkPilotConstellationRecordingBundleV0_1(
        request.requested_output_schema,
        f"slqamrec_{token[:32]}",
        request.recording_id,
        request.recording_object_ref.identity_digest(),
        source_ref,
        source_request_digest,
        request.digest,
        tuple(evidence),
        (
            "candidate-evidence-not-calibrated-detection",
            "published-edge-pilot-not-user-payload",
        ),
        None,
    )
    return PreparedStarlinkPilotConstellationAnalysisV0_1(request, bundle)
