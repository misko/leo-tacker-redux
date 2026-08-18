from leo_flow.contracts.core import Digest, RecordingId
from leo_flow.services.starlink_full_dwell_queue import (
    BoundedFullDwellWorkQueueV0_1,
    FullDwellQueuePolicyV0_1,
    FullDwellWorkItemV0_1,
)


def _item(index: int) -> FullDwellWorkItemV0_1:
    return FullDwellWorkItemV0_1(
        RecordingId(f"rec_fd_{index}"), Digest.sha256(str(index).encode())
    )


def test_queue_admission_is_nonblocking_bounded_and_explicit_on_saturation() -> None:
    queue = BoundedFullDwellWorkQueueV0_1(FullDwellQueuePolicyV0_1(maximum_pending=2))
    assert queue.try_submit(_item(1)).accepted
    assert queue.try_submit(_item(2)).backlog_depth == 2
    saturated = queue.try_submit(_item(3))
    assert not saturated.accepted
    assert saturated.truncated
    assert saturated.reason == "bounded-queue-saturated"
    assert saturated.backlog_depth == 2


def test_queue_exact_replay_does_not_duplicate_work_and_claim_never_waits() -> None:
    queue = BoundedFullDwellWorkQueueV0_1(FullDwellQueuePolicyV0_1(maximum_pending=1))
    item = _item(1)
    queue.try_submit(item)
    replay = queue.try_submit(item)
    assert replay.accepted and replay.reason == "exact-replay"
    assert queue.try_claim() == item
    assert queue.try_claim() is None
    assert queue.backlog_depth == 0
