"""Authoritative effective-dated hardware metadata persistence."""

from .codec import (
    HARDWARE_SNAPSHOT_FORMAT_ID,
    HARDWARE_SNAPSHOT_MEDIA_TYPE,
    MAX_HARDWARE_SNAPSHOT_BYTES,
    MalformedHardwareSnapshotError,
    decode_hardware_snapshot,
    encode_hardware_snapshot,
)
from .linkage import (
    RecordingHardwareAuthorityError,
    RecordingHardwareLinker,
    require_recording_hardware_link,
)
from .persistence import (
    DurableHardwareMetadataRepository,
    HardwareSnapshotIntegrityError,
    HardwareSnapshotNotFoundError,
)

__all__ = [
    "HARDWARE_SNAPSHOT_FORMAT_ID",
    "HARDWARE_SNAPSHOT_MEDIA_TYPE",
    "MAX_HARDWARE_SNAPSHOT_BYTES",
    "DurableHardwareMetadataRepository",
    "HardwareSnapshotIntegrityError",
    "HardwareSnapshotNotFoundError",
    "MalformedHardwareSnapshotError",
    "RecordingHardwareAuthorityError",
    "RecordingHardwareLinker",
    "decode_hardware_snapshot",
    "encode_hardware_snapshot",
    "require_recording_hardware_link",
]
