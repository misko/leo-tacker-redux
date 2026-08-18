from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace

import pytest

from leo_flow.analysis.recording.starlink_receiver_agnostic_cfo_product_persistence import (
    CatalogedReceiverAgnosticCfoQamV0_6,
    DurableReceiverAgnosticCfoQamStoreV0_6,
)
from leo_flow.contracts.core import RecordingId, SegmentId
from leo_flow.contracts.optional_heavy_work_admission import (
    HeavyWorkAdmissionDecisionV0_1,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_receiver_agnostic_cfo import (
    ReceiverAgnosticCfoSearchPlanV0_6,
)
from leo_flow.services.starlink_receiver_agnostic_cfo_qam import (
    CaptureAwareReceiverAgnosticCfoQamRunnerV0_6,
    DurableReceiverAgnosticCfoQamProducerV0_6,
    ReceiverAgnosticCfoQamWindowSelectionV0_6,
)
from leo_flow.storage.filesystem import FileSystemBlobStore
from tests.recording_analysis.fakes import (
    RX_IDS,
    SegmentFixture,
    execution_context,
    make_view,
)


class _RecordingCatalog:
    def __init__(self, ref):  # type: ignore[no-untyped-def]
        self._ref = ref

    def get(self, recording_id):  # type: ignore[no-untyped-def]
        from leo_flow.contracts.storage import PublishedRecordingRef

        return (
            PublishedRecordingRef(self._ref)
            if recording_id == self._ref.recording_id
            else None
        )


class _Reader:
    def __init__(self, ref, view):  # type: ignore[no-untyped-def]
        self._ref, self._view = ref, view
        self.opens = 0

    @contextmanager
    def open(self, ref):  # type: ignore[no-untyped-def]
        assert ref == self._ref
        self.opens += 1
        yield self._view


class _ProductCatalog:
    def __init__(self) -> None:
        self.item = None
        self.key = None

    def publish_receiver_agnostic_cfo_qam(
        self, projection, bundle_ref, recording_ref, *, idempotency_key
    ):  # type: ignore[no-untyped-def]
        del recording_ref
        candidate = CatalogedReceiverAgnosticCfoQamV0_6(projection, bundle_ref)
        if self.item is None:
            self.item, self.key = candidate, idempotency_key
        assert self.item == candidate and self.key == idempotency_key
        return self.item.ref

    def get_receiver_agnostic_cfo_qam(self, ref):  # type: ignore[no-untyped-def]
        return self.item if self.item is not None and self.item.ref == ref else None

    def latest_receiver_agnostic_cfo_qam(self, recording_id):  # type: ignore[no-untyped-def]
        return (
            self.item.ref
            if self.item is not None and self.item.ref.recording_id == recording_id
            else None
        )


def _producer(tmp_path):  # type: ignore[no-untyped-def]
    raw = bytes(7_500 * 2 * 2 * 2)
    view, ref = make_view(
        SegmentFixture(raw, 2_500_000),
        recording_id=RecordingId("rec_v30_producer"),
    )
    product_catalog = _ProductCatalog()
    products = DurableReceiverAgnosticCfoQamStoreV0_6(
        FileSystemBlobStore(tmp_path / "cas"), product_catalog
    )
    reader = _Reader(ref, view)
    search = replace(
        ReceiverAgnosticCfoSearchPlanV0_6(),
        coarse_cfo_step_hz=350_000.0,
        local_cfo_radius_hz=350_000.0,
        local_cfo_step_hz=350_000.0,
        basins_per_pattern=1,
        basin_cfo_separation_hz=350_000.0,
    )
    producer = DurableReceiverAgnosticCfoQamProducerV0_6(
        _RecordingCatalog(ref),
        reader,
        products,
        product_catalog,
        execution_context(),
        search_plan=search,
        pattern_count=2,
    )
    return producer, reader, view


def test_explicit_producer_publishes_and_reuses_exact_request(tmp_path) -> None:
    producer, reader, view = _producer(tmp_path)
    selections = (
        ReceiverAgnosticCfoQamWindowSelectionV0_6(
            SegmentId("seg_00"), RX_IDS[0], StarlinkEdge.LOWER, 0, 7_500
        ),
    )

    first = producer.produce(RecordingId("rec_v30_producer"), selections)
    read_calls = tuple(view.calls)
    second = producer.produce(RecordingId("rec_v30_producer"), selections)

    assert not first.reused and second.reused
    assert second.ref == first.ref
    assert tuple(view.calls) == read_calls
    assert reader.opens == 2  # The second open validates the current manifest/request.
    assert read_calls == ((SegmentId("seg_00"), 0, 7_500),)


def test_explicit_producer_rejects_resource_and_continuity_violations(tmp_path) -> None:
    producer, _, _ = _producer(tmp_path)
    duplicate_stream_windows = tuple(
        ReceiverAgnosticCfoQamWindowSelectionV0_6(
            SegmentId("seg_00"), RX_IDS[0], StarlinkEdge.LOWER, index, 100
        )
        for index in range(4)
    )
    with pytest.raises(ValueError, match="stream bounds"):
        producer.produce(RecordingId("rec_v30_producer"), duplicate_stream_windows)
    with pytest.raises(ValueError, match="unavailable"):
        producer.produce(
            RecordingId("rec_v30_producer"),
            (
                ReceiverAgnosticCfoQamWindowSelectionV0_6(
                    SegmentId("seg_00"),
                    RX_IDS[0],
                    StarlinkEdge.LOWER,
                    7_499,
                    2,
                ),
            ),
        )


class _Permit:
    def __init__(self) -> None:
        self.releases = 0

    def release(self) -> None:
        self.releases += 1


class _Admission:
    def __init__(self, admitted: bool, permit=None) -> None:
        self._admitted, self._permit = admitted, permit

    def acquire(self):  # type: ignore[no-untyped-def]
        return (
            HeavyWorkAdmissionDecisionV0_1(
                self._admitted,
                "admitted" if self._admitted else "capture-guard-active",
            ),
            self._permit,
        )


class _ObservedProducer:
    def __init__(self) -> None:
        self.calls = 0

    def produce(self, *_args):  # type: ignore[no-untyped-def]
        self.calls += 1
        return "result"


def test_capture_guard_denial_never_enters_producer_and_permit_is_released() -> None:
    observed = _ObservedProducer()
    denied = CaptureAwareReceiverAgnosticCfoQamRunnerV0_6(  # type: ignore[arg-type]
        _Admission(False), observed
    )
    reason, result = denied.run_once(RecordingId("rec_denied"), ())
    assert (reason, result, observed.calls) == ("capture-guard-active", None, 0)

    permit = _Permit()
    admitted = CaptureAwareReceiverAgnosticCfoQamRunnerV0_6(  # type: ignore[arg-type]
        _Admission(True, permit), observed
    )
    reason, result = admitted.run_once(RecordingId("rec_admitted"), ())
    assert (reason, result, observed.calls, permit.releases) == (
        "complete",
        "result",
        1,
        1,
    )
