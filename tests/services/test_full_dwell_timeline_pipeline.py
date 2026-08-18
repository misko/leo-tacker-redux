from __future__ import annotations

from contextlib import contextmanager

from leo_flow.analysis.recording.starlink_full_dwell_timeline_persistence import (
    DurableFullDwellTimelineStoreV0_1,
)
from leo_flow.services.full_dwell_timeline import FullDwellTimelineLeaseV0_1
from leo_flow.services.full_dwell_timeline_pipeline import (
    DurableFullDwellTimelineLeaseProducerV0_1,
)
from leo_flow.storage.filesystem import FileSystemBlobStore
from tests.recording_analysis.fakes import execution_context
from tests.recording_analysis.test_starlink_full_dwell_timeline_persistence import (
    _Catalog,
)
from tests.recording_analysis.test_starlink_full_dwell_timeline_product import _case


class _Reader:
    def __init__(self, view) -> None:
        self.view = view
        self.opens = 0

    @contextmanager
    def open(self, recording_ref):
        self.opens += 1
        yield self.view


def test_independent_lease_pipeline_publishes_base_then_returns_exact_work(
    tmp_path,
) -> None:
    view, request, _expected = _case(19)
    catalog = _Catalog()
    products = DurableFullDwellTimelineStoreV0_1(
        FileSystemBlobStore(tmp_path / "cas"), catalog
    )
    reader = _Reader(view)
    producer = DurableFullDwellTimelineLeaseProducerV0_1(
        reader, products, execution_context()
    )
    lease = FullDwellTimelineLeaseV0_1(
        "timeline-work",
        "lease-token",
        1,
        request.recording_object_ref,
        request.plan,
        request.stream_selections,
    )
    result = producer.produce(lease)
    assert reader.opens == 1
    assert result.refinement_request.timeline_ref == result.timeline_ref
    assert len(result.refinement_request.windows) == 6
    with products.open(catalog.item.ref) as persisted:
        assert persisted.recording_id == request.recording_id
        assert all(stream.coverage_fraction == 1.0 for stream in persisted.streams)
