from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from typing import Any

import pytest

from leo_flow.contracts.core import Digest, DigestAlgorithm
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.recording_import.retro_qam import (
    RetroQamCorpusError,
    RetroQamImportSpecification,
    import_retro_qam_recording,
    prepare_retro_qam_recording,
)
from leo_flow.storage.catalog import (
    InMemoryRecordingCatalog,
    RecordingConflictError,
    RecordingPublisherAdapter,
)
from leo_flow.storage.filesystem import FileSystemBlobStore
from leo_flow.storage.local_recording import RootedSigMFRecordingStore
from leo_flow.storage.recording_codec import (
    SigMFRecordingObjectReader,
    SigMFRecordingWriter,
)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _archive(tmp_path: Path) -> tuple[Path, Path, Digest]:
    root = tmp_path / "archive"
    (root / "raw").mkdir(parents=True)
    (root / "provenance").mkdir()
    samples = 8
    words = tuple(range(samples * 2 * 2))
    iq_bytes = struct.pack(f"<{len(words)}h", *words)
    iq_path = root / "raw/clip-002.ci16"
    iq_path.write_bytes(iq_bytes)
    iq_digest = hashlib.sha256(iq_bytes).hexdigest()
    window = iq_bytes[2 * 8 : 5 * 8]
    evidence: dict[str, Any] = {
        "recording_id": "legacy-recording",
        "source": {"recording_id": "legacy-recording"},
        "radio_parameters": {
            "center_frequency_hz": 1_709_687_500.0,
            "sample_rate_hz": 2_500_000.0,
            "bandwidth_hz": 2_500_000.0,
            "configured_gain_db": 50.0,
            "lnb_lo_hz": 9_750_000_000.0,
        },
        "clips": [
            {
                "interval_id": "clip-002",
                "bytes": len(iq_bytes),
                "sample_count": samples,
                "sha256": iq_digest,
                "receiver_count": 2,
                "dtype": "ci16_le",
                "layout": "sample,receiver,component; receivers=rx0,rx1; components=i,q",
                "first_sample": 100,
                "first_utc_ns": 1_786_655_518_595_059_712,
                "stop_utc_ns": 1_786_655_564_217_242_112,
                "utc_uncertainty_s": 0.095,
            }
        ],
    }
    source: dict[str, Any] = {
        "identity": {"serial": "historical-serial"},
        "created_utc_ns": 1_786_655_421_192_437_953,
        "completed_utc_ns": 1_786_655_640_087_028_137,
        "sample_rate_hz": 2_500_000.0,
        "center_frequency_hz": 1_709_687_500.0,
        "bandwidth_hz": 2_500_000.0,
        "configured_gain_db": 50.0,
        "lnb_lo_hz": 9_750_000_000.0,
        "gain_mode": "manual",
        "dtype": "ci16_le",
        "layout": "sample,receiver,component; receivers=rx0,rx1; components=i,q",
    }
    files = {
        "raw/clip-002.ci16": iq_bytes,
        "provenance/manifest.json": _json_bytes(evidence),
        "provenance/source-manifest.json": _json_bytes(source),
    }
    for relative, payload in files.items():
        if relative != "raw/clip-002.ci16":
            (root / relative).write_bytes(payload)
    inventory = {
        relative: hashlib.sha256(payload).hexdigest()
        for relative, payload in files.items()
    }
    sums = "".join(f"{digest}  {relative}\n" for relative, digest in inventory.items())
    (root / "SHA256SUMS").write_text(sums, encoding="ascii")
    document = {
        "schema": "org.leo-flow.external-retro-qam-corpus/v1",
        "recording_id": "legacy-recording",
        "fixture_id": "synthetic-retro",
        "archive": {
            "root": "/canonical/archive",
            "sha256sums_sha256": hashlib.sha256(sums.encode()).hexdigest(),
        },
        "archive_objects": [
            {"relative_path": relative, "sha256": digest}
            for relative, digest in inventory.items()
        ],
        "format": {
            "byte_order": "little",
            "component_dtype": "int16",
            "component_order": ["i", "q"],
            "layout": ["sample", "receiver", "component"],
            "receiver_count": 2,
            "sample_rate_hz": 2_500_000.0,
        },
        "iq_object": {
            "relative_path": "raw/clip-002.ci16",
            "sha256": iq_digest,
            "byte_count": len(iq_bytes),
            "sample_count": samples,
            "bytes_per_sample": 8,
        },
        "selected_window": {
            "sample_offset": 2,
            "sample_count": 3,
            "byte_offset": 16,
            "byte_count": 24,
            "sha256": hashlib.sha256(window).hexdigest(),
        },
        "scope": {
            "calibrated_detection": False,
            "known_published_pilot": True,
            "payload_decoded": False,
        },
    }
    manifest = tmp_path / "corpus.json"
    manifest_bytes = _json_bytes(document)
    manifest.write_bytes(manifest_bytes)
    return manifest, root, Digest.sha256(manifest_bytes)


def _prepare(tmp_path: Path):
    manifest, root, digest = _archive(tmp_path)
    return prepare_retro_qam_recording(
        RetroQamImportSpecification(manifest, root, digest)
    )


