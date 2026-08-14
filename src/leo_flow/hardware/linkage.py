"""Analysis-owned recording-to-authoritative-hardware linkage."""

from __future__ import annotations

from typing import Protocol

from leo_flow.contracts.capture import RecordingManifest
from leo_flow.contracts.core import RecordingId, canonical_digest
from leo_flow.contracts.hardware import (
    HardwareMetadataSnapshot,
    RecordingHardwareLink,
)
from leo_flow.contracts.ports import (
    HardwareMetadataReader,
    HardwareMetadataRefResolver,
)
from leo_flow.contracts.storage import PublishedRecordingRef
from leo_flow.storage.ports import RecordingObjectReader


class RecordingHardwareLinkError(RuntimeError):
    pass


class RecordingHardwareAuthorityError(RecordingHardwareLinkError):
    pass


class RecordingCatalogReader(Protocol):
    def get(self, recording_id: RecordingId) -> PublishedRecordingRef | None: ...


class RecordingHardwareLinkCatalog(Protocol):
    def publish(
        self, link: RecordingHardwareLink, *, idempotency_key: str
    ) -> RecordingHardwareLink: ...

    def get(self, recording_id: RecordingId) -> RecordingHardwareLink | None: ...


class RecordingHardwareLinker:
    """Verify a published manifest and freeze its ID as one exact snapshot ref."""

    def __init__(
        self,
        recordings: RecordingCatalogReader,
        recording_reader: RecordingObjectReader,
        hardware_refs: HardwareMetadataRefResolver,
        hardware: HardwareMetadataReader,
        links: RecordingHardwareLinkCatalog,
    ) -> None:
        self._recordings = recordings
        self._recording_reader = recording_reader
        self._hardware_refs = hardware_refs
        self._hardware = hardware
        self._links = links

    def link(self, recording_id: RecordingId) -> RecordingHardwareLink:
        published = self._recordings.get(recording_id)
        if published is None:
            raise RecordingHardwareAuthorityError(
                "recording is not authoritatively published"
            )
        recording_ref = published.recording_object
        with self._recording_reader.open(recording_ref) as view:
            manifest = view.manifest
        if manifest.recording_id != recording_id:
            raise RecordingHardwareAuthorityError(
                "recording reader substituted a different manifest"
            )
        hardware_ref = self._hardware_refs.resolve_ref(
            manifest.hardware_metadata_snapshot_id
        )
        if hardware_ref.snapshot_id != manifest.hardware_metadata_snapshot_id:
            raise RecordingHardwareAuthorityError(
                "hardware resolver substituted a different snapshot ID"
            )
        snapshot = self._hardware.get(hardware_ref)
        if snapshot.snapshot_id != hardware_ref.snapshot_id:
            raise RecordingHardwareAuthorityError(
                "hardware reader substituted a different snapshot"
            )
        _validate_assignment(manifest, snapshot)
        identity = {
            "recording_id": str(recording_id),
            "recording_identity_digest": str(recording_ref.identity_digest()),
            "hardware_snapshot_id": str(hardware_ref.snapshot_id),
            "hardware_snapshot_digest": str(hardware_ref.digest),
        }
        link_digest = canonical_digest(identity)
        link = RecordingHardwareLink(
            f"hwlink_{link_digest.value[:32]}",
            recording_id,
            recording_ref.identity_digest(),
            hardware_ref,
            link_digest,
        )
        return self._links.publish(
            link, idempotency_key=f"recording-hardware:{recording_id}"
        )


def require_recording_hardware_link(
    catalog: RecordingHardwareLinkCatalog, recording_id: RecordingId
) -> RecordingHardwareLink:
    """Fail closed for legacy or not-yet-linked recordings."""

    link = catalog.get(recording_id)
    if link is None:
        raise RecordingHardwareAuthorityError(
            "recording has no authoritative hardware link"
        )
    return link


def _validate_assignment(
    manifest: RecordingManifest, snapshot: HardwareMetadataSnapshot
) -> None:
    station_id = manifest.station_id
    radio_id = manifest.radio_id
    receiver_chain_ids = manifest.receiver_chain_ids
    started = manifest.capture_started_utc_ns
    finished = manifest.capture_finished_utc_ns
    if snapshot.station_id != station_id:
        raise RecordingHardwareAuthorityError(
            "hardware snapshot belongs to a different station"
        )
    if radio_id not in snapshot.radio_ids:
        raise RecordingHardwareAuthorityError(
            "hardware snapshot does not contain the recording radio"
        )
    used_receiver_ids = {
        receiver_id
        for segment in manifest.segments
        for receiver_id in segment.requested.receiver_chain_ids
    }
    if used_receiver_ids != set(receiver_chain_ids):
        raise RecordingHardwareAuthorityError(
            "manifest receiver-chain inventory differs from captured segments"
        )
    for receiver_id in receiver_chain_ids:
        applicable = tuple(
            chain
            for chain in snapshot.receiver_chains
            if chain.receiver_chain_id == receiver_id
            and chain.radio_id == radio_id
            and chain.valid_from_utc_ns <= started
            and (
                chain.valid_until_utc_ns is None or finished <= chain.valid_until_utc_ns
            )
        )
        if len(applicable) != 1:
            raise RecordingHardwareAuthorityError(
                f"receiver chain {receiver_id} has no single authoritative "
                "assignment covering the recording"
            )
