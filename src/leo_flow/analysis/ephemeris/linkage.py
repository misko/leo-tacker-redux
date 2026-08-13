"""Recording-to-ephemeris linkage consumed by cross-recording analysis."""

from __future__ import annotations

from dataclasses import dataclass

from leo_flow.contracts.core import ArtifactRef, RecordingId, UtcNs
from leo_flow.contracts.ephemeris import (
    EphemerisSelection,
    EphemerisSelectionPolicy,
    EphemerisSource,
    RecordingInterval,
)
from leo_flow.contracts.storage import ObjectRef

from .catalog import EphemerisSnapshotCatalog
from .resolver import TemporalEphemerisResolver


@dataclass(frozen=True)
class RecordingEphemerisInput:
    """Stable second-stage input: exact normalized bytes plus selection audit."""

    recording_id: RecordingId
    recording_interval: RecordingInterval
    selection: EphemerisSelection
    normalized_object_ref: ObjectRef
    provenance_object_ref: ObjectRef


def resolve_recording_ephemeris(
    *,
    catalog: EphemerisSnapshotCatalog,
    recording_id: RecordingId,
    recording_interval: RecordingInterval,
    source: EphemerisSource,
    scope: str,
    policy: EphemerisSelectionPolicy,
    policy_ref: ArtifactRef,
    as_of_utc_ns: UtcNs,
) -> RecordingEphemerisInput:
    selection = TemporalEphemerisResolver(catalog.history(source, scope), policy).resolve(
        source, recording_interval, policy_ref, as_of_utc_ns
    )
    archived = catalog.get(selection.snapshot_ref.snapshot_id)
    if archived is None:
        raise RuntimeError("catalog history referenced a missing snapshot")
    return RecordingEphemerisInput(
        recording_id,
        recording_interval,
        selection,
        archived.snapshot.normalized_object_ref,
        archived.provenance_object_ref,
    )
