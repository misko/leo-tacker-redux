from __future__ import annotations

import io
from dataclasses import replace

import pytest

from leo_flow.contracts.core import Digest
from leo_flow.contracts.storage import ByteRange
from leo_flow.storage.filesystem import (
    BlobIntegrityError,
    FileSystemBlobStore,
    IdempotencyConflictError,
)


def put(store: FileSystemBlobStore, data: bytes, key: str = "key"):
    return store.put(
        io.BytesIO(data),
        expected_digest=Digest.sha256(data),
        expected_bytes=len(data),
        media_type="application/octet-stream",
        format_id="test-v1",
        idempotency_key=key,
    )


def test_put_is_content_addressed_atomic_and_supports_bounded_reads(tmp_path) -> None:
    store = FileSystemBlobStore(tmp_path)
    ref = put(store, b"0123456789")
    assert ref.locator == f"cas:sha256:{Digest.sha256(b'0123456789').value}"
    assert store.head(ref).verified
    with store.open(ref, ByteRange(2, 6)) as stream:
        assert stream.read() == b"2345"
        assert stream.read() == b""
    assert not list((tmp_path / ".tmp").iterdir())


def test_wrong_stream_length_or_digest_never_publishes(tmp_path) -> None:
    store = FileSystemBlobStore(tmp_path)
    with pytest.raises(BlobIntegrityError, match="byte count"):
        store.put(
            io.BytesIO(b"short"),
            expected_digest=Digest.sha256(b"short"),
            expected_bytes=6,
            media_type="application/octet-stream",
            format_id="test-v1",
            idempotency_key="short",
        )
    with pytest.raises(BlobIntegrityError, match="digest"):
        store.put(
            io.BytesIO(b"wrong"),
            expected_digest=Digest.sha256(b"other"),
            expected_bytes=5,
            media_type="application/octet-stream",
            format_id="test-v1",
            idempotency_key="wrong",
        )
    assert not list((tmp_path / ".tmp").iterdir())
    assert (
        not list((tmp_path / "sha256").rglob("*"))
        if (tmp_path / "sha256").exists()
        else True
    )


def test_existing_corruption_and_truncation_are_detected(tmp_path) -> None:
    store = FileSystemBlobStore(tmp_path)
    ref = put(store, b"original")
    path = tmp_path / "sha256" / ref.digest.value[:2] / ref.digest.value
    path.write_bytes(b"changed!")
    with pytest.raises(BlobIntegrityError, match="digest"):
        store.head(ref)
    path.write_bytes(b"tiny")
    with pytest.raises(BlobIntegrityError, match="byte count"):
        store.head(ref)


def test_existing_content_collision_is_verified_not_overwritten(tmp_path) -> None:
    store = FileSystemBlobStore(tmp_path)
    ref = put(store, b"same", "first")
    assert put(store, b"same", "second").digest == ref.digest
    path = tmp_path / "sha256" / ref.digest.value[:2] / ref.digest.value
    path.write_bytes(b"evil")
    with pytest.raises(BlobIntegrityError):
        put(store, b"same", "third")


def test_idempotency_key_cannot_change_content(tmp_path) -> None:
    store = FileSystemBlobStore(tmp_path)
    put(store, b"one", "stable")
    with pytest.raises(IdempotencyConflictError):
        put(store, b"two", "stable")


def test_locator_cannot_escape_store(tmp_path) -> None:
    store = FileSystemBlobStore(tmp_path)
    ref = put(store, b"safe")
    with pytest.raises(BlobIntegrityError, match="locator"):
        store.head(replace(ref, locator="../../escape"))
