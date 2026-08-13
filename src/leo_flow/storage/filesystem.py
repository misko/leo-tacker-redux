"""Content-addressed filesystem blob storage with crash-safe publication."""

from __future__ import annotations

import hashlib
import os
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Self, cast

from leo_flow.contracts.core import Digest, DigestAlgorithm
from leo_flow.contracts.storage import ByteRange, ObjectMetadata, ObjectRef


class BlobStoreError(RuntimeError):
    """Base error for filesystem blob operations."""


class BlobIntegrityError(BlobStoreError):
    """Bytes, length, or immutable identity do not match the reference."""


class IdempotencyConflictError(BlobStoreError):
    """An idempotency key was reused for different content."""


class FileSystemBlobStore:
    """A dependency-free CAS adapter; paths never escape this implementation."""

    _LOCATOR_PREFIX = "cas:sha256:"

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root)
        self._tmp = self._root / ".tmp"
        self._tmp.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._idempotency: dict[str, tuple[Digest, int]] = {}

    def put(
        self,
        stream: BinaryIO,
        *,
        expected_digest: Digest,
        expected_bytes: int,
        media_type: str,
        format_id: str,
        idempotency_key: str,
    ) -> ObjectRef:
        if expected_bytes < 0:
            raise ValueError("expected_bytes must be non-negative")
        if not idempotency_key:
            raise ValueError("idempotency_key cannot be empty")
        identity = (expected_digest, expected_bytes)
        with self._lock:
            prior = self._idempotency.get(idempotency_key)
            if prior is not None and prior != identity:
                raise IdempotencyConflictError(
                    "idempotency key already identifies different bytes"
                )

        temporary = self._tmp / f"{uuid.uuid4().hex}.partial"
        hasher = hashlib.sha256()
        written = 0
        try:
            with temporary.open("xb", buffering=0) as output:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    hasher.update(chunk)
                    written += len(chunk)
                    if written > expected_bytes:
                        raise BlobIntegrityError("stream exceeds expected byte count")
                output.flush()
                os.fsync(output.fileno())
            actual = Digest(DigestAlgorithm.SHA256, hasher.hexdigest())
            if written != expected_bytes:
                raise BlobIntegrityError(
                    f"byte count differs: expected {expected_bytes}, received {written}"
                )
            if actual != expected_digest:
                raise BlobIntegrityError("stream digest does not match expected digest")

            target = self._path_for_digest(expected_digest)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(temporary, target)
                self._fsync_directory(target.parent)
            except FileExistsError:
                self._verify_path(target, expected_digest, expected_bytes)
            temporary.unlink(missing_ok=True)
            with self._lock:
                prior = self._idempotency.get(idempotency_key)
                if prior is not None and prior != identity:
                    raise IdempotencyConflictError(
                        "idempotency key raced with different content"
                    )
                self._idempotency[idempotency_key] = identity
            return ObjectRef(
                expected_digest,
                expected_bytes,
                media_type,
                format_id,
                self._locator(expected_digest),
            )
        finally:
            temporary.unlink(missing_ok=True)

    def head(self, ref: ObjectRef) -> ObjectMetadata:
        path = self._resolve(ref)
        self._verify_path(path, ref.digest, ref.byte_count)
        return ObjectMetadata(ref, verified=True)

    @contextmanager
    def open(
        self, ref: ObjectRef, byte_range: ByteRange | None = None
    ) -> Iterator[BinaryIO]:
        path = self._resolve(ref)
        self._verify_path(path, ref.digest, ref.byte_count)
        stream = path.open("rb", buffering=0)
        try:
            if byte_range is None:
                yield stream
            else:
                if byte_range.stop > ref.byte_count:
                    raise BlobIntegrityError("requested range exceeds object")
                stream.seek(byte_range.start)
                yield cast(
                    BinaryIO, _BoundedReader(stream, byte_range.stop - byte_range.start)
                )
        finally:
            stream.close()

    def _resolve(self, ref: ObjectRef) -> Path:
        if ref.digest.algorithm is not DigestAlgorithm.SHA256:
            raise BlobIntegrityError("unsupported digest algorithm")
        if ref.locator != self._locator(ref.digest):
            raise BlobIntegrityError("opaque locator does not match object digest")
        return self._path_for_digest(ref.digest)

    def _path_for_digest(self, digest: Digest) -> Path:
        return self._root / "sha256" / digest.value[:2] / digest.value

    def _locator(self, digest: Digest) -> str:
        return f"{self._LOCATOR_PREFIX}{digest.value}"

    @staticmethod
    def _verify_path(path: Path, digest: Digest, expected_bytes: int) -> None:
        try:
            stat = path.stat()
        except FileNotFoundError as error:
            raise BlobIntegrityError("object is missing") from error
        if not path.is_file() or stat.st_size != expected_bytes:
            raise BlobIntegrityError("stored object byte count differs")
        hasher = hashlib.sha256()
        with path.open("rb", buffering=0) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        if hasher.hexdigest() != digest.value:
            raise BlobIntegrityError("stored object digest differs")

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class _BoundedReader:
    """Minimal BinaryIO-compatible view that cannot cross a requested range."""

    def __init__(self, stream: BinaryIO, remaining: int) -> None:
        self._stream = stream
        self._remaining = remaining

    def read(self, size: int = -1) -> bytes:
        if size < 0 or size > self._remaining:
            size = self._remaining
        data = self._stream.read(size)
        self._remaining -= len(data)
        return data

    def close(self) -> None:
        self._stream.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
