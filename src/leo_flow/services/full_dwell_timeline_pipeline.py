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
from leo_flow.analysis.recording.starlink_pilot_prescreen import (
    CompleteIqPilotPrescreenAnalyzerV0_1,
)
from leo_flow.analysis.recording.starlink_pilot_prescreen_persistence import (
    DurableStarlinkPilotPrescreenStoreV0_1,
)
from leo_flow.contracts.core import ArtifactRef, SchemaRef
from leo_flow.contracts.starlink_full_dwell_timeline_product import (
    V0_1,
    FullDwellTimelineBundleV0_1,
    FullDwellTimelineRequestV0_1,
)
from leo_flow.contracts.starlink_pilot_prescreen import (
    StarlinkPilotPrescreenPlanV0_1,
    StarlinkPilotPrescreenRequestV0_1,
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
        pilot_prescreens: DurableStarlinkPilotPrescreenStoreV0_1 | None = None,
    ) -> None:
        self._reader, self._products = reader, products
        self._analyzer = CompleteIqTimelineAnalyzerV0_1(execution)
        self._pilot_prescreens = pilot_prescreens
        self._pilot_analyzer = CompleteIqPilotPrescreenAnalyzerV0_1(execution)

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
        pilot_request = None
        if self._pilot_prescreens is not None:
            if lease.plan.maximum_window_count_per_stream < 2:
                raise ValueError("pilot prescreen requires at least two windows")
            periodicity_seeds = min(32, lease.plan.maximum_window_count_per_stream - 1)
            pilot_request = StarlinkPilotPrescreenRequestV0_1(
                SchemaRef(StarlinkPilotPrescreenRequestV0_1.SCHEMA_ID, V0_1),
                lease.recording_ref.recording_id,
                lease.recording_ref,
                StarlinkPilotPrescreenPlanV0_1(
                    tile_sample_count=lease.plan.tile_sample_count,
                    maximum_window_count_per_stream=(
                        lease.plan.maximum_window_count_per_stream
                    ),
                    maximum_periodicity_seeds_per_stream=periodicity_seeds,
                    maximum_power_seeds_per_stream=min(
                        8,
                        lease.plan.maximum_window_count_per_stream - periodicity_seeds,
                    ),
                ),
                lease.stream_selections,
            )
        existing = self._products.latest(request.recording_id)
        bundle = None
        if existing is not None:
            with self._products.open(existing) as persisted:
                if (
                    persisted.recording_id != request.recording_id
                    or persisted.recording_identity_digest
                    != request.recording_object_ref.identity_digest()
                    or persisted.request_digest != request.digest
                    or persisted.plan != request.plan
                    or {
                        tuple(
                            map(
                                str,
                                (
                                    item.radio_id,
                                    item.lnb_id,
                                    item.segment_id,
                                    item.receiver_chain_id,
                                    item.channel_number,
                                    item.edge,
                                ),
                            )
                        )
                        for item in persisted.streams
                    }
                    != {item.identity for item in request.stream_selections}
                ):
                    raise ValueError("existing timeline differs from requested replay")
                bundle = persisted
        with self._reader.open(lease.recording_ref) as recording:
            if bundle is None:
                bundle = self._analyzer.analyze(recording, request)
            pilot_bundle = (
                self._pilot_analyzer.analyze(recording, pilot_request)
                if pilot_request is not None
                else None
            )
        ref = existing
        if ref is None:
            ref = self._products.publish(
                request,
                bundle,
                idempotency_key=f"timeline:{request.recording_id}:{request.digest.value}",
            )
        timeline_ref = ArtifactRef(
            ref.analysis_id, ref.bundle_ref.digest, bundle.schema
        )
        if (
            self._pilot_prescreens is not None
            and pilot_request is not None
            and pilot_bundle is not None
        ):
            self._pilot_prescreens.publish(
                pilot_request,
                pilot_bundle,
                idempotency_key=(
                    f"pilot-prescreen:{pilot_request.recording_id}:"
                    f"{pilot_request.digest.value}"
                ),
            )
        return ProducedFullDwellTimelineV0_1(
            timeline_ref,
            refinement_request_v0_1(request, bundle, ref.bundle_ref.digest),
        )
