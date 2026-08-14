"""Immutable, effective-dated hardware metadata contracts."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

from ._validation import require_utc_ns
from .core import (
    V0_1,
    Digest,
    HardwareSnapshotId,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    StationId,
    UtcNs,
    canonical_digest,
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
        if not isinstance(self.lnb_id, str) or not self.lnb_id:
            raise ValueError("lnb_id must be non-empty")
        if self.polarization is not None and not self.polarization:
            raise ValueError("polarization must be non-empty when present")
        if self.cable_id is not None and not self.cable_id:
            raise ValueError("cable_id must be non-empty when present")
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
        if not self.radio_ids:
            raise ValueError("hardware snapshot must contain a radio")
        if any(chain.radio_id not in self.radio_ids for chain in self.receiver_chains):
            raise ValueError("receiver chain references unknown radio")
        by_receiver: dict[ReceiverChainId, list[ReceiverChainMetadata]] = {}
        for chain in self.receiver_chains:
            by_receiver.setdefault(chain.receiver_chain_id, []).append(chain)
        for receiver, intervals in by_receiver.items():
            ordered = sorted(intervals, key=lambda item: int(item.valid_from_utc_ns))
            for previous, current in pairwise(ordered):
                if (
                    previous.valid_until_utc_ns is None
                    or previous.valid_until_utc_ns > current.valid_from_utc_ns
                ):
                    raise ValueError(
                        f"receiver chain {receiver} has overlapping effective dates"
                    )


@dataclass(frozen=True)
class HardwareMetadataSnapshotRef:
    snapshot_id: HardwareSnapshotId
    digest: Digest


@dataclass(frozen=True)
class RecordingHardwareLink:
    """Immutable authority joining one recording identity to one hardware ref."""

    link_id: str
    recording_id: RecordingId
    recording_identity_digest: Digest
    hardware_snapshot_ref: HardwareMetadataSnapshotRef
    link_digest: Digest

    def __post_init__(self) -> None:
        expected_digest = canonical_digest(
            {
                "recording_id": str(self.recording_id),
                "recording_identity_digest": str(self.recording_identity_digest),
                "hardware_snapshot_id": str(self.hardware_snapshot_ref.snapshot_id),
                "hardware_snapshot_digest": str(self.hardware_snapshot_ref.digest),
            }
        )
        if not self.link_id.startswith("hwlink_") or len(self.link_id) != 39:
            raise ValueError("hardware link ID must be hwlink_ plus 32 hex characters")
        try:
            int(self.link_id[7:], 16)
        except ValueError as error:
            raise ValueError("hardware link ID suffix must be lowercase hex") from error
        if self.link_id[7:] != self.link_id[7:].lower():
            raise ValueError("hardware link ID suffix must be lowercase hex")
        if self.recording_identity_digest.algorithm.value != "sha256":
            raise ValueError("recording identity digest must use sha256")
        if self.link_digest.algorithm.value != "sha256":
            raise ValueError("hardware link digest must use sha256")
        if self.hardware_snapshot_ref.digest.algorithm.value != "sha256":
            raise ValueError("hardware snapshot digest must use sha256")
        if self.link_digest != expected_digest:
            raise ValueError("hardware link digest differs from linked identities")
        if self.link_id != f"hwlink_{self.link_digest.value[:32]}":
            raise ValueError("hardware link ID must derive from link digest")
