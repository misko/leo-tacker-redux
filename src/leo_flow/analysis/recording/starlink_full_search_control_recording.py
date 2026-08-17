"""Bounded recording bridge for additive symmetric rolled-template controls."""

from __future__ import annotations

from leo_flow.contracts.core import SchemaRef, canonical_digest
from leo_flow.contracts.starlink_full_search_control import (
    V0_1,
    StarlinkFullSearchControlRecordingBundleV0_1,
    StarlinkFullSearchControlRecordingState,
)
from leo_flow.contracts.starlink_suite_pipeline import StarlinkDetectorSuiteRequestV0_2
from leo_flow.storage.ports import RecordingView

from .quality import decode_ci16
from .starlink_detector_suite import StarlinkDetectorSuiteV0_2
from .starlink_templates import qin_edge_pilot_template_pair_v0_1


class ExactStarlinkFullSearchControlRecordingAnalyzerV0_1:
    """Compute the symmetric control over the exact v0.2 request membership."""

    def __init__(self, suite: StarlinkDetectorSuiteV0_2) -> None:
        self._suite = suite

    def analyze_full_search_controls(
        self,
        recording: RecordingView,
        request: StarlinkDetectorSuiteRequestV0_2,
    ) -> StarlinkFullSearchControlRecordingBundleV0_1:
        if recording.manifest.recording_id != request.recording_id:
            raise ValueError("recording and control request identities differ")
        recording_digest = request.recording_object_ref.identity_digest()
        request_digest = canonical_digest(request)
        if request.ineligible_reason is not None:
            token = canonical_digest(
                {
                    "request_digest": str(request_digest),
                    "state": StarlinkFullSearchControlRecordingState.NOT_EVALUATED.value,
                }
            ).value
            return StarlinkFullSearchControlRecordingBundleV0_1(
                SchemaRef(StarlinkFullSearchControlRecordingBundleV0_1.SCHEMA_ID, V0_1),
                f"slsctrlrec_{token[:32]}",
                request.recording_id,
                recording_digest,
                request_digest,
                StarlinkFullSearchControlRecordingState.NOT_EVALUATED,
                (),
                (request.ineligible_reason,),
            )

        segments = {
            segment.segment_id: segment for segment in recording.manifest.segments
        }
        suites = []
        for selection in request.stream_selections:
            try:
                segment = segments[selection.segment_id]
                receiver_index = segment.requested.receiver_chain_ids.index(
                    selection.receiver_chain_id
                )
            except (KeyError, ValueError) as error:
                raise ValueError("selected control stream is unavailable") from error
            if selection.probe_sample_count > segment.sample_count:
                raise ValueError("full-search control probe exceeds selected segment")
            templates = qin_edge_pilot_template_pair_v0_1(
                segment.actual_sample_rate_hz, selection.edge
            )
            if (
                templates.exact_ref != selection.exact_template_ref
                or templates.conditioned_control_ref
                != selection.conditioned_control_template_ref
            ):
                raise ValueError("control request does not pin the exact Qin templates")
            raw = recording.read_iq_bytes(
                selection.segment_id, 0, selection.probe_sample_count
            )
            values, count = decode_ci16(raw, len(segment.requested.receiver_chain_ids))
            if count != selection.probe_sample_count:
                raise ValueError("full-search control reader returned another interval")
            stride = len(segment.requested.receiver_chain_ids) * 2
            offset = receiver_index * 2
            samples = tuple(
                complex(values[position], values[position + 1])
                for position in range(offset, count * stride, stride)
            )
            suites.append(
                self._suite.analyze_full_search_control(
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
                "request_digest": str(request_digest),
                "suite_digests": tuple(str(item.digest) for item in suites),
            }
        ).value
        return StarlinkFullSearchControlRecordingBundleV0_1(
            SchemaRef(StarlinkFullSearchControlRecordingBundleV0_1.SCHEMA_ID, V0_1),
            f"slsctrlrec_{token[:32]}",
            request.recording_id,
            recording_digest,
            request_digest,
            StarlinkFullSearchControlRecordingState.CANDIDATES,
            tuple(suites),
            (
                "surrogate-control-only",
                "not-an-empirical-null-distribution",
                "no-calibrated-detection-verdict",
            ),
        )