def test_prepare_verifies_geometry_and_preserves_non_calibration_scope(
    tmp_path: Path,
) -> None:
    prepared = _prepare(tmp_path)

    assert str(prepared.manifest.recording_id) == "rec_retro_qam_20260813_clip002"
    assert prepared.manifest.activities[0].kind.value == "test"
    assert str(prepared.manifest.station_id) == "station_historical_unattributed"
    assert str(prepared.manifest.radio_id) == "radio_historical_pluto_5d4d"
    assert prepared.manifest.capture_started_utc_ns == 1_786_655_518_595_059_712
    assert prepared.manifest.radio_serial == "historical-serial"
    assert prepared.manifest.receiver_chain_ids == (
        "rx_retro_qam_0",
        "rx_retro_qam_1",
    )
    expected_tags = {
        "calibrated_detection": False,
        "calibration_eligible": False,
        "conditioned_canary": True,
        "historical_capture": True,
        "payload_decoded": False,
    }
    tags = dict(prepared.manifest.experiment_tags)
    assert {key: tags[key] for key in expected_tags} == expected_tags
    assert all(
        chain.lnb_id.startswith("historical-unqualified-port-")
        for chain in prepared.hardware_snapshot.receiver_chains
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("manifest_digest", "manifest digest differs"),
        ("iq", "archive object digest differs"),
        ("window", "selected-window digest differs"),
        ("geometry", "geometry is inconsistent"),
        ("inventory", "archive inventory differs"),
    ],
)
def test_prepare_fails_closed_on_pinned_input_changes(
    tmp_path: Path, mutation: str, message: str
) -> None:
    manifest, root, digest = _archive(tmp_path)
    if mutation == "manifest_digest":
        digest = Digest.sha256(b"different")
    elif mutation == "iq":
        (root / "raw/clip-002.ci16").write_bytes(b"x" * 64)
    elif mutation == "window":
        document = json.loads(manifest.read_bytes())
        document["selected_window"]["sha256"] = "0" * 64
        payload = _json_bytes(document)
        manifest.write_bytes(payload)
        digest = Digest.sha256(payload)
    elif mutation == "geometry":
        document = json.loads(manifest.read_bytes())
        document["iq_object"]["bytes_per_sample"] = 4
        payload = _json_bytes(document)
        manifest.write_bytes(payload)
        digest = Digest.sha256(payload)
    else:
        sums = (root / "SHA256SUMS").read_text(encoding="ascii").splitlines()[0] + "\n"
        (root / "SHA256SUMS").write_text(sums, encoding="ascii")
        document = json.loads(manifest.read_bytes())
        document["archive"]["sha256sums_sha256"] = hashlib.sha256(
            sums.encode()
        ).hexdigest()
        payload = _json_bytes(document)
        manifest.write_bytes(payload)
        digest = Digest.sha256(payload)

    with pytest.raises(RetroQamCorpusError, match=message):
        prepare_retro_qam_recording(
            RetroQamImportSpecification(manifest, root, digest)
        )


def test_import_is_publicly_readable_and_idempotent(tmp_path: Path) -> None:
    prepared = _prepare(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    cas = FileSystemBlobStore(tmp_path / "cas")
    catalog = InMemoryRecordingCatalog()

    def run(slot: str):
        local = RootedSigMFRecordingStore(staging)
        publisher = RecordingPublisherAdapter(local, cas, catalog)
        return import_retro_qam_recording(
            prepared,
            SigMFRecordingWriter(),
            publisher,
            destination=str(staging / slot),
        )

    first, _ = run("first")
    second, _ = run("second")
    assert second == first
    with SigMFRecordingObjectReader(cas).open(first.recording_object) as view:
        assert view.manifest == prepared.manifest
        assert view.read_iq_bytes(prepared.manifest.segments[0].segment_id, 2, 5) == (
            prepared.source_iq.read_bytes()[16:40]
        )


def test_import_conflicts_when_public_recording_id_has_other_content(
    tmp_path: Path,
) -> None:
    prepared = _prepare(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    cas = FileSystemBlobStore(tmp_path / "cas")
    catalog = InMemoryRecordingCatalog()
    different = ObjectRef(
        Digest.sha256(b"different-data"),
        len(b"different-data"),
        "application/octet-stream",
        "leo-recording-data-v1",
        "fake:data",
    )
    metadata = ObjectRef(
        Digest.sha256(b"different-metadata"),
        len(b"different-metadata"),
        "application/json",
        "leo-recording-metadata-v1",
        "fake:metadata",
    )
    catalog.publish(
        RecordingObjectRef(
            prepared.manifest.recording_id,
            different,
            metadata,
            Digest.sha256(b"different-manifest"),
        ),
        idempotency_key="other-import",
    )
    publisher = RecordingPublisherAdapter(
        RootedSigMFRecordingStore(staging), cas, catalog
    )

    with pytest.raises(RecordingConflictError):
        import_retro_qam_recording(
            prepared,
            SigMFRecordingWriter(),
            publisher,
            destination=str(staging / "conflict"),
        )


@pytest.mark.integration
def test_mounted_frozen_corpus_prepares_exact_public_recording() -> None:
    root = Path("/mnt/qnap01/mouse9911/leo-store/2026_08_17_RETRO_QAM")
    if not root.is_dir():
        pytest.skip("read-only RETRO QAM corpus is not mounted")
    prepared = prepare_retro_qam_recording(
        RetroQamImportSpecification(
            Path("tests/recording_analysis/fixtures/retro_qam_2026_08_17_v1.json"),
            root,
            Digest(
                DigestAlgorithm.SHA256,
                "47a5c98064128cfdcebcf1350acb3b3005f2646e769d45d8c92a5f2def22ba7e",
            ),
        )
    )

    assert prepared.source_iq_bytes == 500_200_000
    assert prepared.selected_window_start_sample == 38_000_000
    assert prepared.selected_window_sample_count == 25_000
    assert prepared.selected_window_digest.value == (
        "a80c3b0d94b95548d9ae0ab5d8243fee8cf6c760ccb6fa4ca4efeb6351176e50"
    )
