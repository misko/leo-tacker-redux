"""Bounded bridge from an immutable recording to every report detector method."""

from __future__ import annotations

from array import array

from leo_flow.contracts.core import SchemaRef, SegmentId, canonical_digest
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_detector_suite import V0_2
from leo_flow.contracts.starlink_suite_pipeline import (
    StarlinkDetectorSuiteRecordingBundleV0_2,
    StarlinkDetectorSuiteRequestV0_2,
    StarlinkSuiteRecordingState,
)
from leo_flow.storage.ports import RecordingView

from .quality import decode_ci16
from .starlink import KnownCodePilotTemplatePairV0_1
from .starlink_detector_suite import StarlinkDetectorSuiteV0_2
from .starlink_templates import qin_edge_pilot_template_pair_v0_1


class ExactStarlinkDetectorSuiteRecordingAnalyzerV0_2:
    """Execute one exact suite per selected segment/receiver prefix."""

    def __init__(self, suite: StarlinkDetectorSuiteV0_2) -> None:
        self._suite = suite

    def analyze_starlink_suite(
        self,
        recording: RecordingView,
        request: StarlinkDetectorSuiteRequestV0_2,
    ) -> StarlinkDetectorSuiteRecordingBundleV0_2:
        if recording.manifest.recording_id != request.recording_id:
            raise ValueError("recording and detector-suite request identities differ")
        recording_digest = request.recording_object_ref.identity_digest()
        if request.ineligible_reason is not None:
            token = canonical_digest(
                {
                    "request": request,
                    "state": StarlinkSuiteRecordingState.NOT_EVALUATED.value,
                }
            ).value
            return StarlinkDetectorSuiteRecordingBundleV0_2(
                SchemaRef(StarlinkDetectorSuiteRecordingBundleV0_2.SCHEMA_ID, V0_2),
                f"slsuite_{token[:32]}",
                request.recording_id,
                recording_digest,
                StarlinkSuiteRecordingState.NOT_EVALUATED,
                (),
                (request.ineligible_reason,),
                None,
            )
        segments = {
            segment.segment_id: segment for segment in recording.manifest.segments
        }
        decoded_prefixes: dict[tuple[SegmentId, int], tuple[array[int], int]] = {}
        template_pairs: dict[
            tuple[float, StarlinkEdge], KnownCodePilotTemplatePairV0_1
        ] = {}
        suites = []
        for selection in request.stream_selections:
            try:
                segment = segments[selection.segment_id]
                receiver_index = segment.requested.receiver_chain_ids.index(
                    selection.receiver_chain_id
                )
            except (KeyError, ValueError) as error:
                raise ValueError(
                    "selected detector-suite stream is unavailable"
                ) from error
            if selection.probe_sample_count > segment.sample_count:
                raise ValueError("detector-suite probe exceeds selected segment")
            template_key = (segment.actual_sample_rate_hz, selection.edge)
            templates = template_pairs.get(template_key)
            if templates is None:
                templates = qin_edge_pilot_template_pair_v0_1(*template_key)
                template_pairs[template_key] = templates
            if (
                templates.exact_ref != selection.exact_template_ref
                or templates.conditioned_control_ref
                != selection.conditioned_control_template_ref
            ):
                raise ValueError("request does not pin the exact Qin templates")
            prefix_key = (selection.segment_id, selection.probe_sample_count)
            decoded = decoded_prefixes.get(prefix_key)
            if decoded is None:
                raw = recording.read_iq_bytes(
                    selection.segment_id, 0, selection.probe_sample_count
                )
                decoded = decode_ci16(raw, len(segment.requested.receiver_chain_ids))
                decoded_prefixes[prefix_key] = decoded
            values, count = decoded
            if count != selection.probe_sample_count:
                raise ValueError("detector-suite reader returned another interval")
            stride = len(segment.requested.receiver_chain_ids) * 2
            offset = receiver_index * 2
            samples = tuple(
                complex(values[position], values[position + 1])
                for position in range(offset, count * stride, stride)
            )
            suites.append(
                self._suite.analyze_receiver(
                    samples,
                    recording_id=request.recording_id,
                    recording_identity_digest=recording_digest,
                    segment_id=selection.segment_id,
                    receiver_chain_id=selection.receiver_chain_id,
                    templates=templates,
                )
            )
        suites.sort(
            key=lambda item: (str(item.segment_id), str(item.receiver_chain_id))
        )
        token = canonical_digest(
            {
                "request": request,
                "suite_digests": tuple(str(item.digest) for item in suites),
            }
        ).value
        return StarlinkDetectorSuiteRecordingBundleV0_2(
            SchemaRef(StarlinkDetectorSuiteRecordingBundleV0_2.SCHEMA_ID, V0_2),
            f"slsuite_{token[:32]}",
            request.recording_id,
            recording_digest,
            StarlinkSuiteRecordingState.CANDIDATES,
            tuple(suites),
            ("whole-search-calibration-required", "candidate-evidence-only"),
            None,
        )
