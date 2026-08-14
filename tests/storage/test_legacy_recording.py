from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from leo_flow.contracts.core import Digest, SegmentId, canonical_digest
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.legacy_recording import (
    LegacyFileRef,
    LegacyRecordingError,
    LegacyRecordingReader,
    LegacyRecordingRegistration,
    UnsupportedLegacyRecordingError,
    legacy_selected_chunk_index_digest,
)
from leo_flow.storage.recording_codec import UnverifiedContinuityError
from testkit import recording_manifest


def _file_ref(root: Path, relative_path: str) -> LegacyFileRef:
    data = (root / relative_path).read_bytes()
    return LegacyFileRef("archive", relative_path, Digest.sha256(data), len(data))


def _fixture(
    tmp_path: Path,
    *,
    source_change: dict[str, object] | None = None,
    include_survey: bool = False,
    register_survey: bool = False,
    logical_digest: Digest | None = None,
) -> tuple[LegacyRecordingReader, LegacyRecordingRegistration, bytes, Path]:
    root = tmp_path / "archive"
    capture = root / "captures" / "old-one"
    capture.mkdir(parents=True)
    first = bytes(range(24))
    second = bytes(range(24, 64))
    (capture / "chunk-000000.ci16").write_bytes(first)
    (capture / "chunk-000001.ci16").write_bytes(second)
    source: dict[str, object] = {
        "schema": "leo-tracker.beacon-iq/v1",
        "state": "complete",
        "dtype": "ci16_le",
        "layout": "sample,receiver,component; receivers=rx0,rx1; components=i,q",
        "sample_rate_hz": 2_500_000.0,
        "bandwidth_hz": 2_500_000.0,
        "center_frequency_hz": 11_325_000_000.0,
        "receiver_count": 2,
        "gain_mode": "manual",
        "configured_gain_db": 50.0,
        "created_utc_ns": 1_700_000_000_000_000_000,
        "stored_bytes": 64,
        "chunks": [
            {
                "path": "chunk-000000.ci16",
                "first_sample_index": 0,
                "sample_count": 3,
                "bytes": len(first),
                "sha256": hashlib.sha256(first).hexdigest(),
            },
            {
                "path": "chunk-000001.ci16",
                "first_sample_index": 3,
                "sample_count": 5,
                "bytes": len(second),
                "sha256": hashlib.sha256(second).hexdigest(),
            },
        ],
    }
    survey_ref = None
    if include_survey:
        survey = b"survey-iq-exact"
        (capture / "survey.ci16").write_bytes(survey)
        source["survey_iq"] = {
            "path": "survey.ci16",
            "bytes": len(survey),
            "sha256": hashlib.sha256(survey).hexdigest(),
            "dtype": "ci16_le",
            "layout": "tuning,sample,receiver,component",
        }
        if register_survey:
            survey_ref = _file_ref(root, "captures/old-one/survey.ci16")
    if source_change:
        source.update(source_change)
    manifest_bytes = json.dumps(source, sort_keys=True).encode()
    (capture / "manifest.json").write_bytes(manifest_bytes)

    payloads = (
        _file_ref(root, "captures/old-one/chunk-000000.ci16"),
        _file_ref(root, "captures/old-one/chunk-000001.ci16"),
    )
    source_ref = _file_ref(root, "captures/old-one/manifest.json")
    manifest = recording_manifest()
    data = first + second
    data_ref = ObjectRef(
        logical_digest or Digest.sha256(data),
        len(data),
        "application/octet-stream",
        "legacy-ci16-chunk-set-v1",
        "legacy-registration:data",
    )
    metadata_ref = ObjectRef(
        source_ref.digest,
        source_ref.byte_count,
        "application/json",
        "legacy-beacon-manifest-v1",
        "legacy-registration:manifest",
    )
    recording_ref = RecordingObjectRef(
        manifest.recording_id,
        data_ref,
        metadata_ref,
        canonical_digest(manifest),
    )
    registration = LegacyRecordingRegistration(
        recording_ref, manifest, source_ref, payloads, survey_ref
    )
    return (
        LegacyRecordingReader({"archive": root}, (registration,)),
        registration,
        data,
        root,
    )


