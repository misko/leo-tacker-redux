from __future__ import annotations

import ast
import inspect
import io
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO

import pytest

from leo_flow.analysis.model.tracking_input_codec import (
    MAX_TRACKING_INPUT_BYTES,
    encode_tracking_input,
)
from leo_flow.analysis.model.tracking_input_persistence import (
    CatalogedTrackingInput,
    DurableTrackingInputRepository,
    TrackingInputCatalog,
    TrackingInputIntegrityError,
    TrackingInputNotFoundError,
    TrackingInputPersistenceError,
    TrackingInputProjection,
    tracking_input_projection,
)
from leo_flow.contracts.core import Digest, ReceiverChainId, RecordingId
from leo_flow.contracts.storage import ObjectMetadata, ObjectRef
from leo_flow.contracts.tracking_input import (
    TRACKING_INPUT_FORMAT_ID,
    TRACKING_INPUT_MEDIA_TYPE,
    TrackingInputSnapshot,
    TrackingInputSnapshotIdentity,
    TrackingInputSnapshotRef,
)
from tests.model_analysis.test_tracking_input_builder import _case, _observation


class _MemoryBlobs:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.payload = b""
        self.put_ref: ObjectRef | None = None
        self.head_ref: ObjectRef | None = None
        self.verified = True
        self.put_error: Exception | None = None
        self.head_error: Exception | None = None
        self.open_error: Exception | None = None
        self.open_count = 0
        self.put_arguments: dict[str, object] = {}

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
        self.events.append("blob.put")
        if self.put_error is not None:
            raise self.put_error
        self.payload = stream.read()
        self.put_arguments = {
            "expected_digest": expected_digest,
            "expected_bytes": expected_bytes,
            "media_type": media_type,
            "format_id": format_id,
            "idempotency_key": idempotency_key,
        }
        return self.put_ref or ObjectRef(
            expected_digest,
            expected_bytes,
            media_type,
            format_id,
            "cas:tracking-input",
        )

    def head(self, ref: ObjectRef) -> ObjectMetadata:
        self.events.append("blob.head")
        if self.head_error is not None:
            raise self.head_error
        return ObjectMetadata(self.head_ref or ref, self.verified)

    @contextmanager
    def open(self, ref: ObjectRef, byte_range: object = None) -> Iterator[BinaryIO]:
        del ref, byte_range
        self.events.append("blob.open")
        self.open_count += 1
        if self.open_error is not None:
            raise self.open_error
        yield io.BytesIO(self.payload)


class _MemoryCatalog(TrackingInputCatalog):
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.projection: TrackingInputProjection | None = None
        self.publish_ref: TrackingInputSnapshotRef | None = None
        self.publish_error: Exception | None = None
        self.get_error: Exception | None = None
        self.identity_error: Exception | None = None
        self.last_get: TrackingInputSnapshotRef | None = None
        self.last_identity: TrackingInputSnapshotIdentity | None = None
        self.publish_key = ""

    def publish(
        self,
        projection: TrackingInputProjection,
        *,
        idempotency_key: str,
    ) -> TrackingInputSnapshotRef:
        self.events.append("catalog.publish")
        if self.publish_error is not None:
            raise self.publish_error
        self.projection = projection
        self.publish_key = idempotency_key
        return self.publish_ref or projection.ref

    def get(self, ref: TrackingInputSnapshotRef) -> CatalogedTrackingInput | None:
        self.events.append("catalog.get")
        self.last_get = ref
        if self.get_error is not None:
            raise self.get_error
        if self.projection is None:
            return None
        return CatalogedTrackingInput(self.projection)

    def get_by_identity(
        self, identity: TrackingInputSnapshotIdentity
    ) -> CatalogedTrackingInput | None:
        self.events.append("catalog.get_by_identity")
        self.last_identity = identity
        if self.identity_error is not None:
            raise self.identity_error
        if self.projection is None:
            return None
        return CatalogedTrackingInput(self.projection)


def _snapshot() -> TrackingInputSnapshot:
    return _case().freeze()


