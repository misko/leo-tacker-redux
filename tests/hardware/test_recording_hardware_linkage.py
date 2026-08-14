from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace

import pytest

from leo_flow.contracts.core import (
    Digest,
    RadioId,
    ReceiverChainId,
    SchemaRef,
    StationId,
    UtcNs,
    canonical_digest,
)
from leo_flow.contracts.hardware import (
    HardwareMetadataSnapshot,
    HardwareMetadataSnapshotRef,
    ReceiverChainMetadata,
    RecordingHardwareLink,
)
from leo_flow.contracts.storage import PublishedRecordingRef
from leo_flow.hardware import (
    RecordingHardwareAuthorityError,
    RecordingHardwareLinker,
    require_recording_hardware_link,
)
from tests.recording_analysis.fakes import SegmentFixture, make_view


class _Authority:
    def __init__(self, ref: HardwareMetadataSnapshotRef, snapshot) -> None:
        self.ref = ref
        self.snapshot = snapshot
        self.resolve_calls = []
        self.get_calls = []

    def resolve_ref(self, snapshot_id):
        self.resolve_calls.append(snapshot_id)
        if snapshot_id != self.ref.snapshot_id:
            raise RecordingHardwareAuthorityError("unknown hardware ID")
        return self.ref

    def get(self, ref):
        self.get_calls.append(ref)
        if ref != self.ref:
            raise RecordingHardwareAuthorityError("wrong exact ref")
        return self.snapshot


class _Links:
    def __init__(self) -> None:
        self.value: RecordingHardwareLink | None = None
        self.key: str | None = None

    def publish(self, link, *, idempotency_key):
        if self.value is not None and (
            self.value != link or self.key != idempotency_key
        ):
            raise RuntimeError("link conflict")
        self.value = link
        self.key = idempotency_key
        return link

    def get(self, recording_id):
        if self.value is None or self.value.recording_id != recording_id:
            return None
        return self.value


def _fixture():
    view, ref = make_view(SegmentFixture(bytes(range(64)), 8))
    manifest = view.manifest
    hardware_ref = HardwareMetadataSnapshotRef(
        manifest.hardware_metadata_snapshot_id, Digest.sha256(b"hardware")
    )
    snapshot = HardwareMetadataSnapshot(
        SchemaRef(HardwareMetadataSnapshot.SCHEMA_ID),
        hardware_ref.snapshot_id,
        manifest.station_id,
        (manifest.radio_id,),
        tuple(
            ReceiverChainMetadata(
                receiver,
                manifest.radio_id,
                index,
                f"lnb-{index}",
                None,
                None,
                UtcNs(int(manifest.capture_started_utc_ns) - 1),
                UtcNs(int(manifest.capture_finished_utc_ns) + 1),
            )
            for index, receiver in enumerate(manifest.receiver_chain_ids)
        ),
    )
    recordings = type(
        "Recordings",
        (),
        {"get": lambda self, recording_id: PublishedRecordingRef(ref)},
    )()
    reader = type("Reader", (), {"open": lambda self, exact_ref: nullcontext(view)})()
    return view, ref, hardware_ref, snapshot, recordings, reader


def test_linker_resolves_id_to_exact_digest_and_verifies_full_assignment() -> None:
    view, ref, hardware_ref, snapshot, recordings, reader = _fixture()
    authority = _Authority(hardware_ref, snapshot)
    links = _Links()
    linker = RecordingHardwareLinker(recordings, reader, authority, authority, links)

    first = linker.link(ref.recording_id)
    second = linker.link(ref.recording_id)

    assert first == second
    assert first.recording_identity_digest == ref.identity_digest()
    assert first.hardware_snapshot_ref == hardware_ref
    assert authority.resolve_calls == [
        view.manifest.hardware_metadata_snapshot_id,
        view.manifest.hardware_metadata_snapshot_id,
    ]
    assert require_recording_hardware_link(links, ref.recording_id) == first


@pytest.mark.parametrize("failure", ["station", "radio", "interval", "receiver"])
def test_linker_rejects_hardware_that_does_not_authorize_recording(
    failure: str,
) -> None:
    view, ref, hardware_ref, snapshot, recordings, reader = _fixture()
    if failure == "station":
        snapshot = replace(snapshot, station_id=StationId("station_other"))
    elif failure == "radio":
        other = RadioId("radio_other")
        snapshot = replace(
            snapshot,
            radio_ids=(other,),
            receiver_chains=tuple(
                replace(chain, radio_id=other) for chain in snapshot.receiver_chains
            ),
        )
    elif failure == "interval":
        snapshot = replace(
            snapshot,
            receiver_chains=tuple(
                replace(
                    chain,
                    valid_until_utc_ns=view.manifest.capture_finished_utc_ns,
                )
                for chain in snapshot.receiver_chains
            ),
        )
        # Half-open hardware validity must extend beyond the last capture instant.
        snapshot = replace(
            snapshot,
            receiver_chains=(
                replace(
                    snapshot.receiver_chains[0],
                    valid_until_utc_ns=UtcNs(
                        int(view.manifest.capture_finished_utc_ns) - 1
                    ),
                ),
                snapshot.receiver_chains[1],
            ),
        )
    else:
        snapshot = replace(
            snapshot,
            receiver_chains=tuple(
                replace(chain, receiver_chain_id=ReceiverChainId(f"rx_other_{index}"))
                for index, chain in enumerate(snapshot.receiver_chains)
            ),
        )
    linker = RecordingHardwareLinker(
        recordings,
        reader,
        _Authority(hardware_ref, snapshot),
        _Authority(hardware_ref, snapshot),
        _Links(),
    )
    with pytest.raises(RecordingHardwareAuthorityError):
        linker.link(ref.recording_id)


def test_unlinked_legacy_recording_fails_closed() -> None:
    _, ref, _, _, _, _ = _fixture()
    with pytest.raises(RecordingHardwareAuthorityError, match="no authoritative"):
        require_recording_hardware_link(_Links(), ref.recording_id)


def test_hardware_link_contract_recomputes_linked_identity_digest() -> None:
    _, ref, hardware_ref, _, _, _ = _fixture()
    identity = {
        "recording_id": str(ref.recording_id),
        "recording_identity_digest": str(ref.identity_digest()),
        "hardware_snapshot_id": str(hardware_ref.snapshot_id),
        "hardware_snapshot_digest": str(hardware_ref.digest),
    }
    digest = canonical_digest(identity)
    link = RecordingHardwareLink(
        f"hwlink_{digest.value[:32]}",
        ref.recording_id,
        ref.identity_digest(),
        hardware_ref,
        digest,
    )

    with pytest.raises(ValueError, match="linked identities"):
        replace(link, recording_identity_digest=Digest.sha256(b"substituted"))
