"""Source-closed production of a prompt timeline from an independent lease."""

from __future__ import annotations

from leo_flow.analysis.recording.api import AnalysisExecutionContext
from leo_flow.analysis.recording.starlink_full_dwell_timeline_persistence import (
    DurableFullDwellTimelineStoreV0_1,
)
from leo_flow.analysis.recording.starlink_full_dwell_timeline_product import (
    CompleteIqTimelineAnalyzerV0_1,
    refinement_request_v0_1,
)
from leo_flow.contracts.core import ArtifactRef, SchemaRef
from leo_flow.contracts.starlink_full_dwell_timeline_product import (
    V0_1,
    FullDwellTimelineBundleV0_1,
    FullDwellTimelineRequestV0_1,
)
from leo_flow.storage.ports import RecordingObjectReader

from .full_dwell_timeline import (
    FullDwellTimelineLeaseV0_1,
    ProducedFullDwellTimelineV0_1,
)


class DurableFullDwellTimelineLeaseProducerV0_1:
    """Analyze and publish only the cheap product; never run exact detection."""

    def __init__(
        self,
        reader: RecordingObjectReader,
        products: DurableFullDwellTimelineStoreV0_1,
        execution: AnalysisExecutionContext,
    ) -> None:
        self._reader, self._products = reader, products
        self._analyzer = CompleteIqTimelineAnalyzerV0_1(execution)

    def produce(
        self, lease: FullDwellTimelineLeaseV0_1
    ) -> ProducedFullDwellTimelineV0_1:
        request = FullDwellTimelineRequestV0_1(
            SchemaRef(FullDwellTimelineRequestV0_1.SCHEMA_ID, V0_1),
            lease.recording_ref.recording_id,
            lease.recording_ref,
            lease.plan,
            lease.stream_selections,
            SchemaRef(FullDwellTimelineBundleV0_1.SCHEMA_ID, V0_1),
        )
        with self._reader.open(lease.recording_ref) as recording:
            bundle = self._analyzer.analyze(recording, request)
        ref = self._products.publish(
            request,
            bundle,
            idempotency_key=f"timeline:{request.recording_id}:{request.digest.value}",
        )
        timeline_ref = ArtifactRef(
            ref.analysis_id, ref.bundle_ref.digest, bundle.schema
        )
        return ProducedFullDwellTimelineV0_1(
            timeline_ref,
            refinement_request_v0_1(request, bundle, ref.bundle_ref.digest),
        )