def _stored(
    snapshot: TrackingInputSnapshot | None = None,
    *,
    payload: bytes | None = None,
) -> tuple[
    DurableTrackingInputRepository,
    _MemoryBlobs,
    _MemoryCatalog,
    TrackingInputSnapshotRef,
]:
    actual_snapshot = snapshot or _snapshot()
    actual_payload = payload or encode_tracking_input(actual_snapshot)
    ref = TrackingInputSnapshotRef(
        actual_snapshot.snapshot_id,
        actual_snapshot.snapshot_digest,
        actual_snapshot.membership_digest,
        ObjectRef(
            Digest.sha256(actual_payload),
            len(actual_payload),
            TRACKING_INPUT_MEDIA_TYPE,
            TRACKING_INPUT_FORMAT_ID,
            "cas:tracking-input",
        ),
    )
    events: list[str] = []
    blobs = _MemoryBlobs(events)
    blobs.payload = actual_payload
    catalog = _MemoryCatalog(events)
    catalog.projection = tracking_input_projection(actual_snapshot, ref)
    return DurableTrackingInputRepository(blobs, catalog), blobs, catalog, ref


def test_publish_is_cas_first_with_exact_metadata_and_idempotency() -> None:
    snapshot = _snapshot()
    events: list[str] = []
    blobs = _MemoryBlobs(events)
    catalog = _MemoryCatalog(events)
    repository = DurableTrackingInputRepository(blobs, catalog)

    ref = repository.publish(snapshot, idempotency_key="tracking-run-1")
    payload = encode_tracking_input(snapshot)

    assert events == ["blob.put", "catalog.publish"]
    assert blobs.payload == payload
    assert blobs.put_arguments == {
        "expected_digest": Digest.sha256(payload),
        "expected_bytes": len(payload),
        "media_type": TRACKING_INPUT_MEDIA_TYPE,
        "format_id": TRACKING_INPUT_FORMAT_ID,
        "idempotency_key": "tracking-run-1:tracking-input-bundle",
    }
    assert catalog.publish_key == "tracking-run-1"
    assert ref.snapshot_id == snapshot.snapshot_id
    assert catalog.projection == tracking_input_projection(snapshot, ref)


@pytest.mark.parametrize("key", ["", "has whitespace"])
def test_publish_rejects_invalid_idempotency_before_writes(key: str) -> None:
    events: list[str] = []
    repository = DurableTrackingInputRepository(
        _MemoryBlobs(events), _MemoryCatalog(events)
    )
    with pytest.raises(ValueError, match="token"):
        repository.publish(_snapshot(), idempotency_key=key)
    assert events == []


def test_publish_fails_closed_on_blob_catalog_and_returned_ref_errors() -> None:
    snapshot = _snapshot()
    events: list[str] = []
    blobs = _MemoryBlobs(events)
    catalog = _MemoryCatalog(events)
    repository = DurableTrackingInputRepository(blobs, catalog)

    blobs.put_error = OSError("CAS unavailable")
    with pytest.raises(TrackingInputPersistenceError, match="CAS publication"):
        repository.publish(snapshot, idempotency_key="run-1")
    assert events == ["blob.put"]

    events.clear()
    blobs.put_error = None
    blobs.put_ref = ObjectRef(
        _digest_for("wrong"),
        1,
        TRACKING_INPUT_MEDIA_TYPE,
        TRACKING_INPUT_FORMAT_ID,
        "cas:wrong",
    )
    with pytest.raises(TrackingInputIntegrityError, match="metadata"):
        repository.publish(snapshot, idempotency_key="run-2")
    assert events == ["blob.put"]

    events.clear()
    blobs.put_ref = None
    catalog.publish_error = RuntimeError("catalog unavailable")
    with pytest.raises(TrackingInputPersistenceError, match="catalog publication"):
        repository.publish(snapshot, idempotency_key="run-3")
    assert events == ["blob.put", "catalog.publish"]

    events.clear()
    catalog.publish_error = None
    good_payload = encode_tracking_input(snapshot)
    catalog.publish_ref = TrackingInputSnapshotRef(
        snapshot.snapshot_id,
        snapshot.snapshot_digest,
        _digest_for("substituted-membership"),
        ObjectRef(
            Digest.sha256(good_payload),
            len(good_payload),
            TRACKING_INPUT_MEDIA_TYPE,
            TRACKING_INPUT_FORMAT_ID,
            "cas:tracking-input",
        ),
    )
    with pytest.raises(TrackingInputIntegrityError, match="different"):
        repository.publish(snapshot, idempotency_key="run-4")