def test_read_in_place_crosses_chunks_and_exposes_existing_view(tmp_path: Path) -> None:
    reader, registration, data, root = _fixture(
        tmp_path, include_survey=True, register_survey=True
    )
    before = sorted(path.relative_to(root) for path in root.rglob("*"))
    segment_id = registration.manifest.segments[0].segment_id

    with reader.open(registration.recording_ref) as view:
        assert view.manifest == registration.manifest
        assert view.read_iq_bytes(segment_id, 2, 6) == data[16:48]
        assert view.continuity(segment_id) is None
        with pytest.raises(UnverifiedContinuityError, match="no metadata-verified"):
            tuple(view.iter_safe_windows(segment_id, 2, 1))

    assert sorted(path.relative_to(root) for path in root.rglob("*")) == before
    assert legacy_selected_chunk_index_digest(registration.payloads) != (
        registration.recording_ref.data_object.digest
    )


def test_survey_must_be_an_exact_explicit_omission(tmp_path: Path) -> None:
    reader, registration, _, _ = _fixture(tmp_path, include_survey=True)
    with (
        pytest.raises(UnsupportedLegacyRecordingError, match="explicitly registered"),
        reader.open(registration.recording_ref),
    ):
        pass


def test_explicitly_omitted_survey_is_still_verified(tmp_path: Path) -> None:
    reader, registration, _, root = _fixture(
        tmp_path, include_survey=True, register_survey=True
    )
    assert registration.omitted_survey is not None
    survey = root / registration.omitted_survey.relative_path
    survey.write_bytes(b"x" * registration.omitted_survey.byte_count)
    with (
        pytest.raises(LegacyRecordingError, match="SHA-256 differs"),
        reader.open(registration.recording_ref),
    ):
        pass


def test_logical_digest_is_concatenated_content_not_chunk_index(tmp_path: Path) -> None:
    reader, registration, _, _ = _fixture(
        tmp_path, logical_digest=Digest.sha256(b"substituted logical bytes")
    )
    with (
        pytest.raises(LegacyRecordingError, match="logical dwell byte-stream"),
        reader.open(registration.recording_ref),
    ):
        pass


def test_manifest_size_limit_is_checked_before_open_or_allocation(
    tmp_path: Path,
) -> None:
    _, registration, _, root = _fixture(tmp_path)
    oversized = 16 * 1024 * 1024 + 1
    source_ref = replace(registration.source_manifest, byte_count=oversized)
    metadata_ref = replace(
        registration.recording_ref.metadata_object, byte_count=oversized
    )
    changed = replace(
        registration,
        source_manifest=source_ref,
        recording_ref=replace(registration.recording_ref, metadata_object=metadata_ref),
    )
    reader = LegacyRecordingReader({"archive": root}, (changed,))
    with (
        pytest.raises(UnsupportedLegacyRecordingError, match="size limit"),
        reader.open(changed.recording_ref),
    ):
        pass


def test_payload_corruption_and_declared_source_substitution_fail(
    tmp_path: Path,
) -> None:
    reader, registration, _, root = _fixture(tmp_path)
    chunk = root / registration.payloads[0].relative_path
    chunk.write_bytes(b"x" * registration.payloads[0].byte_count)
    with (
        pytest.raises(LegacyRecordingError, match="SHA-256 differs"),
        reader.open(registration.recording_ref),
    ):
        pass

    other = tmp_path / "other"
    reader, registration, _, root = _fixture(other)
    manifest_path = root / registration.source_manifest.relative_path
    manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
    with (
        pytest.raises(LegacyRecordingError, match="size differs"),
        reader.open(registration.recording_ref),
    ):
        pass


def test_unknown_reference_and_fact_mismatch_fail_closed(tmp_path: Path) -> None:
    reader, registration, _, _ = _fixture(tmp_path)
    wrong_ref = replace(
        registration.recording_ref,
        data_object=replace(
            registration.recording_ref.data_object,
            digest=Digest.sha256(b"another object"),
        ),
    )
    with (
        pytest.raises(LegacyRecordingError, match="not registered"),
        reader.open(wrong_ref),
    ):
        pass


def test_unknown_gain_mode_fails_instead_of_becoming_agc(tmp_path: Path) -> None:
    reader, registration, _, _ = _fixture(
        tmp_path, source_change={"gain_mode": "mystery-controller"}
    )
    with (
        pytest.raises(UnsupportedLegacyRecordingError, match="gain mode"),
        reader.open(registration.recording_ref),
    ):
        pass


