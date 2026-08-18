from __future__ import annotations

import numpy as np
import pytest

from leo_flow.contracts.core import ArtifactRef, Digest, SchemaRef
from leo_flow.services.starlink_symbolwise_replay_product import (
    BoundedStarlinkSymbolwiseReplayServiceV0_1,
    DurableStarlinkSymbolwiseReplayLeaseProducerV0_1,
    StaleStarlinkSymbolwiseReplayLeaseError,
    StarlinkSymbolwiseReplayWorkLeaseV0_1,
    _Ci16ReceiverWindowReader,
    recording_symbolwise_bundle_v0_1,
)
from tests.recording_analysis.fakes import execution_context
from tests.recording_analysis.symbolwise_product_fixtures import (
    receiver_bundle,
    request,
)


def _lease(*, attempt: int = 1) -> StarlinkSymbolwiseReplayWorkLeaseV0_1:
    replay_request = request()
    return StarlinkSymbolwiseReplayWorkLeaseV0_1(
        "slsymwork_" + "a" * 32,
        replay_request,
        replay_request.recording_object_ref,
        "worker:lease_1",
        1,
        attempt,
    )


class _Work:
    def __init__(self, lease=None, *, stale_transition: bool = False) -> None:
        self.lease = lease
        self.stale_transition = stale_transition
        self.transitions = []
        self.claims = 0

    def claim(self, worker_id, lease_ttl_s):
        self.claims += 1
        self.claim_args = worker_id, lease_ttl_s
        value, self.lease = self.lease, None
        return value

    def _transition(self, name, *values):
        if self.stale_transition:
            raise StaleStarlinkSymbolwiseReplayLeaseError("stale")
        self.transitions.append((name, *values))

    def complete(self, lease, result):
        self._transition("complete", lease, result)

    def retry(self, lease, reason):
        self._transition("retry", lease, reason)

    def park(self, lease, reason):
        self._transition("park", lease, reason)


class _Producer:
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.calls = []

    def produce(self, lease):
        self.calls.append(lease)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _result() -> ArtifactRef:
    return ArtifactRef(
        "slsymrec_" + "b" * 32,
        Digest.sha256(b"bundle"),
        SchemaRef("org.leo-flow.starlink-symbolwise-recording-bundle"),
    )


@pytest.mark.parametrize(
    ("outcome", "attempt", "expected"),
    (
        (_result(), 1, "complete"),
        (RuntimeError("transient"), 1, "retry"),
        (RuntimeError("exhausted"), 3, "park"),
        (ValueError("immutable input differs"), 1, "park"),
    ),
)
def test_bounded_service_claims_at_most_one_and_uses_fenced_transition(
    outcome, attempt, expected
) -> None:
    lease = _lease(attempt=attempt)
    work = _Work(lease)
    producer = _Producer(outcome)
    service = BoundedStarlinkSymbolwiseReplayServiceV0_1(
        work, producer, worker_id="symbolwise-worker", lease_ttl_s=7_200
    )

    assert service.run_once()
    assert work.claims == 1
    assert len(producer.calls) == 1
    assert work.transitions[0][0] == expected
    assert service.run_once() is False
    assert work.claims == 2


def test_stale_transition_is_never_retried_under_another_fence() -> None:
    work = _Work(_lease(), stale_transition=True)
    service = BoundedStarlinkSymbolwiseReplayServiceV0_1(
        work, _Producer(_result()), worker_id="symbolwise-worker"
    )

    assert service.run_once()
    assert work.transitions == []


def test_recording_factory_preserves_full_candidate_accounting_and_cfo_source() -> None:
    replay_request = request()
    stream = receiver_bundle(replay_request)

    result = recording_symbolwise_bundle_v0_1(
        replay_request, (stream,), execution_context()
    )

    assert (
        result.streams[0].frequency_center
        == replay_request.stream_selections[0].frequency_center
    )
    assert result.total_window_count == 600
    assert result.total_pattern_evidence_count == 3_000
    assert result.streams[0].coverage_fraction == 0.1
    assert result.candidates_only


class _TinyRecording:
    def read_iq_bytes(self, segment_id, start_sample, stop_sample):
        del segment_id, start_sample
        samples = stop_sample
        values = np.array(
            [
                [[1, -2], [3, -4]],
                [[5, -6], [7, -8]],
            ],
            dtype="<i2",
        )
        return values[:samples].tobytes()


def test_ci16_reader_selects_receiver_without_pooling_or_label_inference() -> None:
    selection = request().stream_selections[0]
    first = _Ci16ReceiverWindowReader(_TinyRecording(), selection, 0, 2)
    second = _Ci16ReceiverWindowReader(_TinyRecording(), selection, 1, 2)

    assert np.asarray(first.read_window(0, 2)) == pytest.approx(
        np.array([1 - 2j, 5 - 6j]) / 32768
    )
    assert np.asarray(second.read_window(0, 2)) == pytest.approx(
        np.array([3 - 4j, 7 - 8j]) / 32768
    )


class _NoRecordingCatalog:
    def get(self, recording_id):
        del recording_id


def test_producer_rejects_nonpublished_source_before_open_or_analysis() -> None:
    class _Never:
        def __getattr__(self, name):
            raise AssertionError(f"unexpected dependency access: {name}")

    producer = DurableStarlinkSymbolwiseReplayLeaseProducerV0_1(
        _NoRecordingCatalog(), _Never(), _Never(), execution_context()
    )

    with pytest.raises(ValueError, match="not exact and published"):
        producer.produce(_lease())
