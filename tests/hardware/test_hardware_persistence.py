from __future__ import annotations

import json
from dataclasses import replace

import pytest

from leo_flow.contracts.core import (
    Digest,
    HardwareSnapshotId,
    RadioId,
    ReceiverChainId,
    SchemaRef,
    StationId,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.hardware import (
    HardwareMetadataSnapshot,
    HardwareMetadataSnapshotRef,
    ReceiverChainMetadata,
)
from leo_flow.contracts.storage import ObjectRef
from leo_flow.hardware import (
    MAX_HARDWARE_SNAPSHOT_BYTES,
    DurableHardwareMetadataRepository,
    HardwareSnapshotIntegrityError,
    HardwareSnapshotNotFoundError,
    MalformedHardwareSnapshotError,
    decode_hardware_snapshot,
    encode_hardware_snapshot,
)
from leo_flow.hardware.persistence import (
    CatalogedHardwareSnapshot,
    HardwareSnapshotCatalog,
    HardwareSnapshotProjection,
)
from leo_flow.storage.filesystem import FileSystemBlobStore


def _snapshot() -> HardwareMetadataSnapshot:
    radio = RadioId("radio_v5")
    receiver = ReceiverChainId("rx_v5_0")
    return HardwareMetadataSnapshot(
        SchemaRef(HardwareMetadataSnapshot.SCHEMA_ID),
        HardwareSnapshotId("hw_v5_authority"),
        StationId("station_lab"),
        (radio,),
        (
            ReceiverChainMetadata(
                receiver,
                radio,
                0,
                "lnb-a",
                "linear-h",
                "cable-a",
                UtcNs(0),
                UtcNs(100),
            ),
            ReceiverChainMetadata(
                receiver,
                radio,
                0,
                "lnb-b",
                "linear-v",
                "cable-b",
                UtcNs(100),
                None,
            ),
        ),
    )


class _Catalog(HardwareSnapshotCatalog):
    def __init__(self) -> None:
        self.entry: CatalogedHardwareSnapshot | None = None
        self.key: str | None = None
        self.calls = 0

    def publish(
        self,
        projection: HardwareSnapshotProjection,
        bundle_ref: ObjectRef,
        *,
        idempotency_key: str,
    ) -> HardwareMetadataSnapshotRef:
        self.calls += 1
        entry = CatalogedHardwareSnapshot(projection, bundle_ref)
        if self.entry is not None and (
            self.entry != entry or self.key != idempotency_key
        ):
            raise RuntimeError("conflict")
        self.entry = entry
        self.key = idempotency_key
        return projection.ref

    def get(self, ref: HardwareMetadataSnapshotRef) -> CatalogedHardwareSnapshot | None:
        if self.entry is None or self.entry.projection.ref != ref:
            return None
        return self.entry

    def resolve(
        self, snapshot_id: HardwareSnapshotId
    ) -> CatalogedHardwareSnapshot | None:
        if self.entry is None or self.entry.projection.ref.snapshot_id != snapshot_id:
            return None
        return self.entry


def test_codec_is_deterministic_and_round_trips_effective_dated_metadata() -> None:
    snapshot = _snapshot()
    payload = encode_hardware_snapshot(snapshot)

    assert decode_hardware_snapshot(payload) == snapshot
    assert encode_hardware_snapshot(decode_hardware_snapshot(payload)) == payload


@pytest.mark.parametrize(
    "payload, message",
    [
        (b'{"x":1,"x":2}', "duplicate"),
        (b" " + encode_hardware_snapshot(_snapshot()), "canonical"),
        (b"[]", "root"),
        (b"0" * (MAX_HARDWARE_SNAPSHOT_BYTES + 1), "size"),
    ],
)
def test_codec_rejects_ambiguous_noncanonical_or_oversized_bytes(
    payload: bytes, message: str
) -> None:
    with pytest.raises(MalformedHardwareSnapshotError, match=message):
        decode_hardware_snapshot(payload)


def test_codec_rejects_unknown_fields() -> None:
    document = json.loads(encode_hardware_snapshot(_snapshot()))
    document["invented"] = True

    with pytest.raises(MalformedHardwareSnapshotError, match="fields"):
        decode_hardware_snapshot(canonical_json_bytes(document))


def test_contract_rejects_overlapping_receiver_effective_dates() -> None:
    snapshot = _snapshot()
    overlapping = replace(
        snapshot.receiver_chains[1],
        valid_from_utc_ns=UtcNs(99),
    )

    with pytest.raises(ValueError, match="overlapping"):
        replace(snapshot, receiver_chains=(snapshot.receiver_chains[0], overlapping))


def test_repository_is_cas_first_idempotent_and_reads_exact_snapshot(tmp_path) -> None:
    snapshot = _snapshot()
    catalog = _Catalog()
    repository = DurableHardwareMetadataRepository(
        FileSystemBlobStore(tmp_path / "cas"), catalog
    )

    ref = repository.publish(snapshot, idempotency_key="hardware:v5")
    assert repository.publish(snapshot, idempotency_key="hardware:v5") == ref
    assert repository.resolve_ref(snapshot.snapshot_id) == ref
    assert repository.get(ref) == snapshot
    assert catalog.calls == 2
    assert len(tuple((tmp_path / "cas" / "sha256").glob("*/*"))) == 1

    wrong = HardwareMetadataSnapshotRef(ref.snapshot_id, Digest.sha256(b"wrong"))
    with pytest.raises(HardwareSnapshotNotFoundError, match="exactly"):
        repository.get(wrong)
    with pytest.raises(HardwareSnapshotNotFoundError, match="requested ID"):
        repository.resolve_ref(HardwareSnapshotId("hw_missing"))


def test_reader_rejects_catalog_projection_disagreement(tmp_path) -> None:
    snapshot = _snapshot()
    catalog = _Catalog()
    repository = DurableHardwareMetadataRepository(
        FileSystemBlobStore(tmp_path / "cas"), catalog
    )
    ref = repository.publish(snapshot, idempotency_key="hardware:projection")
    assert catalog.entry is not None
    catalog.entry = replace(
        catalog.entry,
        projection=replace(catalog.entry.projection, station_id="station_tampered"),
    )

    with pytest.raises(HardwareSnapshotIntegrityError, match="projection"):
        repository.get(ref)