@pytest.mark.parametrize(
    "source_change",
    (
        {"dtype": "complex64"},
        {"layout": "receiver,sample,component"},
        {"receiver_count": 1},
    ),
)
def test_unsupported_representation_fails_closed(
    tmp_path: Path, source_change: dict[str, object]
) -> None:
    reader, registration, _, _ = _fixture(tmp_path, source_change=source_change)
    with (
        pytest.raises(UnsupportedLegacyRecordingError, match="unsupported|paired"),
        reader.open(registration.recording_ref),
    ):
        pass


def test_registration_refuses_multi_segment_mapping(tmp_path: Path) -> None:
    _, registration, _, _ = _fixture(tmp_path)
    first = registration.manifest.segments[0]
    second_id = SegmentId("seg_02")
    second_request = replace(first.requested, segment_id=second_id, sample_count=1)
    second = replace(
        first,
        segment_id=second_id,
        requested=second_request,
        sample_count=1,
        shape=(1, 2, 2),
    )
    activity = replace(
        registration.manifest.activities[0],
        segment_ids=(first.segment_id, second_id),
    )
    manifest = replace(
        registration.manifest,
        activities=(activity,),
        segments=(first, second),
    )
    recording_ref = replace(
        registration.recording_ref, manifest_digest=canonical_digest(manifest)
    )
    with pytest.raises(UnsupportedLegacyRecordingError, match="exactly one"):
        LegacyRecordingRegistration(
            recording_ref,
            manifest,
            registration.source_manifest,
            registration.payloads,
        )

    other = tmp_path / "rate"
    reader, registration, _, _ = _fixture(
        other, source_change={"sample_rate_hz": 1_000_000.0}
    )
    with (
        pytest.raises(LegacyRecordingError, match="redux facts differ"),
        reader.open(registration.recording_ref),
    ):
        pass


def test_noncontiguous_chunks_fail_closed(tmp_path: Path) -> None:
    reader, registration, _, root = _fixture(tmp_path)
    path = root / registration.source_manifest.relative_path
    source = json.loads(path.read_bytes())
    source["chunks"][1]["first_sample_index"] = 4
    changed = json.dumps(source, sort_keys=True).encode()
    path.write_bytes(changed)
    changed_source = LegacyFileRef(
        "archive",
        registration.source_manifest.relative_path,
        Digest.sha256(changed),
        len(changed),
    )
    changed_metadata = replace(
        registration.recording_ref.metadata_object,
        digest=changed_source.digest,
        byte_count=changed_source.byte_count,
    )
    changed_registration = replace(
        registration,
        source_manifest=changed_source,
        recording_ref=replace(
            registration.recording_ref, metadata_object=changed_metadata
        ),
    )
    reader = LegacyRecordingReader({"archive": root}, (changed_registration,))
    with (
        pytest.raises(LegacyRecordingError, match="order, path, size, or digest"),
        reader.open(changed_registration.recording_ref),
    ):
        pass


@pytest.mark.parametrize(
    "relative_path",
    ("../escape", "/absolute", "captures/./object", "captures//object", "a\\b"),
)
def test_registration_rejects_path_traversal(relative_path: str) -> None:
    with pytest.raises(ValueError, match="confined POSIX"):
        LegacyFileRef("archive", relative_path, Digest.sha256(b"x"), 1)


def test_symlink_payload_is_rejected_without_following(tmp_path: Path) -> None:
    reader, registration, _, root = _fixture(tmp_path)
    path = root / registration.payloads[0].relative_path
    target = path.with_name("target.ci16")
    path.rename(target)
    path.symlink_to(target.name)
    with (
        pytest.raises(LegacyRecordingError, match="violates confinement"),
        reader.open(registration.recording_ref),
    ):
        pass


def test_configured_root_identity_cannot_be_atomically_substituted(
    tmp_path: Path,
) -> None:
    reader, registration, _, root = _fixture(tmp_path)
    old_root = tmp_path / "replaced-archive"
    root.rename(old_root)
    root.mkdir()
    with (
        pytest.raises(LegacyRecordingError, match="storage root was replaced"),
        reader.open(registration.recording_ref),
    ):
        pass
