"""Bounded, capture-independent production of receiver-agnostic CFO/QAM v0.6."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, cast

import numpy as np

from leo_flow.analysis.recording.api import AnalysisExecutionContext
from leo_flow.analysis.recording.starlink_receiver_agnostic_cfo import (
    RAW_IQ_SCORER_MAXIMUM_WINDOW_SAMPLES,
    ReceiverAgnosticCfoQamAnalyzerV0_6,
)
from leo_flow.analysis.recording.starlink_receiver_agnostic_cfo_product_persistence import (
    DurableReceiverAgnosticCfoQamStoreV0_6,
    ReceiverAgnosticCfoQamCatalogV0_6,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    Provenance,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    canonical_digest,
)
from leo_flow.contracts.optional_heavy_work_admission import (
    OptionalHeavyWorkAdmissionPortV0_1,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_receiver_agnostic_cfo import (
    V0_6,
    ReceiverAgnosticCfoQamWindowBundleV0_6,
    ReceiverAgnosticCfoSearchPlanV0_6,
    ReceiverAgnosticCfoWindowV0_6,
)
from leo_flow.contracts.starlink_receiver_agnostic_cfo_product import (
    MAXIMUM_CFO_QAM_RECORDING_STREAMS,
    MAXIMUM_CFO_QAM_RECORDING_WINDOWS,
    MAXIMUM_CFO_QAM_WINDOWS_PER_STREAM,
    ReceiverAgnosticCfoQamRecordingBundleV0_6,
    ReceiverAgnosticCfoQamRecordingPlanV0_6,
    ReceiverAgnosticCfoQamRecordingProductRefV0_6,
    ReceiverAgnosticCfoQamRecordingRequestV0_6,
)
from leo_flow.contracts.storage import RecordingObjectRef
from leo_flow.services.capture_batch_analysis import PublishedRecordingCatalog
from leo_flow.storage.ports import RecordingObjectReader, RecordingView


@dataclass(frozen=True, slots=True)
class ReceiverAgnosticCfoQamWindowSelectionV0_6:
    """Operator-owned exact interval; no receiver-specific frequency correction."""

    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    edge: StarlinkEdge
    start_sample: int
    sample_count: int = 20_000

    def __post_init__(self) -> None:
        if (
            self.start_sample < 0
            or not 1 <= self.sample_count <= RAW_IQ_SCORER_MAXIMUM_WINDOW_SAMPLES
        ):
            raise ValueError("receiver-agnostic CFO/QAM selection is invalid")

    @property
    def stop_sample(self) -> int:
        return self.start_sample + self.sample_count


class ReceiverAgnosticCfoQamWindowAnalyzerV0_6(Protocol):
    def analyze(
        self,
        samples: Sequence[complex],
        window: ReceiverAgnosticCfoWindowV0_6,
        *,
        pattern_count: int,
        execution: AnalysisExecutionContext,
    ) -> ReceiverAgnosticCfoQamWindowBundleV0_6: ...


class ReceiverAgnosticCfoQamAnalyzerFactoryV0_6(Protocol):
    def __call__(
        self, plan: ReceiverAgnosticCfoSearchPlanV0_6
    ) -> ReceiverAgnosticCfoQamWindowAnalyzerV0_6: ...


@dataclass(frozen=True, slots=True)
class ReceiverAgnosticCfoQamProductionResultV0_6:
    ref: ReceiverAgnosticCfoQamRecordingProductRefV0_6
    reused: bool


class DurableReceiverAgnosticCfoQamProducerV0_6:
    """Analyze at most six explicit small windows from one published recording."""

    def __init__(
        self,
        recordings: PublishedRecordingCatalog,
        reader: RecordingObjectReader,
        products: DurableReceiverAgnosticCfoQamStoreV0_6,
        catalog: ReceiverAgnosticCfoQamCatalogV0_6,
        execution: AnalysisExecutionContext,
        *,
        search_plan: ReceiverAgnosticCfoSearchPlanV0_6 | None = None,
        pattern_count: int = 9,
        analyzer_factory: ReceiverAgnosticCfoQamAnalyzerFactoryV0_6 = (
            ReceiverAgnosticCfoQamAnalyzerV0_6
        ),
    ) -> None:
        if not 1 <= pattern_count <= 9:
            raise ValueError("receiver-agnostic CFO/QAM pattern count is invalid")
        self._recordings = recordings
        self._reader = reader
        self._products = products
        self._catalog = catalog
        self._execution = execution
        self._search_plan = search_plan or ReceiverAgnosticCfoSearchPlanV0_6()
        self._pattern_count = pattern_count
        self._analyzer_factory = analyzer_factory

    def produce(
        self,
        recording_id: RecordingId,
        selections: tuple[ReceiverAgnosticCfoQamWindowSelectionV0_6, ...],
    ) -> ReceiverAgnosticCfoQamProductionResultV0_6:
        published = self._recordings.get(recording_id)
        if published is None:
            raise ValueError("receiver-agnostic CFO/QAM recording is not published")
        plan = self._recording_plan(selections)
        with self._reader.open(published.recording_object) as recording:
            windows = self._windows(recording, published.recording_object, selections)
            request = ReceiverAgnosticCfoQamRecordingRequestV0_6(
                SchemaRef(ReceiverAgnosticCfoQamRecordingRequestV0_6.SCHEMA_ID, V0_6),
                recording_id,
                published.recording_object,
                plan,
                windows,
                SchemaRef(ReceiverAgnosticCfoQamRecordingBundleV0_6.SCHEMA_ID, V0_6),
            )
            existing = self._matching_latest(request)
            if existing is not None:
                return ReceiverAgnosticCfoQamProductionResultV0_6(existing, True)
            analyzer = self._analyzer_factory(self._search_plan)
            products = tuple(
                analyzer.analyze(
                    self._read_receiver_window(recording, window),
                    window,
                    pattern_count=self._pattern_count,
                    execution=self._execution,
                )
                for window in windows
            )
        bundle = recording_receiver_agnostic_cfo_qam_bundle_v0_6(
            request, products, self._execution
        )
        ref = self._products.publish(
            request,
            bundle,
            idempotency_key=(
                f"receiver-agnostic-cfo-qam-v0.6:{recording_id}:{request.digest.value}"
            ),
        )
        return ReceiverAgnosticCfoQamProductionResultV0_6(ref, False)

    def _recording_plan(
        self, selections: tuple[ReceiverAgnosticCfoQamWindowSelectionV0_6, ...]
    ) -> ReceiverAgnosticCfoQamRecordingPlanV0_6:
        if not selections or len(selections) > MAXIMUM_CFO_QAM_RECORDING_WINDOWS:
            raise ValueError("receiver-agnostic CFO/QAM selection count is invalid")
        identities = tuple(
            (item.segment_id, item.receiver_chain_id, item.edge) for item in selections
        )
        counts = Counter(identities)
        if (
            len(counts) > MAXIMUM_CFO_QAM_RECORDING_STREAMS
            or any(
                value > MAXIMUM_CFO_QAM_WINDOWS_PER_STREAM for value in counts.values()
            )
            or len(
                {
                    (item.segment_id, item.receiver_chain_id, item.start_sample)
                    for item in selections
                }
            )
            != len(selections)
        ):
            raise ValueError("receiver-agnostic CFO/QAM stream bounds are exceeded")
        return ReceiverAgnosticCfoQamRecordingPlanV0_6(
            self._search_plan,
            maximum_streams=len(counts),
            maximum_windows_per_stream=max(counts.values()),
            maximum_patterns=self._pattern_count,
            maximum_pattern_evidence=(
                len(counts) * max(counts.values()) * self._pattern_count
            ),
        )

    def _windows(
        self,
        recording: RecordingView,
        recording_ref: RecordingObjectRef,
        selections: tuple[ReceiverAgnosticCfoQamWindowSelectionV0_6, ...],
    ) -> tuple[ReceiverAgnosticCfoWindowV0_6, ...]:
        manifest = recording.manifest
        if manifest.recording_id != recording_ref.recording_id:
            raise ValueError("recording manifest identity differs from publication")
        segments = {item.segment_id: item for item in manifest.segments}
        identity = recording_ref.identity_digest()
        source = ArtifactRef("published-recording", identity)
        windows = []
        for selection in selections:
            segment = segments.get(selection.segment_id)
            if (
                segment is None
                or selection.receiver_chain_id
                not in segment.requested.receiver_chain_ids
                or selection.stop_sample > segment.sample_count
            ):
                raise ValueError("receiver-agnostic CFO/QAM selection is unavailable")
            if not any(
                span.start_sample <= selection.start_sample
                and selection.stop_sample <= span.stop_sample
                for span in recording.contiguous_rf_spans(selection.segment_id)
            ):
                raise ValueError("receiver-agnostic CFO/QAM selection is discontinuous")
            window_digest = canonical_digest(
                {
                    "recording_identity": identity,
                    "segment_id": selection.segment_id,
                    "receiver_chain_id": selection.receiver_chain_id,
                    "edge": selection.edge,
                    "start_sample": selection.start_sample,
                    "stop_sample": selection.stop_sample,
                }
            )
            windows.append(
                ReceiverAgnosticCfoWindowV0_6(
                    manifest.recording_id,
                    identity,
                    manifest.radio_id,
                    selection.segment_id,
                    selection.receiver_chain_id,
                    selection.edge,
                    segment.actual_sample_rate_hz,
                    selection.start_sample,
                    selection.stop_sample,
                    source,
                    ArtifactRef("exact-ci16-receiver-window", window_digest),
                )
            )
        return tuple(sorted(windows, key=lambda item: item.identity))

    def _matching_latest(
        self, request: ReceiverAgnosticCfoQamRecordingRequestV0_6
    ) -> ReceiverAgnosticCfoQamRecordingProductRefV0_6 | None:
        ref = self._catalog.latest_receiver_agnostic_cfo_qam(request.recording_id)
        if ref is None:
            return None
        with self._products.open(ref) as existing:
            return ref if existing.request_digest == request.digest else None

    @staticmethod
    def _read_receiver_window(
        recording: RecordingView, window: ReceiverAgnosticCfoWindowV0_6
    ) -> Sequence[complex]:
        segment = next(
            item
            for item in recording.manifest.segments
            if item.segment_id == window.segment_id
        )
        receiver_count = len(segment.requested.receiver_chain_ids)
        receiver_index = segment.requested.receiver_chain_ids.index(
            window.receiver_chain_id
        )
        payload = recording.read_iq_bytes(
            window.segment_id, window.start_sample, window.stop_sample
        )
        raw = np.frombuffer(payload, dtype="<i2")
        expected = window.sample_count * receiver_count * 2
        if raw.size != expected:
            raise ValueError("receiver-agnostic CFO/QAM CI16 interval is truncated")
        values = raw.reshape(window.sample_count, receiver_count, 2)[:, receiver_index]
        return cast(
            Sequence[complex],
            np.asarray(
                (values[:, 0].astype(np.float32) + 1j * values[:, 1]) / 32768.0,
                dtype=np.complex64,
            ),
        )


class CaptureAwareReceiverAgnosticCfoQamRunnerV0_6:
    """Run at most one recording only after the shared optional-work gate admits it."""

    def __init__(
        self,
        admission: OptionalHeavyWorkAdmissionPortV0_1,
        producer: DurableReceiverAgnosticCfoQamProducerV0_6,
    ) -> None:
        self._admission, self._producer = admission, producer

    def run_once(
        self,
        recording_id: RecordingId,
        selections: tuple[ReceiverAgnosticCfoQamWindowSelectionV0_6, ...],
    ) -> tuple[str, ReceiverAgnosticCfoQamProductionResultV0_6 | None]:
        decision, permit = self._admission.acquire()
        if not decision.admitted or permit is None:
            return decision.reason, None
        try:
            return "complete", self._producer.produce(recording_id, selections)
        finally:
            permit.release()


def recording_receiver_agnostic_cfo_qam_bundle_v0_6(
    request: ReceiverAgnosticCfoQamRecordingRequestV0_6,
    products: tuple[ReceiverAgnosticCfoQamWindowBundleV0_6, ...],
    execution: AnalysisExecutionContext,
) -> ReceiverAgnosticCfoQamRecordingBundleV0_6:
    if tuple(item.window for item in products) != request.windows:
        raise ValueError("receiver-agnostic CFO/QAM products differ from request")
    identity = canonical_digest(
        {
            "request_digest": request.digest,
            "products": tuple(x.digest for x in products),
        }
    )
    provenance = Provenance(
        execution.producer_name,
        execution.producer_version,
        execution.git_commit,
        execution.environment_digest,
        request.plan.digest,
        (request.recording_object_ref.identity_digest(), request.digest),
        tuple(item.digest for item in products),
        execution.started_utc_ns,
        execution.completed_utc_ns,
        execution.host_class,
    )
    return ReceiverAgnosticCfoQamRecordingBundleV0_6(
        SchemaRef(ReceiverAgnosticCfoQamRecordingBundleV0_6.SCHEMA_ID, V0_6),
        f"slcfoqam6rec_{identity.value[:32]}",
        request.recording_id,
        request.recording_object_ref.identity_digest(),
        request.digest,
        request.plan,
        products,
        provenance,
        True,
        None,
        tuple(
            sorted(
                (
                    "candidate-evidence-not-calibrated-detection",
                    "explicit-offline-publication-only",
                    "identical-residual-cfo-domain-for-every-radio-rx",
                    "no-lnb-label-center-or-receiver-correction",
                    "pattern-symmetric-known-pattern-qam",
                )
            )
        ),
    )