def _digest_for(label: str) -> Digest:
    return Digest.sha256(label.encode())


def test_get_verifies_catalog_blob_bytes_decoder_and_projection() -> None:
    snapshot = _snapshot()
    repository, blobs, catalog, ref = _stored(snapshot)

    assert repository.get(ref) == snapshot
    assert catalog.last_get == ref
    assert blobs.events == ["catalog.get", "blob.head", "blob.open"]


def test_get_accepts_locator_relocation_but_not_identity_substitution() -> None:
    snapshot = _snapshot()
    repository, blobs, catalog, requested = _stored(snapshot)
    assert catalog.projection is not None
    relocated_ref = replace(
        catalog.projection.ref,
        bundle_ref=replace(
            catalog.projection.ref.bundle_ref, locator="cas:relocated-tracking-input"
        ),
    )
    catalog.projection = replace(catalog.projection, ref=relocated_ref)

    assert repository.get(requested) == snapshot
    assert requested.identity_digest() == relocated_ref.identity_digest()
    assert blobs.events[-2:] == ["blob.head", "blob.open"]

    catalog.projection = replace(
        catalog.projection,
        ref=replace(relocated_ref, membership_digest=_digest_for("other-membership")),
    )
    with pytest.raises(TrackingInputIntegrityError, match="substituted"):
        repository.get(requested)


def test_get_by_identity_resolves_relocated_ref_without_second_catalog_lookup() -> None:
    snapshot = _snapshot()
    repository, blobs, catalog, requested = _stored(snapshot)
    assert catalog.projection is not None
    relocated_ref = replace(
        catalog.projection.ref,
        bundle_ref=replace(
            catalog.projection.ref.bundle_ref, locator="cas:relocated-current"
        ),
    )
    catalog.projection = replace(catalog.projection, ref=relocated_ref)

    view = repository.get_by_identity(requested.identity())

    assert view.ref == relocated_ref
    assert view.snapshot == snapshot
    assert catalog.last_identity == requested.identity()
    assert catalog.last_get is None
    assert blobs.events == ["catalog.get_by_identity", "blob.head", "blob.open"]


def test_get_by_identity_rejects_missing_ambiguous_or_substituted_state() -> None:
    repository, _, catalog, requested = _stored()
    catalog.projection = None
    with pytest.raises(TrackingInputNotFoundError):
        repository.get_by_identity(requested.identity())

    catalog.identity_error = RuntimeError("ambiguous identity")
    with pytest.raises(TrackingInputPersistenceError, match="resolution"):
        repository.get_by_identity(requested.identity())

    repository, _, catalog, requested = _stored()
    assert catalog.projection is not None
    catalog.projection = replace(
        catalog.projection,
        ref=replace(
            catalog.projection.ref,
            membership_digest=_digest_for("substituted-membership"),
        ),
    )
    with pytest.raises(TrackingInputIntegrityError, match="substituted"):
        repository.get_by_identity(requested.identity())


def test_get_fails_closed_on_missing_or_catalog_and_blob_failures() -> None:
    repository, blobs, catalog, ref = _stored()
    catalog.projection = None
    with pytest.raises(TrackingInputNotFoundError):
        repository.get(ref)

    catalog.get_error = RuntimeError("catalog failed")
    with pytest.raises(TrackingInputPersistenceError, match="catalog read"):
        repository.get(ref)

    repository, blobs, catalog, ref = _stored()
    blobs.head_error = OSError("head failed")
    with pytest.raises(TrackingInputPersistenceError, match="blob read"):
        repository.get(ref)

    blobs.head_error = None
    blobs.open_error = OSError("open failed")
    with pytest.raises(TrackingInputPersistenceError, match="blob read"):
        repository.get(ref)


@pytest.mark.parametrize("verified", [False, True])
def test_get_rejects_unverified_or_substituted_blob_metadata(verified: bool) -> None:
    repository, blobs, _, ref = _stored()
    blobs.verified = verified
    if verified:
        blobs.head_ref = replace(ref.bundle_ref, locator="cas:unexpected")
    with pytest.raises(TrackingInputIntegrityError, match="metadata"):
        repository.get(ref)
    assert blobs.open_count == 0


