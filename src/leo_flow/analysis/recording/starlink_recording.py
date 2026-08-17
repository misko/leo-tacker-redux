"""Exact bounded bridge from a recording view to known-code stream searches."""

from __future__ import annotations

from leo_flow.contracts.core import ArtifactRef, SchemaRef, canonical_digest
from leo_flow.contracts.starlink import (
    StarlinkPilotAnalysisBundleV0_1,
    StarlinkPilotSearchCandidateV0_1,
)
from leo_flow.contracts.starlink_pipeline import StarlinkPilotAnalysisRequestV0_1
from leo_flow.storage.ports import RecordingView

from .quality import decode_ci16
from .starlink import KnownCodePilotSearchV0_1
from .starlink_templates import qin_edge_pilot_template_pair_v0_1


class ExactKnownCodeRecordingAnalyzerV0_1:
    """Analyze only request-selected prefix probes with pre-approved templates."""

    def __init__(
        self,
        search: KnownCodePilotSearchV0_1,
        *,
        algorithm_ref: ArtifactRef,
        config_ref: ArtifactRef,
    ) -> None:
        self._search = search
        self._algorithm_ref = algorithm_ref
        self._config_ref = config_ref

    def analyze_starlink(
        self, recording: RecordingView, request: StarlinkPilotAnalysisRequestV0_1
    ) -> StarlinkPilotAnalysisBundleV0_1:
        if recording.manifest.recording_id != request.recording_id:
            raise ValueError("recording and Starlink request identities differ")
        if (
            request.algorithm_ref != self._algorithm_ref
            or request.config_ref != self._config_ref
        ):
            raise ValueError("exact Starlink analyzer is not registered")
        segments = {item.segment_id: item for item in recording.manifest.segments}
        candidates: list[StarlinkPilotSearchCandidateV0_1] = []
        warnings: set[str] = set()
        for selection in request.stream_selections:
            try:
                segment = segments[selection.segment_id]
                receiver_index = segment.requested.receiver_chain_ids.index(
                    selection.receiver_chain_id
                )
            except (KeyError, ValueError) as error:
                raise ValueError("selected Starlink stream is unavailable") from error
            if selection.probe_sample_count > segment.sample_count:
                raise ValueError("Starlink probe exceeds selected segment")
            templates = qin_edge_pilot_template_pair_v0_1(
                segment.actual_sample_rate_hz, selection.edge
            )
            if (
                templates.exact_ref != selection.exact_template_ref
                or templates.conditioned_control_ref
                != selection.conditioned_control_template_ref
            ):
                raise ValueError("request does not pin the exact Qin templates")
            raw = recording.read_iq_bytes(
                selection.segment_id, 0, selection.probe_sample_count
            )
            values, count = decode_ci16(raw, len(segment.requested.receiver_chain_ids))
            if count != selection.probe_sample_count:
                raise ValueError("Starlink probe reader returned another interval")
            stride = len(segment.requested.receiver_chain_ids) * 2
            offset = receiver_index * 2
            samples = tuple(
                complex(values[position], values[position + 1])
                for position in range(offset, count * stride, stride)
            )
            bundle = self._search.analyze_receiver(
                samples,
                recording_id=request.recording_id,
                recording_identity_digest=request.recording_object_ref.identity_digest(),
                segment_id=selection.segment_id,
                receiver_chain_id=selection.receiver_chain_id,
                templates=templates,
            )
            if len(bundle.candidates) != 1:
                raise ValueError("one selected stream must yield one search maximum")
            candidates.extend(bundle.candidates)
            warnings.update(bundle.warnings)
        candidates.sort(
            key=lambda item: (str(item.segment_id), str(item.receiver_chain_id))
        )
        token = canonical_digest(
            {
                "recording_identity_digest": str(
                    request.recording_object_ref.identity_digest()
                ),
                "candidate_ids": tuple(item.candidate_id for item in candidates),
            }
        ).value
        return StarlinkPilotAnalysisBundleV0_1(
            SchemaRef(StarlinkPilotAnalysisBundleV0_1.SCHEMA_ID),
            f"slanalysis_{token[:32]}",
            request.recording_id,
            request.recording_object_ref.identity_digest(),
            tuple(candidates),
            tuple(sorted(warnings)),
        )
