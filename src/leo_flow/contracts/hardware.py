"""Immutable, effective-dated hardware metadata contracts."""

from __future__ import annotations

from dataclasses import dataclass

from ._validation import require_utc_ns
from .core import (
    V0_1,
    Digest,
    HardwareSnapshotId,
    RadioId,
    ReceiverChainId,
    SchemaRef,
    StationId,
    UtcNs,
)


@dataclass(frozen=True)
class ReceiverChainMetadata:
    receiver_chain_id: ReceiverChainId
    radio_id: RadioId
    radio_channel: int
    lnb_id: str
    polarization: str | None
    cable_id: str | None
    valid_from_utc_ns: UtcNs
    valid_until_utc_ns: UtcNs | None

    def __post_init__(self) -> None:
        require_utc_ns(self.valid_from_utc_ns, "valid_from_utc_ns")
        if self.radio_channel < 0:
            raise ValueError("radio_channel must be non-negative")
        if self.valid_until_utc_ns is not None:
            require_utc_ns(self.valid_until_utc_ns, "valid_until_utc_ns")
            if self.valid_until_utc_ns <= self.valid_from_utc_ns:
                raise ValueError("hardware validity interval must be non-empty")


@dataclass(frozen=True)
class HardwareMetadataSnapshot:
    schema: SchemaRef
    snapshot_id: HardwareSnapshotId
    station_id: StationId
    radio_ids: tuple[RadioId, ...]
    receiver_chains: tuple[ReceiverChainMetadata, ...]

    SCHEMA_ID = "org.leo-flow.hardware-metadata-snapshot"

    def __post_init__(self) -> None:
        if self.schema.schema_id != self.SCHEMA_ID or self.schema.version != V0_1:
            raise ValueError("unsupported hardware metadata schema")
        if len(set(self.radio_ids)) != len(self.radio_ids):
            raise ValueError("radio IDs must be unique")
        if any(chain.radio_id not in self.radio_ids for chain in self.receiver_chains):
            raise ValueError("receiver chain references unknown radio")


@dataclass(frozen=True)
class HardwareMetadataSnapshotRef:
    snapshot_id: HardwareSnapshotId
    digest: Digest