@pytest.mark.parametrize("mutation", ["truncated", "appended", "same-length"])
def test_get_rejects_bytes_that_disagree_with_catalog(mutation: str) -> None:
    repository, blobs, _, ref = _stored()
    if mutation == "truncated":
        blobs.payload = blobs.payload[:-1]
    elif mutation == "appended":
        blobs.payload += b"x"
    else:
        blobs.payload = b"x" + blobs.payload[1:]
    with pytest.raises(TrackingInputIntegrityError, match="bytes"):
        repository.get(ref)


def test_get_rejects_noncanonical_bytes_even_when_blob_digest_agrees() -> None:
    snapshot = _snapshot()
    noncanonical = encode_tracking_input(snapshot) + b"\n"
    repository, _, _, ref = _stored(snapshot, payload=noncanonical)
    with pytest.raises(TrackingInputIntegrityError, match="noncanonical"):
        repository.get(ref)


def test_get_rejects_decoded_snapshot_and_projection_substitutions() -> None:
    expected = _snapshot()
    changed_observation = _observation(
        RecordingId("rec_tracking"),
        ReceiverChainId("rx_tracking"),
        frequency_hz=1_500_000_001.0,
    )
    other = _case(observations=(changed_observation,)).freeze()
    assert other != expected

    substituted, _, _, substituted_ref = _stored(
        expected, payload=encode_tracking_input(other)
    )
    with pytest.raises(TrackingInputIntegrityError, match="decoded"):
        substituted.get(substituted_ref)

    repository, _, catalog, ref = _stored(expected)
    assert catalog.projection is not None
    first = catalog.projection.entries[0]
    catalog.projection = replace(
        catalog.projection,
        entries=(replace(first, feature_id="feature_substituted"),),
    )
    with pytest.raises(TrackingInputIntegrityError, match="projection"):
        repository.get(ref)

    catalog.projection = tracking_input_projection(expected, ref)
    catalog.projection = replace(
        catalog.projection,
        durable_dataset=replace(
            catalog.projection.durable_dataset,
            snapshot_digest=_digest_for("substituted-dataset"),
        ),
    )
    with pytest.raises(TrackingInputIntegrityError, match="projection"):
        repository.get(ref)


def test_get_rejects_oversized_declared_bundle_before_open() -> None:
    repository, blobs, catalog, ref = _stored()
    oversized_object = replace(ref.bundle_ref, byte_count=MAX_TRACKING_INPUT_BYTES + 1)
    oversized_ref = replace(ref, bundle_ref=oversized_object)
    assert catalog.projection is not None
    catalog.projection = replace(catalog.projection, ref=oversized_ref)
    with pytest.raises(TrackingInputIntegrityError, match="metadata"):
        repository.get(oversized_ref)
    assert blobs.open_count == 0


def test_projection_binds_every_ordered_entry_identity() -> None:
    snapshot = _snapshot()
    _, _, catalog, ref = _stored(snapshot)
    projection = tracking_input_projection(snapshot, ref)
    entry = snapshot.entries[0]

    assert catalog.projection == projection
    assert projection.ref.identity_digest() == ref.identity_digest()
    assert projection.entry_count == len(snapshot.entries)
    assert projection.entry_count == len(projection.entries)
    assert projection.provenance_digest == Digest.sha256(
        _canonical_provenance(snapshot)
    )
    assert (
        projection.entries[0].feature_bundle_digest == entry.feature_set.bundle_digest
    )
    assert (
        projection.entries[0].recording_identity_digest
        == entry.recording_identity_digest
    )
    assert projection.entries[0].hardware_link_digest == entry.hardware_link.link_digest
    assert (
        projection.entries[0].ephemeris_link_digest == entry.ephemeris_link.link_digest
    )
    assert projection.entries[0].calibration_ref == entry.calibration.calibration_ref


def _canonical_provenance(snapshot: TrackingInputSnapshot) -> bytes:
    from leo_flow.contracts.core import canonical_json_bytes

    return canonical_json_bytes(snapshot.provenance)


def test_persistence_architecture_has_no_forbidden_capabilities() -> None:
    path = Path(inspect.getfile(DurableTrackingInputRepository))
    tree = ast.parse(path.read_text())
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    forbidden = (
        "pathlib",
        "socket",
        "requests",
        "httpx",
        "time",
        "datetime",
        "psycopg",
    )
    assert not any(name.startswith(forbidden) for name in imports)
    assert not any("postgres" in name or "recording" in name for name in imports)
