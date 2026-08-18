"""Fail-closed import of the one frozen RETRO QAM recording.

This component translates independently pinned historical facts into public
Redux contracts.  The archive path is an operator input at this boundary; it
is never persisted as a recording locator or exposed to downstream analysis.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Protocol, cast

from leo_flow.contracts.capture import (
    ActivityKind,
    ActivityManifest,
    ActivityRequest,
    CapturePlan,
    CompletedLocalRecording,
    GainMode,
    GainSetting,
    RecordingManifest,
    SegmentManifest,
    SegmentRequest,
)
from leo_flow.contracts.core import (
    ActivityId,
    Digest,
    DigestAlgorithm,
    HardwareSnapshotId,
    PlanId,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    StationId,
    UtcNs,
)
from leo_flow.contracts.hardware import HardwareMetadataSnapshot, ReceiverChainMetadata
from leo_flow.contracts.ports import RecordingPublisher
from leo_flow.contracts.storage import PublishedRecordingRef
from leo_flow.storage.ports import RecordingWriter

_SCHEMA = "org.leo-flow.external-retro-qam-corpus/v1"
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_ARCHIVE_JSON_BYTES = 16 * 1024 * 1024
_READ_SIZE = 8 * 1024 * 1024
_RECORDING_ID = RecordingId("rec_retro_qam_20260813_clip002")
_PLAN_ID = PlanId("plan_retro_qam_20260813_clip002_import")
_ACTIVITY_ID = ActivityId("act_retro_qam_historical_test")
_SEGMENT_ID = SegmentId("seg_retro_qam_clip002")
_STATION_ID = StationId("station_historical_unattributed")
_RADIO_ID = RadioId("radio_historical_pluto_5d4d")
_RECEIVERS = (
    ReceiverChainId("rx_retro_qam_0"),
    ReceiverChainId("rx_retro_qam_1"),
)
_HARDWARE_ID = HardwareSnapshotId("hw_historical_pluto_5d4d_unqualified")


class RetroQamCorpusError(RuntimeError):
    """Pinned corpus bytes or historical facts failed verification."""


@dataclass(frozen=True)
class RetroQamImportSpecification:
    corpus_manifest: Path
    archive_root: Path
    expected_manifest_digest: Digest

    def __post_init__(self) -> None:
        if self.expected_manifest_digest.algorithm is not DigestAlgorithm.SHA256:
            raise ValueError("RETRO corpus manifest identity must use SHA-256")


@dataclass(frozen=True)
class PreparedRetroQamRecording:
    plan: CapturePlan
    manifest: RecordingManifest
    hardware_snapshot: HardwareMetadataSnapshot
    source_iq: Path
    source_iq_digest: Digest
    source_iq_bytes: int
    selected_window_start_sample: int
    selected_window_sample_count: int
    selected_window_digest: Digest
    corpus_manifest_digest: Digest

    @property
    def publication_idempotency_key(self) -> str:
        return f"retro-qam-import:{self.corpus_manifest_digest.value}"


class _WriteSession(Protocol):
    def append_iq(self, segment_id: SegmentId, ci16_bytes: bytes) -> None: ...

    def finish_segment(self, segment: SegmentManifest) -> None: ...

    def finalize(self, manifest: RecordingManifest) -> CompletedLocalRecording: ...

    def abort(self, reason: str) -> None: ...


def prepare_retro_qam_recording(
    specification: RetroQamImportSpecification,
) -> PreparedRetroQamRecording:
    """Verify every pinned object and construct truthful public contracts."""

    manifest_bytes = _read_bounded_regular(
        specification.corpus_manifest, _MAX_MANIFEST_BYTES
    )
    observed_manifest_digest = Digest.sha256(manifest_bytes)
    if observed_manifest_digest != specification.expected_manifest_digest:
        raise RetroQamCorpusError("RETRO corpus manifest digest differs")
    document = _json_mapping(manifest_bytes, "corpus manifest")
    if document.get("schema") != _SCHEMA:
        raise RetroQamCorpusError("unsupported RETRO corpus manifest schema")

    root = _verified_root(specification.archive_root)
    archive = _mapping(document.get("archive"), "archive")
    checksum_path = _confined_regular(root, "SHA256SUMS")
    checksum_digest = _digest_file(checksum_path)
    if checksum_digest != _sha256_digest(archive.get("sha256sums_sha256")):
        raise RetroQamCorpusError("RETRO SHA256SUMS digest differs")

    expected_objects = _expected_archive_objects(document)
    listed_objects = _parse_sha256sums(
        _read_bounded_regular(checksum_path, _MAX_MANIFEST_BYTES)
    )
    if listed_objects != expected_objects:
        raise RetroQamCorpusError("RETRO archive inventory differs from manifest")
    verified_paths: dict[str, Path] = {}
    for relative_path, expected_digest in expected_objects.items():
        path = _confined_regular(root, relative_path)
        if _digest_file(path) != expected_digest:
            raise RetroQamCorpusError(
                f"RETRO archive object digest differs: {relative_path}"
            )
        verified_paths[relative_path] = path

    iq = _mapping(document.get("iq_object"), "IQ object")
    fmt = _mapping(document.get("format"), "format")
    window = _mapping(document.get("selected_window"), "selected window")
    iq_relative = _string(iq.get("relative_path"), "IQ relative path")
    try:
        iq_path = verified_paths[iq_relative]
    except KeyError as error:
        raise RetroQamCorpusError("IQ object is absent from pinned inventory") from error
    iq_digest = _sha256_digest(iq.get("sha256"))
    if expected_objects[iq_relative] != iq_digest:
        raise RetroQamCorpusError("IQ identities differ inside corpus manifest")
    iq_bytes, iq_samples, bytes_per_sample = _validate_ci16_geometry(iq, fmt, window)
    if _file_size(iq_path) != iq_bytes:
        raise RetroQamCorpusError("RETRO IQ byte count differs")
    selected_start = _integer(window.get("sample_offset"), "window sample offset")
    selected_count = _integer(window.get("sample_count"), "window sample count")
    selected_digest = _sha256_digest(window.get("sha256"))
    with _open_stable(iq_path) as stream:
        stream.seek(selected_start * bytes_per_sample)
        selected = stream.read(selected_count * bytes_per_sample)
    if len(selected) != selected_count * bytes_per_sample:
        raise RetroQamCorpusError("RETRO selected window is truncated")
    if Digest.sha256(selected) != selected_digest:
        raise RetroQamCorpusError("RETRO selected-window digest differs")

    evidence = _json_from_verified(verified_paths, "provenance/manifest.json")
    source = _json_from_verified(verified_paths, "provenance/source-manifest.json")
    facts = _validate_historical_facts(document, evidence, source, iq, fmt)
    plan, recording_manifest, hardware = _contracts(
        document,
        facts,
        iq_samples,
        selected_start,
        selected_count,
        selected_digest,
        observed_manifest_digest,
        iq_digest,
    )
    return PreparedRetroQamRecording(
        plan,
        recording_manifest,
        hardware,
        iq_path,
        iq_digest,
        iq_bytes,
        selected_start,
        selected_count,
        selected_digest,
        observed_manifest_digest,
    )


def import_retro_qam_recording(
    prepared: PreparedRetroQamRecording,
    writer: RecordingWriter,
    publisher: RecordingPublisher,
    *,
    destination: str,
) -> tuple[PublishedRecordingRef, CompletedLocalRecording]:
    """Materialize the verified object and atomically publish its public pair."""

    session = cast(
        _WriteSession,
        writer.begin(
            prepared.manifest.recording_id,
            prepared.plan,
            prepared.manifest.hardware_metadata_snapshot_id,
            destination,
        ),
    )
    hasher = hashlib.sha256()
    byte_count = 0
    try:
        with _open_stable(prepared.source_iq) as source:
            while block := source.read(_READ_SIZE):
                hasher.update(block)
                byte_count += len(block)
                session.append_iq(_SEGMENT_ID, block)
        if byte_count != prepared.source_iq_bytes:
            raise RetroQamCorpusError("RETRO IQ changed length during import")
        if hasher.hexdigest() != prepared.source_iq_digest.value:
            raise RetroQamCorpusError("RETRO IQ changed content during import")
        session.finish_segment(prepared.manifest.segments[0])
        completed = session.finalize(prepared.manifest)
    except Exception as error:
        session.abort(f"{type(error).__name__}: {error}")
        raise
    if (
        completed.data_object.digest != prepared.source_iq_digest
        or completed.data_object.byte_count != prepared.source_iq_bytes
    ):
        raise RetroQamCorpusError("materialized recording differs from pinned IQ")
    published = publisher.publish(
        completed, idempotency_key=prepared.publication_idempotency_key
    )
    if published.recording_object.recording_id != prepared.manifest.recording_id:
        raise RetroQamCorpusError("publisher substituted a recording identity")
    return published, completed


@dataclass(frozen=True)
class _HistoricalFacts:
    original_recording_id: str
    radio_serial: str
    first_utc_ns: int
    stop_utc_ns: int
    created_utc_ns: int
    source_completed_utc_ns: int
    center_frequency_hz: float
    sample_rate_hz: float
    bandwidth_hz: float
    gain_db: float
    lnb_lo_hz: float
    utc_uncertainty_s: float
    original_sample_offset: int


def _validate_historical_facts(
    document: Mapping[str, Any],
    evidence: Mapping[str, Any],
    source: Mapping[str, Any],
    iq: Mapping[str, Any],
    fmt: Mapping[str, Any],
) -> _HistoricalFacts:
    original_id = _string(document.get("recording_id"), "recording_id")
    if evidence.get("recording_id") != original_id:
        raise RetroQamCorpusError("historical recording identities differ")
    source_ref = _mapping(evidence.get("source"), "evidence source")
    if source_ref.get("recording_id") != original_id:
        raise RetroQamCorpusError("source evidence recording identity differs")
    clips = evidence.get("clips")
    if not isinstance(clips, list):
        raise RetroQamCorpusError("evidence clips must be a list")
    matches = [item for item in clips if isinstance(item, dict) and item.get("interval_id") == "clip-002"]
    if len(matches) != 1:
        raise RetroQamCorpusError("evidence must contain one clip-002")
    clip = cast(dict[str, Any], matches[0])
    identity = _mapping(source.get("identity"), "source identity")
    radio = _mapping(evidence.get("radio_parameters"), "radio parameters")
    comparisons = (
        (clip.get("bytes"), iq.get("byte_count"), "clip byte count"),
        (clip.get("sample_count"), iq.get("sample_count"), "clip sample count"),
        (clip.get("sha256"), iq.get("sha256"), "clip digest"),
        (clip.get("receiver_count"), fmt.get("receiver_count"), "receiver count"),
        (source.get("sample_rate_hz"), fmt.get("sample_rate_hz"), "sample rate"),
        (source.get("center_frequency_hz"), radio.get("center_frequency_hz"), "center frequency"),
        (source.get("bandwidth_hz"), radio.get("bandwidth_hz"), "bandwidth"),
        (source.get("configured_gain_db"), radio.get("configured_gain_db"), "gain"),
    )
    for actual, expected, label in comparisons:
        if actual != expected:
            raise RetroQamCorpusError(f"historical {label} differs")
    if (
        clip.get("dtype") != "ci16_le"
        or source.get("dtype") != "ci16_le"
        or source.get("gain_mode") != "manual"
        or clip.get("layout") != source.get("layout")
    ):
        raise RetroQamCorpusError("historical representation differs")
    return _HistoricalFacts(
        original_id,
        _string(identity.get("serial"), "historical radio serial"),
        _integer(clip.get("first_utc_ns"), "clip first UTC"),
        _integer(clip.get("stop_utc_ns"), "clip stop UTC"),
        _integer(source.get("created_utc_ns"), "source created UTC"),
        _integer(source.get("completed_utc_ns"), "source completed UTC"),
        _number(source.get("center_frequency_hz"), "center frequency"),
        _number(source.get("sample_rate_hz"), "sample rate"),
        _number(source.get("bandwidth_hz"), "bandwidth"),
        _number(source.get("configured_gain_db"), "gain"),
        _number(source.get("lnb_lo_hz"), "historical LNB LO"),
        _number(clip.get("utc_uncertainty_s"), "UTC uncertainty"),
        _integer(clip.get("first_sample"), "historical first sample"),
    )


def _contracts(
    document: Mapping[str, Any],
    facts: _HistoricalFacts,
    sample_count: int,
    selected_start: int,
    selected_count: int,
    selected_digest: Digest,
    corpus_digest: Digest,
    iq_digest: Digest,
) -> tuple[CapturePlan, RecordingManifest, HardwareMetadataSnapshot]:
    gain = GainSetting(GainMode.MANUAL, facts.gain_db)
    request = SegmentRequest.create(
        segment_id=_SEGMENT_ID,
        center_frequency_hz=facts.center_frequency_hz,
        sample_rate_hz=facts.sample_rate_hz,
        bandwidth_hz=facts.bandwidth_hz,
        receiver_chain_ids=_RECEIVERS,
        gain=gain,
        sample_count=sample_count,
        scheduled_utc_ns=UtcNs(facts.first_utc_ns),
        hardware_controls={"historical_source_only": True},
        tags={
            "edge": "lower",
            "historical_conditioned_canary": True,
            "original_recording_id": facts.original_recording_id,
        },
    )
    activity = ActivityRequest(_ACTIVITY_ID, ActivityKind.TEST, (request,))
    tags = (
        ("calibrated_detection", False),
        ("calibration_eligible", False),
        ("conditioned_canary", True),
        ("historical_capture", True),
        ("known_published_pilot", True),
        ("original_recording_id", facts.original_recording_id),
        ("payload_decoded", False),
        ("retro_corpus_manifest_digest", str(corpus_digest)),
        ("retro_iq_digest", str(iq_digest)),
        ("selected_window_digest", str(selected_digest)),
        ("selected_window_sample_count", selected_count),
        ("selected_window_start_sample", selected_start),
    )
    plan = CapturePlan(
        SchemaRef(CapturePlan.SCHEMA_ID),
        _PLAN_ID,
        _RADIO_ID,
        _RECEIVERS,
        (activity,),
        tags,
    )
    segment = SegmentManifest(
        _SEGMENT_ID,
        request,
        facts.center_frequency_hz,
        facts.sample_rate_hz,
        facts.bandwidth_hz,
        gain,
        UtcNs(facts.first_utc_ns),
        0,
        sample_count,
        (sample_count, 2, 2),
        (
            ("continuity_verified", False),
            ("historical_original_sample_offset", facts.original_sample_offset),
            ("historical_utc_mapping", "iio_read_midpoint_interpolation"),
            ("historical_utc_uncertainty_s", facts.utc_uncertainty_s),
        ),
    )
    activity_manifest = ActivityManifest(
        _ACTIVITY_ID,
        ActivityKind.TEST,
        UtcNs(facts.first_utc_ns),
        UtcNs(facts.stop_utc_ns),
        (_SEGMENT_ID,),
    )
    manifest = RecordingManifest(
        SchemaRef(RecordingManifest.SCHEMA_ID),
        _RECORDING_ID,
        UtcNs(facts.created_utc_ns),
        UtcNs(facts.first_utc_ns),
        UtcNs(facts.stop_utc_ns),
        _STATION_ID,
        _RADIO_ID,
        facts.radio_serial,
        _RECEIVERS,
        "historical-host-bracket-uncertain",
        _HARDWARE_ID,
        (activity_manifest,),
        (segment,),
        _PLAN_ID,
        "leo-retro-qam-recording-import/0.1.0",
        tags,
    )
    hardware = HardwareMetadataSnapshot(
        SchemaRef(HardwareMetadataSnapshot.SCHEMA_ID),
        _HARDWARE_ID,
        _STATION_ID,
        (_RADIO_ID,),
        tuple(
            ReceiverChainMetadata(
                receiver,
                _RADIO_ID,
                index,
                f"historical-unqualified-port-{index}",
                None,
                None,
                UtcNs(facts.created_utc_ns),
                UtcNs(facts.source_completed_utc_ns),
            )
            for index, receiver in enumerate(_RECEIVERS)
        ),
    )
    scope = _mapping(document.get("scope"), "scope")
    if scope != {
        "calibrated_detection": False,
        "known_published_pilot": True,
        "payload_decoded": False,
    }:
        raise RetroQamCorpusError("RETRO scope labels differ")
    return plan, manifest, hardware


def _validate_ci16_geometry(
    iq: Mapping[str, Any], fmt: Mapping[str, Any], window: Mapping[str, Any]
) -> tuple[int, int, int]:
    if (
        fmt.get("component_dtype") != "int16"
        or fmt.get("byte_order") != "little"
        or fmt.get("component_order") != ["i", "q"]
        or fmt.get("layout") != ["sample", "receiver", "component"]
        or fmt.get("receiver_count") != 2
    ):
        raise RetroQamCorpusError("unsupported RETRO CI16 geometry")
    iq_bytes = _integer(iq.get("byte_count"), "IQ byte count")
    iq_samples = _integer(iq.get("sample_count"), "IQ sample count")
    bytes_per_sample = _integer(iq.get("bytes_per_sample"), "bytes per sample")
    if bytes_per_sample != 2 * 2 * 2 or iq_bytes != iq_samples * bytes_per_sample:
        raise RetroQamCorpusError("RETRO IQ geometry is inconsistent")
    window_start = _integer(window.get("sample_offset"), "window sample offset")
    window_count = _integer(window.get("sample_count"), "window sample count")
    window_byte_offset = _integer(window.get("byte_offset"), "window byte offset")
    window_bytes = _integer(window.get("byte_count"), "window byte count")
    if (
        window_start < 0
        or window_count <= 0
        or window_start + window_count > iq_samples
        or window_byte_offset != window_start * bytes_per_sample
        or window_bytes != window_count * bytes_per_sample
    ):
        raise RetroQamCorpusError("RETRO selected-window geometry is inconsistent")
    return iq_bytes, iq_samples, bytes_per_sample


def _expected_archive_objects(document: Mapping[str, Any]) -> dict[str, Digest]:
    raw = document.get("archive_objects")
    if not isinstance(raw, list) or not raw:
        raise RetroQamCorpusError("RETRO archive inventory is empty")
    result: dict[str, Digest] = {}
    for value in raw:
        item = _mapping(value, "archive object")
        relative = _string(item.get("relative_path"), "archive relative path")
        _relative_parts(relative)
        if relative in result:
            raise RetroQamCorpusError("RETRO archive inventory has duplicates")
        result[relative] = _sha256_digest(item.get("sha256"))
    return result


def _parse_sha256sums(payload: bytes) -> dict[str, Digest]:
    try:
        lines = payload.decode("ascii").splitlines()
    except UnicodeDecodeError as error:
        raise RetroQamCorpusError("RETRO SHA256SUMS is not ASCII") from error
    result: dict[str, Digest] = {}
    for line in lines:
        digest, separator, relative = line.partition("  ")
        if not separator or not relative or relative in result:
            raise RetroQamCorpusError("RETRO SHA256SUMS syntax is invalid")
        _relative_parts(relative)
        result[relative] = _sha256_digest(digest)
    return result


def _verified_root(path: Path) -> Path:
    supplied = Path(path)
    if not supplied.is_absolute() or supplied.is_symlink():
        raise RetroQamCorpusError("RETRO archive root must be absolute and non-symlink")
    try:
        root = supplied.resolve(strict=True)
    except OSError as error:
        raise RetroQamCorpusError("RETRO archive root is unavailable") from error
    if not root.is_dir():
        raise RetroQamCorpusError("RETRO archive root is not a directory")
    return root


def _relative_parts(relative: str) -> tuple[str, ...]:
    value = PurePosixPath(relative)
    if value.is_absolute() or not value.parts or any(part in {"", ".", ".."} for part in value.parts):
        raise RetroQamCorpusError("RETRO archive path is not confined")
    return value.parts


def _confined_regular(root: Path, relative: str) -> Path:
    parts = _relative_parts(relative)
    path = root.joinpath(*parts)
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise RetroQamCorpusError("RETRO archive entries cannot be symlinks")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as error:
        raise RetroQamCorpusError("RETRO archive path escapes or is absent") from error
    if not resolved.is_file():
        raise RetroQamCorpusError("RETRO archive object is not a regular file")
    return resolved


class _StableFile:
    def __init__(self, path: Path):
        self._path = path
        self.stream: BinaryIO | None = None
        self.identity: tuple[int, int, int, int, int] | None = None

    def __enter__(self) -> BinaryIO:
        descriptor = os.open(
            self._path,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        self.stream = os.fdopen(descriptor, "rb", buffering=0)
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            self.stream.close()
            raise RetroQamCorpusError("RETRO archive object is not regular")
        self.identity = _file_identity(details)
        return self.stream

    def __exit__(self, *_: object) -> None:
        assert self.stream is not None and self.identity is not None
        try:
            if _file_identity(os.fstat(self.stream.fileno())) != self.identity:
                raise RetroQamCorpusError("RETRO archive object changed while open")
        finally:
            self.stream.close()


def _open_stable(path: Path) -> _StableFile:
    return _StableFile(path)


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _file_size(path: Path) -> int:
    with _open_stable(path) as stream:
        return os.fstat(stream.fileno()).st_size


def _digest_file(path: Path) -> Digest:
    digest = hashlib.sha256()
    with _open_stable(path) as stream:
        while block := stream.read(_READ_SIZE):
            digest.update(block)
    return Digest(DigestAlgorithm.SHA256, digest.hexdigest())


def _read_bounded_regular(path: Path, maximum_bytes: int) -> bytes:
    with _open_stable(path) as stream:
        value = stream.read(maximum_bytes + 1)
    if not value or len(value) > maximum_bytes:
        raise RetroQamCorpusError("RETRO manifest object size is invalid")
    return value


def _json_from_verified(paths: Mapping[str, Path], relative: str) -> dict[str, Any]:
    try:
        return _json_mapping(
            _read_bounded_regular(paths[relative], _MAX_ARCHIVE_JSON_BYTES), relative
        )
    except KeyError as error:
        raise RetroQamCorpusError(f"required archive object is absent: {relative}") from error


def _json_mapping(payload: bytes, label: str) -> dict[str, Any]:
    try:
        return _mapping(json.loads(payload), label)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RetroQamCorpusError(f"{label} is not valid JSON") from error


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RetroQamCorpusError(f"{label} must be an object")
    return cast(dict[str, Any], value)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RetroQamCorpusError(f"{label} must be a non-empty string")
    return value


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise RetroQamCorpusError(f"{label} must be an integer")
    return value


def _number(value: object, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise RetroQamCorpusError(f"{label} must be a number")
    return float(value)


def _sha256_digest(value: object) -> Digest:
    try:
        return Digest(DigestAlgorithm.SHA256, _string(value, "SHA-256 digest"))
    except ValueError as error:
        raise RetroQamCorpusError("SHA-256 digest is invalid") from error
