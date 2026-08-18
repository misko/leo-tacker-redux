"""Fenced optional production of durable recording-level symbolwise replay."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

import numpy as np

from leo_flow.analysis.recording.api import AnalysisExecutionContext
from leo_flow.analysis.recording.starlink_symbolwise_replay import (
    StarlinkSymbolwiseReplayAnalyzerV0_1,
    StarlinkSymbolwiseReplayConfigV0_1,
)
from leo_flow.analysis.recording.starlink_symbolwise_replay_product_persistence import (
    DurableStarlinkSymbolwiseReplayStoreV0_1,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    Provenance,
    SchemaRef,
    canonical_digest,
)
from leo_flow.contracts.starlink_symbolwise_replay import (
    StarlinkSymbolwiseReplayBundleV0_1,
)
from leo_flow.contracts.starlink_symbolwise_replay_product import (
    V0_1,
    StarlinkSymbolwiseRecordingBundleV0_1,
    StarlinkSymbolwiseReplayPublicationFenceV0_1,
    StarlinkSymbolwiseReplayRequestV0_1,
    StarlinkSymbolwiseReplayStreamSelectionV0_1,
)
from leo_flow.contracts.storage import RecordingObjectRef
from leo_flow.services.capture_batch_analysis import PublishedRecordingCatalog
from leo_flow.storage.ports import RecordingObjectReader, RecordingView


class StaleStarlinkSymbolwiseReplayLeaseError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StarlinkSymbolwiseReplayWorkLeaseV0_1:
    work_id: str
    request: StarlinkSymbolwiseReplayRequestV0_1
    recording_ref: RecordingObjectRef
    lease_token: str
    lease_generation: int
    attempt: int


class StarlinkSymbolwiseReplayWorkRepositoryV0_1(Protocol):
    def claim(
        self, worker_id: str, lease_ttl_s: float
    ) -> StarlinkSymbolwiseReplayWorkLeaseV0_1 | None: ...

    def complete(
        self, lease: StarlinkSymbolwiseReplayWorkLeaseV0_1, result: ArtifactRef
    ) -> None: ...

    def retry(
        self, lease: StarlinkSymbolwiseReplayWorkLeaseV0_1, reason: str
    ) -> None: ...

    def park(
        self, lease: StarlinkSymbolwiseReplayWorkLeaseV0_1, reason: str
    ) -> None: ...


class StarlinkSymbolwiseReplayLeaseProducerV0_1(Protocol):
    def produce(self, lease: StarlinkSymbolwiseReplayWorkLeaseV0_1) -> ArtifactRef: ...


class BoundedStarlinkSymbolwiseReplayServiceV0_1:
    """Process at most one explicitly queued replay per capture-aware cycle."""

    def __init__(
        self,
        work: StarlinkSymbolwiseReplayWorkRepositoryV0_1,
        producer: StarlinkSymbolwiseReplayLeaseProducerV0_1,
        *,
        worker_id: str,
        lease_ttl_s: float = 7200.0,
        maximum_attempts: int = 3,
    ) -> None:
        if not worker_id or lease_ttl_s <= 0 or maximum_attempts <= 0:
            raise ValueError("symbolwise replay worker bounds are invalid")
        self._work, self._producer = work, producer
        self._worker_id, self._lease_ttl_s = worker_id, lease_ttl_s
        self._maximum_attempts = maximum_attempts

    def run_once(self) -> bool:
        lease = self._work.claim(self._worker_id, self._lease_ttl_s)
        if lease is None:
            return False
        try:
            result = self._producer.produce(lease)
            self._work.complete(lease, result)
        except StaleStarlinkSymbolwiseReplayLeaseError:
            pass
        except ValueError:
            self._safe_park(lease, "symbolwise-replay-invalid-input")
        except Exception:  # noqa: BLE001 - durable optional-work boundary
            if lease.attempt >= self._maximum_attempts:
                self._safe_park(lease, "symbolwise-replay-attempts-exhausted")
            else:
                try:
                    self._work.retry(lease, "symbolwise-replay-transient-failure")
                except StaleStarlinkSymbolwiseReplayLeaseError:
                    pass
        return True

    def _safe_park(
        self, lease: StarlinkSymbolwiseReplayWorkLeaseV0_1, reason: str
    ) -> None:
        try:
            self._work.park(lease, reason)
        except StaleStarlinkSymbolwiseReplayLeaseError:
            pass


class SymbolwiseWindowReaderV0_1(Protocol):
    def read_window(
        self, start_sample: int, sample_count: int
    ) -> Sequence[complex]: ...


AnalyzerFactory = Callable[
    [StarlinkSymbolwiseReplayConfigV0_1, AnalysisExecutionContext],
    StarlinkSymbolwiseReplayAnalyzerV0_1,
]


class DurableStarlinkSymbolwiseReplayLeaseProducerV0_1:
    def __init__(
        self,
        recordings: PublishedRecordingCatalog,
        reader: RecordingObjectReader,
        products: DurableStarlinkSymbolwiseReplayStoreV0_1,
        execution: AnalysisExecutionContext,
        *,
        analyzer_factory: AnalyzerFactory = StarlinkSymbolwiseReplayAnalyzerV0_1,
    ) -> None:
        self._recordings, self._reader, self._products = recordings, reader, products
        self._execution = execution
        self._analyzer_factory = analyzer_factory

    def produce(self, lease: StarlinkSymbolwiseReplayWorkLeaseV0_1) -> ArtifactRef:
        request = lease.request
        published = self._recordings.get(request.recording_id)
        if (
            published is None
            or published.recording_object != lease.recording_ref
            or published.recording_object != request.recording_object_ref
        ):
            raise ValueError("symbolwise replay recording is not exact and published")
        config = _analysis_config(request)
        analyzer = self._analyzer_factory(config, self._execution)
        with self._reader.open(lease.recording_ref) as recording:
            streams = tuple(
                self._analyze_stream(recording, request, selection, analyzer)
                for selection in request.stream_selections
            )
        bundle = recording_symbolwise_bundle_v0_1(request, streams, self._execution)
        result = self._products.publish(
            request,
            bundle,
            lease_fence=StarlinkSymbolwiseReplayPublicationFenceV0_1(
                lease.work_id, lease.lease_token, lease.lease_generation
            ),
            idempotency_key=f"symbolwise-replay:{lease.work_id}:{request.digest.value}",
        )
        return ArtifactRef(result.analysis_id, result.bundle_ref.digest, bundle.schema)

    def _analyze_stream(
        self,
        recording: RecordingView,
        request: StarlinkSymbolwiseReplayRequestV0_1,
        selection: StarlinkSymbolwiseReplayStreamSelectionV0_1,
        analyzer: StarlinkSymbolwiseReplayAnalyzerV0_1,
    ) -> StarlinkSymbolwiseReplayBundleV0_1:
        manifest = recording.manifest
        if manifest.radio_id != selection.radio_id:
            raise ValueError("symbolwise selection radio differs from recording")
        segments = {item.segment_id: item for item in manifest.segments}
        segment = segments.get(selection.segment_id)
        if (
            segment is None
            or segment.actual_sample_rate_hz != selection.sample_rate_hz
            or segment.sample_count != selection.segment_sample_count
            or selection.receiver_chain_id not in segment.requested.receiver_chain_ids
        ):
            raise ValueError("symbolwise selection differs from recording stream")
        window_samples = round(selection.sample_rate_hz * 0.010)
        cadence_samples = round(selection.sample_rate_hz * 0.100)
        starts = tuple(
            range(
                0, selection.segment_sample_count - window_samples + 1, cadence_samples
            )
        )
        contiguous = recording.contiguous_rf_spans(selection.segment_id)
        if any(
            not any(
                span.start_sample <= start
                and start + window_samples <= span.stop_sample
                for span in contiguous
            )
            for start in starts
        ):
            raise ValueError("symbolwise replay window crosses unverified continuity")
        reader = _Ci16ReceiverWindowReader(
            recording,
            selection,
            segment.requested.receiver_chain_ids.index(selection.receiver_chain_id),
            len(segment.requested.receiver_chain_ids),
        )
        return analyzer.analyze_receiver(
            reader,
            recording_id=request.recording_id,
            recording_identity_digest=request.recording_object_ref.identity_digest(),
            segment_id=selection.segment_id,
            receiver_chain_id=selection.receiver_chain_id,
            edge=selection.edge,
            sample_rate_hz=selection.sample_rate_hz,
            segment_sample_count=selection.segment_sample_count,
            frequency_center=selection.frequency_center,
        )


class _Ci16ReceiverWindowReader:
    def __init__(
        self,
        recording: RecordingView,
        selection: StarlinkSymbolwiseReplayStreamSelectionV0_1,
        receiver_index: int,
        receiver_count: int,
    ) -> None:
        self._recording = recording
        self._selection = selection
        self._receiver_index = receiver_index
        self._receiver_count = receiver_count

    def read_window(self, start_sample: int, sample_count: int) -> Sequence[complex]:
        data = self._recording.read_iq_bytes(
            self._selection.segment_id,
            start_sample,
            start_sample + sample_count,
        )
        expected_components = sample_count * self._receiver_count * 2
        raw = np.frombuffer(data, dtype="<i2")
        if raw.size != expected_components:
            raise ValueError("symbolwise replay received truncated ci16 bytes")
        values = raw.reshape(sample_count, self._receiver_count, 2)[
            :, self._receiver_index
        ]
        return cast(
            Sequence[complex],
            np.asarray(
                (values[:, 0].astype(np.float32) + 1j * values[:, 1]) / 32768.0,
                dtype=np.complex64,
            ),
        )


def _analysis_config(
    request: StarlinkSymbolwiseReplayRequestV0_1,
) -> StarlinkSymbolwiseReplayConfigV0_1:
    plan = request.plan
    return StarlinkSymbolwiseReplayConfigV0_1(
        surrogate_count=plan.surrogate_count,
        maximum_windows=plan.maximum_windows,
        maximum_window_samples=plan.maximum_window_samples,
        maximum_timing_search_cells=plan.maximum_timing_search_cells,
        maximum_refinement_search_cells=plan.maximum_refinement_search_cells,
        maximum_working_bytes=plan.maximum_working_bytes,
    )


def recording_symbolwise_bundle_v0_1(
    request: StarlinkSymbolwiseReplayRequestV0_1,
    streams: tuple[StarlinkSymbolwiseReplayBundleV0_1, ...],
    execution: AnalysisExecutionContext,
) -> StarlinkSymbolwiseRecordingBundleV0_1:
    identity = canonical_digest(
        {
            "request_digest": request.digest,
            "stream_digests": tuple(stream.digest for stream in streams),
        }
    )
    provenance = Provenance(
        execution.producer_name,
        execution.producer_version,
        execution.git_commit,
        execution.environment_digest,
        request.plan.digest,
        (request.recording_object_ref.identity_digest(), request.digest),
        tuple(stream.digest for stream in streams),
        execution.started_utc_ns,
        execution.completed_utc_ns,
        execution.host_class,
    )
    return StarlinkSymbolwiseRecordingBundleV0_1(
        SchemaRef(StarlinkSymbolwiseRecordingBundleV0_1.SCHEMA_ID, V0_1),
        f"slsymrec_{identity.value[:32]}",
        request.recording_id,
        request.recording_object_ref.identity_digest(),
        request.digest,
        request.plan,
        request.stream_selections,
        streams,
        provenance,
        True,
        (
            "finite-pattern-controls-not-empirical-null",
            "whole-search-calibration-required",
            "explicit-on-demand-or-backfill-only",
            "candidate-evidence-not-calibrated-detection",
        ),
    )
