from __future__ import annotations

import json
from dataclasses import replace
from io import StringIO
from pathlib import Path

import pytest

from leo_flow.analysis.recording.starlink_symbolwise_replay_product_codec import (
    encode_starlink_symbolwise_replay_request,
)
from leo_flow.contracts.storage import PublishedRecordingRef
from leo_station.symbolwise_replay_enqueue_operator import enqueue_exact_request, main
from tests.recording_analysis.symbolwise_product_fixtures import request


class _Catalog:
    def __init__(self, published: PublishedRecordingRef | None) -> None:
        self.published = published

    def get(self, _recording_id):
        return self.published


class _Queue:
    def __init__(self) -> None:
        self.calls: list[tuple[object, int, str]] = []

    def enqueue(self, replay_request, *, priority: int, idempotency_key: str) -> str:
        self.calls.append((replay_request, priority, idempotency_key))
        return "slsymwork_" + "1" * 32


def _write_request(path: Path) -> object:
    replay_request = request()
    path.write_bytes(encode_starlink_symbolwise_replay_request(replay_request))
    return replay_request


def test_exact_enqueue_checks_full_public_recording_identity() -> None:
    replay_request = request()
    queue = _Queue()
    assert (
        enqueue_exact_request(
            replay_request,
            _Catalog(PublishedRecordingRef(replay_request.recording_object_ref)),
            queue,
            priority=7,
        )
        == "slsymwork_" + "1" * 32
    )
    assert queue.calls == [
        (
            replay_request,
            7,
            f"symbolwise-replay-explicit:{replay_request.digest.value}",
        )
    ]

    changed = replace(
        replay_request.recording_object_ref,
        manifest_digest=replay_request.recording_object_ref.data_object.digest,
    )
    with pytest.raises(ValueError, match="exact recording"):
        enqueue_exact_request(
            replay_request,
            _Catalog(PublishedRecordingRef(changed)),
            queue,
            priority=7,
        )
    assert len(queue.calls) == 1


def test_validate_request_is_offline_and_reports_exact_identities(
    tmp_path: Path,
) -> None:
    path = tmp_path / "request.json"
    replay_request = _write_request(path)
    output = StringIO()

    def no_external_access(*_args, **_kwargs):
        raise AssertionError("validation attempted external access")

    assert (
        main(
            ["validate-request", "--request", str(path)],
            stdout=output,
            enqueuer=no_external_access,
        )
        == 0
    )
    assert json.loads(output.getvalue()) == {
        "event": "symbolwise_replay_request_valid",
        "recording_id": str(replay_request.recording_id),
        "recording_identity_digest": (
            replay_request.recording_object_ref.identity_digest().value
        ),
        "request_digest": replay_request.digest.value,
        "stream_count": 1,
    }


def test_enqueue_request_is_one_explicit_bounded_action(tmp_path: Path) -> None:
    path = tmp_path / "request.json"
    replay_request = _write_request(path)
    output = StringIO()
    observed = {}

    def enqueue(item, credentials, *, priority: int) -> str:
        observed.update(item=item, credentials=credentials, priority=priority)
        return "slsymwork_" + "2" * 32

    assert (
        main(
            [
                "enqueue-request",
                "--request",
                str(path),
                "--credential-directory",
                "/credentials",
                "--priority",
                "9",
            ],
            stdout=output,
            enqueuer=enqueue,
        )
        == 0
    )
    assert observed == {
        "item": replay_request,
        "credentials": Path("/credentials"),
        "priority": 9,
    }
    assert json.loads(output.getvalue())["work_id"] == "slsymwork_" + "2" * 32


def test_enqueue_failure_is_sanitized(tmp_path: Path) -> None:
    path = tmp_path / "request.json"
    _write_request(path)
    errors = StringIO()

    def fail(*_args, **_kwargs):
        raise RuntimeError("postgresql://secret")

    assert (
        main(
            [
                "enqueue-request",
                "--request",
                str(path),
                "--credential-directory",
                "/credentials",
            ],
            stderr=errors,
            enqueuer=fail,
        )
        == 3
    )
    assert errors.getvalue() == '{"event":"symbolwise_replay_enqueue_failed"}\n'
    assert "secret" not in errors.getvalue()
