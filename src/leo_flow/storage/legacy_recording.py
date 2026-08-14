"""Strict, read-in-place access to one narrow legacy CI16 format.

The old repository is documentation for this adapter, never a dependency.  A
caller must register every source object and all factual redux metadata.  The
adapter does not discover directories or infer missing capture facts.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol, cast

from leo_flow.contracts.capture import GainMode, RecordingManifest
from leo_flow.contracts.continuity import ContiguousRfSpan, SafeSampleWindow
from leo_flow.contracts.core import Digest, DigestAlgorithm, SegmentId, canonical_digest
from leo_flow.contracts.storage import RecordingObjectRef

from .ports import RecordingView
from .recording_codec import UnverifiedContinuityError


class LegacyRecordingError(RuntimeError):
    """A registration or on-disk legacy object failed closed."""


class UnsupportedLegacyRecordingError(LegacyRecordingError):
    """The source cannot be represented truthfully by the narrow adapter."""


@dataclass(frozen=True)
class LegacyFileRef:
    """One immutable identity beneath an operator-configured storage root."""

    root_id: str
    relative_path: str
    digest: Digest
    byte_count: int

    def __post_init__(self) -> None:
        if not self.root_id:
            raise ValueError("legacy root ID cannot be empty")
        _relative_parts(self.relative_path)
        if self.digest.algorithm is not DigestAlgorithm.SHA256:
            raise ValueError("legacy files require SHA-256 identities")
        if self.byte_count <= 0:
            raise ValueError("legacy files must be non-empty")


def legacy_selected_chunk_index_digest(payloads: tuple[LegacyFileRef, ...]) -> Digest:
    """Provenance identity for the selected ordered chunk index, not its bytes."""

    if not payloads:
        raise ValueError("legacy payload set cannot be empty")
    return canonical_digest(
        {
            "format": "leo-flow.legacy-ci16-chunk-set/v1",
            "members": [
                {
                    "root_id": item.root_id,
                    "relative_path": item.relative_path,
                    "digest": str(item.digest),
                    "byte_count": item.byte_count,
                }
                for item in payloads
            ],
        }
    )


@dataclass(frozen=True)
class LegacyRecordingRegistration:
    """Exact legacy objects plus independently supplied factual redux metadata."""

    recording_ref: RecordingObjectRef
    manifest: RecordingManifest
    source_manifest: LegacyFileRef
    payloads: tuple[LegacyFileRef, ...]
    omitted_survey: LegacyFileRef | None = None

    def __post_init__(self) -> None:
        if self.recording_ref.recording_id != self.manifest.recording_id:
            raise ValueError("registration recording IDs differ")
        if self.recording_ref.manifest_digest != canonical_digest(self.manifest):
            raise ValueError("registration manifest digest differs")
        metadata = self.recording_ref.metadata_object
        if (
            metadata.digest != self.source_manifest.digest
            or metadata.byte_count != self.source_manifest.byte_count
            or metadata.media_type != "application/json"
            or metadata.format_id != "legacy-beacon-manifest-v1"
        ):
            raise ValueError("metadata object does not identify the source manifest")
        data = self.recording_ref.data_object
        if (
            data.digest.algorithm is not DigestAlgorithm.SHA256
            or data.byte_count != sum(item.byte_count for item in self.payloads)
            or data.media_type != "application/octet-stream"
            or data.format_id != "legacy-ci16-chunk-set-v1"
        ):
            raise ValueError("data object cannot identify the ordered payload stream")
        if len(self.manifest.segments) != 1:
            raise UnsupportedLegacyRecordingError(
                "legacy v1 registration supports exactly one redux segment"
            )


class LegacyRecordingReader:
    """Read registered legacy chunks in place as a ``RecordingObjectReader``.

    Roots and registrations are configuration, not discovery inputs.  Opening a
    recording verifies the complete source manifest and every payload before
    returning a view.
    """

    def __init__(
        self,
        roots: Mapping[str, Path],
        registrations: tuple[LegacyRecordingRegistration, ...],
    ) -> None:
        self._roots = _validate_roots(roots)
        self._registrations: dict[str, LegacyRecordingRegistration] = {}
        for registration in registrations:
            key = registration.recording_ref.identity_digest().value
            if key in self._registrations:
                raise ValueError("duplicate legacy recording identity")
            referenced_roots = {
                registration.source_manifest.root_id,
                *(item.root_id for item in registration.payloads),
            }
            if registration.omitted_survey is not None:
                referenced_roots.add(registration.omitted_survey.root_id)
            if not referenced_roots <= self._roots.keys():
                raise ValueError("registration references an unconfigured root ID")
            self._registrations[key] = registration

    @contextmanager
    def open(self, recording_ref: RecordingObjectRef) -> Iterator[RecordingView]:
        key = recording_ref.identity_digest().value
        try:
            registration = self._registrations[key]
        except KeyError as error:
            raise LegacyRecordingError(
                "legacy recording identity is not registered"
            ) from error
        opened: list[_VerifiedFile] = []
        try:
            if registration.source_manifest.byte_count > 16 * 1024 * 1024:
                raise UnsupportedLegacyRecordingError(
                    "legacy manifest exceeds size limit"
                )
            source = _open_verified(self._roots, registration.source_manifest)
            opened.append(source)
            source_value = _decode_source_manifest(source.read_all())
            logical_digest = hashlib.sha256()
            for payload in registration.payloads:
                opened.append(
                    _open_verified(self._roots, payload, logical_digest=logical_digest)
                )
            if registration.omitted_survey is not None:
                opened.append(_open_verified(self._roots, registration.omitted_survey))
            _validate_source(source_value, registration)
            if (
                logical_digest.hexdigest()
                != registration.recording_ref.data_object.digest.value
            ):
                raise LegacyRecordingError("logical dwell byte-stream SHA-256 differs")
            yield cast(
                RecordingView,
                _LegacyRecordingView(
                    registration.manifest,
                    tuple(opened[1 : 1 + len(registration.payloads)]),
                ),
            )
        finally:
            for item in reversed(opened):
                item.close()


class _LegacyRecordingView:
    def __init__(
        self, manifest: RecordingManifest, payloads: tuple[_VerifiedFile, ...]
    ):
        self._manifest = manifest
        self._payloads = payloads
        self._segment_id = manifest.segments[0].segment_id

    @property
    def manifest(self) -> RecordingManifest:
        return self._manifest

    def read_iq_bytes(
        self, segment_id: SegmentId, start_sample: int, stop_sample: int
    ) -> bytes:
        if segment_id != self._segment_id:
            raise KeyError(f"unknown segment {segment_id}")
        sample_count = self._manifest.segments[0].sample_count
        if not 0 <= start_sample < stop_sample <= sample_count:
            raise ValueError("sample range lies outside segment")
        start = start_sample * 8
        stop = stop_sample * 8
        output = bytearray()
        cursor = 0
        for payload in self._payloads:
            payload_stop = cursor + payload.byte_count
            if start < payload_stop and stop > cursor:
                local_start = max(start, cursor) - cursor
                local_stop = min(stop, payload_stop) - cursor
                output.extend(payload.read_range(local_start, local_stop))
            cursor = payload_stop
            if cursor >= stop:
                break
        expected = stop - start
        if len(output) != expected:
            raise LegacyRecordingError("legacy payload stream was truncated")
        return bytes(output)

    def continuity(self, segment_id: SegmentId) -> None:
        if segment_id != self._segment_id:
            raise KeyError(f"unknown segment {segment_id}")

    def contiguous_rf_spans(
        self, segment_id: SegmentId
    ) -> tuple[ContiguousRfSpan, ...]:
        self.continuity(segment_id)
        raise UnverifiedContinuityError(
            "legacy chunks have no metadata-verified RF continuity"
        )

    def iter_safe_windows(
        self, segment_id: SegmentId, window_samples: int, stride_samples: int
    ) -> Iterator[SafeSampleWindow]:
        del window_samples, stride_samples
        self.continuity(segment_id)
        raise UnverifiedContinuityError(
            "legacy chunks have no metadata-verified RF continuity"
        )
        yield  # pragma: no cover - makes this an iterator while always failing


@dataclass(frozen=True)
class _Fingerprint:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


class _VerifiedFile:
    def __init__(self, descriptor: int, ref: LegacyFileRef, fingerprint: _Fingerprint):
        self._descriptor = descriptor
        self._ref = ref
        self._fingerprint = fingerprint

    @property
    def byte_count(self) -> int:
        return self._ref.byte_count

    def read_all(self) -> bytes:
        return self.read_range(0, self.byte_count)

    def read_range(self, start: int, stop: int) -> bytes:
        if not 0 <= start < stop <= self.byte_count:
            raise ValueError("legacy byte range lies outside object")
        self._assert_unchanged()
        data = os.pread(self._descriptor, stop - start, start)
        self._assert_unchanged()
        if len(data) != stop - start:
            raise LegacyRecordingError("legacy object changed or was truncated")
        return data

    def _assert_unchanged(self) -> None:
        if _fingerprint(os.fstat(self._descriptor)) != self._fingerprint:
            raise LegacyRecordingError("verified legacy object changed while open")

    def close(self) -> None:
        os.close(self._descriptor)


@dataclass(frozen=True)
class _ConfiguredRoot:
    path: Path
    device: int
    inode: int


def _validate_roots(roots: Mapping[str, Path]) -> dict[str, _ConfiguredRoot]:
    result: dict[str, _ConfiguredRoot] = {}
    for root_id, raw_root in roots.items():
        if not root_id or root_id in result:
            raise ValueError("storage root IDs must be non-empty and unique")
        root = Path(raw_root)
        if not root.is_absolute():
            raise ValueError("legacy storage roots must be absolute")
        if root.is_symlink():
            raise ValueError("legacy storage root cannot be a symlink")
        try:
            descriptor = os.open(
                root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
            )
        except OSError as error:
            raise ValueError("legacy storage root is not a secure directory") from error
        info = os.fstat(descriptor)
        os.close(descriptor)
        result[root_id] = _ConfiguredRoot(root, info.st_dev, info.st_ino)
    return result


def _relative_parts(value: str) -> tuple[str, ...]:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or str(path) != value
        or "\\" in value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("legacy object path must be a confined POSIX relative path")
    return path.parts


def _open_verified(
    roots: Mapping[str, _ConfiguredRoot],
    ref: LegacyFileRef,
    *,
    logical_digest: object | None = None,
) -> _VerifiedFile:
    parts = _relative_parts(ref.relative_path)
    try:
        root = roots[ref.root_id]
        descriptor = os.open(
            root.path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        root_info = os.fstat(descriptor)
        if (root_info.st_dev, root_info.st_ino) != (root.device, root.inode):
            os.close(descriptor)
            raise LegacyRecordingError("configured legacy storage root was replaced")
        for part in parts[:-1]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        file_descriptor = os.open(
            parts[-1], os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=descriptor
        )
        os.close(descriptor)
    except (KeyError, OSError) as error:
        try:
            os.close(descriptor)
        except (UnboundLocalError, OSError):
            pass
        raise LegacyRecordingError(
            "legacy object is missing or violates confinement"
        ) from error

    try:
        info = os.fstat(file_descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size != ref.byte_count:
            raise LegacyRecordingError("legacy object type or size differs")
        digest = hashlib.sha256()
        logical = cast("_DigestUpdater | None", logical_digest)
        offset = 0
        while offset < ref.byte_count:
            block = os.pread(
                file_descriptor, min(1024 * 1024, ref.byte_count - offset), offset
            )
            if not block:
                raise LegacyRecordingError(
                    "legacy object was truncated during verification"
                )
            digest.update(block)
            if logical is not None:
                logical.update(block)
            offset += len(block)
        after = os.fstat(file_descriptor)
        if _fingerprint(info) != _fingerprint(after):
            raise LegacyRecordingError("legacy object changed during verification")
        if digest.hexdigest() != ref.digest.value:
            raise LegacyRecordingError("legacy object SHA-256 differs")
        return _VerifiedFile(file_descriptor, ref, _fingerprint(after))
    except BaseException:
        os.close(file_descriptor)
        raise


def _fingerprint(value: os.stat_result) -> _Fingerprint:
    return _Fingerprint(
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


class _DigestUpdater(Protocol):
    def update(self, value: bytes) -> None: ...


def _decode_source_manifest(data: bytes) -> dict[str, object]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LegacyRecordingError("legacy manifest is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise LegacyRecordingError("legacy manifest must be a JSON object")
    return cast(dict[str, object], value)


def _validate_source(
    source: dict[str, object], registration: LegacyRecordingRegistration
) -> None:
    if source.get("schema") != "leo-tracker.beacon-iq/v1":
        raise UnsupportedLegacyRecordingError("unsupported legacy manifest schema")
    if source.get("state") != "complete":
        raise UnsupportedLegacyRecordingError("legacy recording is not complete")
    if source.get("dtype") != "ci16_le" or source.get("layout") != (
        "sample,receiver,component; receivers=rx0,rx1; components=i,q"
    ):
        raise UnsupportedLegacyRecordingError("unsupported legacy dtype or layout")
    if source.get("receiver_count") != 2:
        raise UnsupportedLegacyRecordingError(
            "only paired-receiver legacy IQ is supported"
        )
    _validate_omitted_survey(source, registration)
    chunks = source.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        raise UnsupportedLegacyRecordingError("legacy manifest has no chunk stream")
    if len(chunks) != len(registration.payloads):
        raise LegacyRecordingError(
            "registered payload count differs from legacy chunks"
        )

    source_parent = PurePosixPath(registration.source_manifest.relative_path).parent
    next_sample = 0
    total_bytes = 0
    for raw, payload in zip(chunks, registration.payloads, strict=True):
        if not isinstance(raw, dict):
            raise LegacyRecordingError("legacy chunk entry is not an object")
        path = raw.get("path")
        first = raw.get("first_sample_index")
        samples = raw.get("sample_count")
        byte_count = raw.get("bytes")
        sha256 = raw.get("sha256")
        if (
            not isinstance(path, str)
            or isinstance(first, bool)
            or not isinstance(first, int)
            or isinstance(samples, bool)
            or not isinstance(samples, int)
            or samples <= 0
            or isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
        ):
            raise LegacyRecordingError("legacy chunk declaration is malformed")
        _relative_parts(path)
        expected_path = str(source_parent / PurePosixPath(path))
        if (
            payload.root_id != registration.source_manifest.root_id
            or payload.relative_path != expected_path
            or first != next_sample
            or byte_count != samples * 8
            or byte_count != payload.byte_count
            or sha256 != payload.digest.value
        ):
            raise LegacyRecordingError(
                "legacy chunk order, path, size, or digest differs"
            )
        next_sample += samples
        total_bytes += byte_count

    segment = registration.manifest.segments[0]
    requested = segment.requested
    legacy_gain_mode = source.get("gain_mode")
    if legacy_gain_mode == "manual":
        expected_gain_mode = GainMode.MANUAL
    elif legacy_gain_mode in {"slow_attack", "fast_attack"}:
        expected_gain_mode = GainMode.AGC
    else:
        raise UnsupportedLegacyRecordingError("unsupported legacy gain mode")
    source_gain = source.get("configured_gain_db")
    checks = (
        segment.sample_count == next_sample,
        segment.shape == (next_sample, 2, 2),
        requested.receiver_chain_ids == registration.manifest.receiver_chain_ids,
        segment.actual_sample_rate_hz == source.get("sample_rate_hz"),
        requested.sample_rate_hz == source.get("sample_rate_hz"),
        segment.actual_center_frequency_hz == source.get("center_frequency_hz"),
        requested.center_frequency_hz == source.get("center_frequency_hz"),
        segment.actual_bandwidth_hz == source.get("bandwidth_hz"),
        requested.bandwidth_hz == source.get("bandwidth_hz"),
        segment.actual_gain.mode is expected_gain_mode,
        requested.gain.mode is expected_gain_mode,
        segment.actual_gain.gain_db == source_gain,
        requested.gain.gain_db == source_gain,
        registration.manifest.created_utc_ns == source.get("created_utc_ns"),
        total_bytes == registration.recording_ref.data_object.byte_count,
    )
    if not all(checks):
        raise LegacyRecordingError(
            "registered redux facts differ from legacy declarations"
        )


def _validate_omitted_survey(
    source: dict[str, object], registration: LegacyRecordingRegistration
) -> None:
    raw = source.get("survey_iq")
    omitted = registration.omitted_survey
    if raw is None:
        if omitted is not None:
            raise LegacyRecordingError("omitted survey is not declared by source")
        return
    if omitted is None:
        raise UnsupportedLegacyRecordingError(
            "declared survey must be explicitly registered as omitted"
        )
    if not isinstance(raw, dict):
        raise LegacyRecordingError("legacy survey declaration is malformed")
    path = raw.get("path")
    byte_count = raw.get("bytes")
    sha256 = raw.get("sha256")
    if not isinstance(path, str):
        raise LegacyRecordingError("legacy survey path is malformed")
    _relative_parts(path)
    expected_path = str(
        PurePosixPath(registration.source_manifest.relative_path).parent
        / PurePosixPath(path)
    )
    if (
        omitted.root_id != registration.source_manifest.root_id
        or omitted.relative_path != expected_path
        or byte_count != omitted.byte_count
        or sha256 != omitted.digest.value
    ):
        raise LegacyRecordingError("omitted survey path, size, or digest differs")
