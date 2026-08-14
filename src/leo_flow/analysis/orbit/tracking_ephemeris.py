"""Exact, offline-only orbit materialization for tracking analysis.

The public prediction boundary deliberately has no locator, snapshot-selection,
NORAD-selection, raw-recording, or provider capability.  Those choices are
closed once from an already verified :class:`TrackingInputSnapshot` and exact
archived normalized TLE objects.  A later estimator can therefore request only
an entry/carrier pair that was allow-listed during materialization.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Protocol

from leo_flow.analysis.ephemeris.normalization import (
    NormalizedTLE,
    TLEFormatError,
    decode_normalized_catalog,
)
from leo_flow.analysis.orbit.association import (
    OrbitPropagator,
    PropagatedState,
    PropagationSpecification,
    SatelliteCarrierHypothesis,
    StationGeometrySnapshot,
)
from leo_flow.analysis.orbit.sgp4_adapter import (
    Sgp4OrbitPropagator,
    sgp4_vallado_wgs72_specification,
)
from leo_flow.contracts.core import (
    V0_1,
    ArtifactRef,
    Digest,
    FeatureId,
    FeatureSetId,
    RecordingId,
    SchemaRef,
    UtcNs,
    canonical_digest,
    canonical_json_bytes,
)
from leo_flow.contracts.ephemeris import (
    EphemerisSelectionPolicy,
    EphemerisSnapshot,
    EphemerisSnapshotRef,
)
from leo_flow.contracts.features import Covariance
from leo_flow.contracts.ports import EphemerisReader
from leo_flow.contracts.tracking_input import (
    RF_MEASUREMENT_BASIS,
    RF_UNITS,
    TrackingInputEntry,
    TrackingInputSnapshot,
)

MAX_NORMALIZED_EPHEMERIS_BYTES = 16 * 1024 * 1024
CARRIER_HYPOTHESIS_SCHEMA_ID = "org.leo-flow.satellite-carrier-hypothesis"


class TrackingEphemerisMaterializationError(ValueError):
    """Exact archived evidence is missing, substituted, or internally inconsistent."""


class ArchivedEphemerisView(Protocol):
    """Narrow read view: metadata and normalized bytes, never provider access."""

    @property
    def snapshot(self) -> EphemerisSnapshot: ...

    def normalized_bytes(self) -> bytes: ...


class ExactArchivedEphemerisReader(Protocol):
    """Open only the archived snapshot named by an immutable reference."""

    def open(
        self, ref: EphemerisSnapshotRef
    ) -> AbstractContextManager[ArchivedEphemerisView]: ...


@dataclass(frozen=True)
class ReferencedCarrierHypothesis:
    """Content-identified carrier hypothesis; several may share one NORAD ID."""

    ref: ArtifactRef
    hypothesis: SatelliteCarrierHypothesis

    def __post_init__(self) -> None:
        expected = carrier_hypothesis_digest(self.hypothesis)
        if (
            self.ref.schema != SchemaRef(CARRIER_HYPOTHESIS_SCHEMA_ID, V0_1)
            or self.ref.digest != expected
            or self.ref.artifact_id != f"carrier_{expected.value[:32]}"
        ):
            raise ValueError("carrier hypothesis reference differs from its content")


def referenced_carrier_hypothesis(
    hypothesis: SatelliteCarrierHypothesis,
) -> ReferencedCarrierHypothesis:
    digest = carrier_hypothesis_digest(hypothesis)
    return ReferencedCarrierHypothesis(
        ArtifactRef(
            f"carrier_{digest.value[:32]}",
            digest,
            SchemaRef(CARRIER_HYPOTHESIS_SCHEMA_ID, V0_1),
        ),
        hypothesis,
    )


def carrier_hypothesis_digest(hypothesis: SatelliteCarrierHypothesis) -> Digest:
    return canonical_digest(
        {
            "schema": SchemaRef(CARRIER_HYPOTHESIS_SCHEMA_ID, V0_1),
            "norad_id": hypothesis.norad_id,
            "carrier_hz": hypothesis.carrier_hz,
            "carrier_variance_hz2": hypothesis.carrier_variance_hz2,
        }
    )


@dataclass(frozen=True)
class MaterializedEphemerisSnapshotEvidence:
    snapshot_ref: EphemerisSnapshotRef
    scope: str
    retrieved_at_utc_ns: UtcNs
    parser_ref: ArtifactRef
    validation_policy_ref: ArtifactRef
    satellite_count: int
    norad_id_set_digest: Digest
    element_epoch_min_utc_ns: UtcNs
    element_epoch_max_utc_ns: UtcNs
    selected_norad_ids: tuple[int, ...]
    link_digests: tuple[Digest, ...]
    selection_policy_refs: tuple[ArtifactRef, ...]


@dataclass(frozen=True)
class TrackingEphemerisMaterializationEvidence:
    tracking_input_snapshot_digest: Digest
    tracking_input_membership_digest: Digest
    snapshots: tuple[MaterializedEphemerisSnapshotEvidence, ...]
    carrier_hypothesis_refs: tuple[ArtifactRef, ...]
    propagation: PropagationSpecification
    materialization_digest: Digest

    def __post_init__(self) -> None:
        expected = canonical_digest(
            {
                "tracking_input_snapshot_digest": self.tracking_input_snapshot_digest,
                "tracking_input_membership_digest": self.tracking_input_membership_digest,
                "snapshots": self.snapshots,
                "carrier_hypothesis_refs": self.carrier_hypothesis_refs,
                "propagation": self.propagation,
            }
        )
        if self.materialization_digest != expected:
            raise ValueError("tracking ephemeris materialization digest differs")


@dataclass(frozen=True)
class TrackingPredictionRequest:
    """An allow-listed frozen entry/carrier pair plus exact station geometry."""

    feature_set_id: FeatureSetId
    feature_id: FeatureId
    carrier_hypothesis_ref: ArtifactRef
    station: StationGeometrySnapshot


@dataclass(frozen=True)
class TrackingOrbitPrediction:
    feature_set_id: FeatureSetId
    feature_id: FeatureId
    recording_id: RecordingId
    midpoint_utc_ns: UtcNs
    carrier_hypothesis_ref: ArtifactRef
    norad_id: int
    snapshot_ref: EphemerisSnapshotRef
    ephemeris_link_digest: Digest
    selection_policy_ref: ArtifactRef
    predicted_frequency_hz: float
    predicted_drift_hz_s: float
    covariance: Covariance
    elevation_deg: float
    error_code: str | None = None


@dataclass(frozen=True)
class _LoadedSnapshot:
    evidence: MaterializedEphemerisSnapshotEvidence
    normalized_bytes: bytes


@dataclass(frozen=True)
class _EntryAuthority:
    entry: TrackingInputEntry
    snapshot_ref: EphemerisSnapshotRef


@dataclass(frozen=True)
class _SealedEphemerisView:
    ref: EphemerisSnapshotRef
    payload: bytes

    def normalized_bytes(self) -> bytes:
        return self.payload


class _SealedEphemerisReader(EphemerisReader):
    """Private exact-byte reader created only after archive verification."""

    def __init__(self, loaded: tuple[_LoadedSnapshot, ...]) -> None:
        self._payloads = {
            item.evidence.snapshot_ref: item.normalized_bytes for item in loaded
        }

    @contextmanager
    def open(self, ref: EphemerisSnapshotRef) -> Iterator[_SealedEphemerisView]:
        try:
            payload = self._payloads[ref]
        except KeyError as error:
            raise LookupError(
                "ephemeris reference is outside the sealed allow-list"
            ) from error
        yield _SealedEphemerisView(ref, payload)


class TrackingEphemerisPredictor(Protocol):
    """Read-only estimator port with no archive or selection capability."""

    @property
    def evidence(self) -> TrackingEphemerisMaterializationEvidence: ...

    def predict(
        self, request: TrackingPredictionRequest
    ) -> TrackingOrbitPrediction: ...


class _OfflineTrackingPredictor:
    """Predict only frozen entry/carrier pairs with one sealed orbit adapter."""

    def __init__(
        self,
        *,
        evidence: TrackingEphemerisMaterializationEvidence,
        entries: dict[tuple[FeatureSetId, FeatureId], _EntryAuthority],
        carriers: dict[ArtifactRef, SatelliteCarrierHypothesis],
        propagator: OrbitPropagator,
    ) -> None:
        self._evidence = evidence
        self._entries = entries
        self._carriers = carriers
        self._propagator = propagator
        self._state_cache: dict[
            tuple[EphemerisSnapshotRef, int, UtcNs, Digest], PropagatedState
        ] = {}

    @property
    def evidence(self) -> TrackingEphemerisMaterializationEvidence:
        return self._evidence

    def predict(self, request: TrackingPredictionRequest) -> TrackingOrbitPrediction:
        try:
            authority = self._entries[(request.feature_set_id, request.feature_id)]
        except KeyError as error:
            raise LookupError(
                "tracking entry is outside the materialized allow-list"
            ) from error
        try:
            carrier = self._carriers[request.carrier_hypothesis_ref]
        except KeyError as error:
            raise LookupError(
                "carrier hypothesis is outside the materialized allow-list"
            ) from error

        entry = authority.entry
        if request.station.station_id != entry.calibration.station_id:
            raise TrackingEphemerisMaterializationError(
                "station geometry differs from frozen receiver calibration"
            )
        instant = entry.measurement.midpoint_utc_ns
        state_key = (
            authority.snapshot_ref,
            carrier.norad_id,
            instant,
            request.station.digest,
        )
        state = self._state_cache.get(state_key)
        if state is None:
            state = self._propagator.propagate(
                authority.snapshot_ref,
                request.station,
                self._evidence.propagation,
                carrier.norad_id,
                instant,
            )
            if state.norad_id != carrier.norad_id or state.utc_ns != instant:
                raise TrackingEphemerisMaterializationError(
                    "orbit propagator substituted prediction identity"
                )
            self._state_cache[state_key] = state

        speed = self._evidence.propagation.speed_of_light_m_s
        frequency_factor = 1.0 - state.range_rate_m_s / speed
        drift_factor = -state.range_acceleration_m_s2 / speed
        predicted_frequency = carrier.carrier_hz * frequency_factor
        predicted_drift = carrier.carrier_hz * drift_factor
        base = entry.prediction.covariance.values
        carrier_variance = carrier.carrier_variance_hz2
        covariance = Covariance(
            RF_MEASUREMENT_BASIS,
            RF_UNITS,
            (
                (
                    base[0][0] + carrier_variance * frequency_factor**2,
                    base[0][1] + carrier_variance * frequency_factor * drift_factor,
                ),
                (
                    base[1][0] + carrier_variance * drift_factor * frequency_factor,
                    base[1][1] + carrier_variance * drift_factor**2,
                ),
            ),
        )
        for value in (predicted_frequency, predicted_drift):
            if not math.isfinite(value):
                raise TrackingEphemerisMaterializationError(
                    "orbit prediction produced a non-finite RF value"
                )
        link = entry.ephemeris_link
        return TrackingOrbitPrediction(
            request.feature_set_id,
            request.feature_id,
            entry.measurement.recording_id,
            instant,
            request.carrier_hypothesis_ref,
            carrier.norad_id,
            authority.snapshot_ref,
            link.link_digest,
            link.selection.policy_ref,
            predicted_frequency,
            predicted_drift,
            covariance,
            state.elevation_deg,
            state.error_code,
        )


def materialize_tracking_ephemerides(
    tracking_input: TrackingInputSnapshot,
    carrier_hypotheses: tuple[ReferencedCarrierHypothesis, ...],
    archives: ExactArchivedEphemerisReader,
) -> TrackingEphemerisPredictor:
    """Close exact archive evidence and construct the pinned offline predictor."""

    if not carrier_hypotheses:
        raise TrackingEphemerisMaterializationError(
            "tracking ephemeris materialization requires carrier hypotheses"
        )
    ordered_carriers = tuple(sorted(carrier_hypotheses, key=_carrier_key))
    if ordered_carriers != carrier_hypotheses:
        raise TrackingEphemerisMaterializationError(
            "carrier hypotheses are not in canonical order"
        )
    carrier_refs = tuple(item.ref for item in ordered_carriers)
    if len(set(carrier_refs)) != len(carrier_refs):
        raise TrackingEphemerisMaterializationError(
            "carrier hypothesis references are duplicated"
        )
    # Different hypotheses for one satellite remain separate.  Only the orbit
    # lookup is shared by NORAD ID.
    required_norad_ids = tuple(
        sorted({item.hypothesis.norad_id for item in ordered_carriers})
    )

    links_by_recording = _exact_recording_links(tracking_input)
    links_by_ref: dict[EphemerisSnapshotRef, list[TrackingInputEntry]] = {}
    refs_by_id: dict[object, EphemerisSnapshotRef] = {}
    for entry in links_by_recording.values():
        ref = entry.ephemeris_link.selection.snapshot_ref
        prior = refs_by_id.setdefault(ref.snapshot_id, ref)
        if prior != ref:
            raise TrackingEphemerisMaterializationError(
                "one ephemeris snapshot ID has conflicting exact references"
            )
        links_by_ref.setdefault(ref, []).append(entry)

    loaded: list[_LoadedSnapshot] = []
    for ref in sorted(links_by_ref, key=_snapshot_ref_key):
        loaded.append(
            _load_exact_snapshot(
                ref, tuple(links_by_ref[ref]), required_norad_ids, archives
            )
        )

    propagation = sgp4_vallado_wgs72_specification()
    snapshots = tuple(item.evidence for item in loaded)
    materialization_digest = canonical_digest(
        {
            "tracking_input_snapshot_digest": tracking_input.snapshot_digest,
            "tracking_input_membership_digest": tracking_input.membership_digest,
            "snapshots": snapshots,
            "carrier_hypothesis_refs": carrier_refs,
            "propagation": propagation,
        }
    )
    evidence = TrackingEphemerisMaterializationEvidence(
        tracking_input.snapshot_digest,
        tracking_input.membership_digest,
        snapshots,
        carrier_refs,
        propagation,
        materialization_digest,
    )
    entries = {
        (
            entry.feature_set.feature_set_id,
            entry.measurement.feature_id,
        ): _EntryAuthority(entry, entry.ephemeris_link.selection.snapshot_ref)
        for entry in tracking_input.entries
    }
    carriers = {item.ref: item.hypothesis for item in ordered_carriers}
    sealed_reader = _SealedEphemerisReader(tuple(loaded))
    return _OfflineTrackingPredictor(
        evidence=evidence,
        entries=entries,
        carriers=carriers,
        propagator=Sgp4OrbitPropagator(sealed_reader),
    )


def _exact_recording_links(
    tracking_input: TrackingInputSnapshot,
) -> dict[RecordingId, TrackingInputEntry]:
    result: dict[RecordingId, TrackingInputEntry] = {}
    seen_link_ids: dict[str, Digest] = {}
    for entry in tracking_input.entries:
        link = entry.ephemeris_link
        try:
            exact_link = type(link)(
                link.link_id,
                link.recording_id,
                link.recording_identity_digest,
                link.recording_interval,
                link.scope,
                link.selection,
                link.link_digest,
            )
        except ValueError as error:
            raise TrackingEphemerisMaterializationError(
                "frozen ephemeris link identity is invalid"
            ) from error
        if exact_link.selection.policy is EphemerisSelectionPolicy.BEST_EPHEMERIS:
            raise TrackingEphemerisMaterializationError(
                "best_ephemeris is not a frozen time-selection policy"
            )
        if exact_link.selection.policy_ref.schema is None:
            raise TrackingEphemerisMaterializationError(
                "ephemeris time-selection policy is not schema-bearing"
            )
        prior_digest = seen_link_ids.setdefault(
            exact_link.link_id, exact_link.link_digest
        )
        if prior_digest != exact_link.link_digest:
            raise TrackingEphemerisMaterializationError(
                "ephemeris link ID identifies conflicting evidence"
            )
        prior = result.setdefault(exact_link.recording_id, entry)
        if prior.ephemeris_link != exact_link:
            raise TrackingEphemerisMaterializationError(
                "one recording has conflicting ephemeris links"
            )
    return result


def _load_exact_snapshot(
    ref: EphemerisSnapshotRef,
    entries: tuple[TrackingInputEntry, ...],
    required_norad_ids: tuple[int, ...],
    archives: ExactArchivedEphemerisReader,
) -> _LoadedSnapshot:
    try:
        with archives.open(ref) as view:
            snapshot = view.snapshot
            payload = view.normalized_bytes()
    except (KeyError, LookupError) as error:
        raise TrackingEphemerisMaterializationError(
            "exact archived ephemeris snapshot is missing"
        ) from error
    if _snapshot_ref(snapshot) != ref:
        raise TrackingEphemerisMaterializationError(
            "archived ephemeris reader substituted snapshot identity"
        )
    scopes = {entry.ephemeris_link.scope for entry in entries}
    sources = {entry.ephemeris_link.selection.source for entry in entries}
    if scopes != {snapshot.scope} or sources != {snapshot.source}:
        raise TrackingEphemerisMaterializationError(
            "archived ephemeris source or scope differs from frozen links"
        )
    _verify_archive_metadata(snapshot, payload)
    normalized = _decode_exact_normalized(payload, snapshot)
    available = {item.norad_id for item in normalized}
    missing = tuple(item for item in required_norad_ids if item not in available)
    if missing:
        raise TrackingEphemerisMaterializationError(
            f"carrier NORAD IDs are absent from exact snapshot: {missing}"
        )
    for entry in entries:
        _verify_temporal_policy(entry, snapshot)
    links = tuple(
        sorted({entry.ephemeris_link.link_digest for entry in entries}, key=str)
    )
    policies = tuple(
        sorted(
            {entry.ephemeris_link.selection.policy_ref for entry in entries},
            key=_artifact_key,
        )
    )
    evidence = MaterializedEphemerisSnapshotEvidence(
        ref,
        snapshot.scope,
        snapshot.retrieved_at_utc_ns,
        snapshot.parser_ref,
        snapshot.validation.policy_ref,
        snapshot.satellite_count,
        snapshot.norad_id_set_digest,
        snapshot.element_epoch_min_utc_ns,
        snapshot.element_epoch_max_utc_ns,
        required_norad_ids,
        links,
        policies,
    )
    return _LoadedSnapshot(evidence, payload)


def _verify_archive_metadata(snapshot: EphemerisSnapshot, payload: bytes) -> None:
    try:
        EphemerisSnapshot(
            snapshot.schema,
            snapshot.snapshot_id,
            snapshot.retrieval_id,
            snapshot.source,
            snapshot.scope,
            snapshot.retrieved_at_utc_ns,
            snapshot.raw_object_ref,
            snapshot.normalized_object_ref,
            snapshot.parser_ref,
            snapshot.satellite_count,
            snapshot.norad_id_set_digest,
            snapshot.element_epoch_min_utc_ns,
            snapshot.element_epoch_max_utc_ns,
            snapshot.validation,
            snapshot.attribution,
        )
    except ValueError as error:
        raise TrackingEphemerisMaterializationError(
            "archived ephemeris snapshot contract is invalid"
        ) from error
    normalized_ref = snapshot.normalized_object_ref
    if (
        normalized_ref.media_type != "application/json"
        or normalized_ref.format_id != "tle-normalized-v1"
        or not 0 < normalized_ref.byte_count <= MAX_NORMALIZED_EPHEMERIS_BYTES
        or len(payload) != normalized_ref.byte_count
        or Digest.sha256(payload) != normalized_ref.digest
    ):
        raise TrackingEphemerisMaterializationError(
            "archived normalized ephemeris bytes or metadata differ"
        )
    if (
        snapshot.parser_ref.schema is None
        or snapshot.validation.policy_ref.schema is None
    ):
        raise TrackingEphemerisMaterializationError(
            "ephemeris parser and validation policy must be schema-bearing"
        )


def _decode_exact_normalized(
    payload: bytes, snapshot: EphemerisSnapshot
) -> tuple[NormalizedTLE, ...]:
    try:
        document = json.loads(payload)
        if not isinstance(document, dict) or set(document) != {
            "schema",
            "version",
            "source",
            "scope",
            "entries",
        }:
            raise TLEFormatError("normalized catalog fields differ")
        if document["scope"] != snapshot.scope:
            raise TLEFormatError("normalized catalog scope differs")
        entries = decode_normalized_catalog(payload, snapshot.source)
        expected_document = {
            "schema": "org.leo-flow.normalized-tle-catalog",
            "version": "1.0",
            "source": snapshot.source.value,
            "scope": snapshot.scope,
            "entries": [
                {
                    "norad_id": entry.norad_id,
                    "name": entry.name,
                    "line1": entry.line1,
                    "line2": entry.line2,
                    "epoch_utc_ns": int(entry.epoch_utc_ns),
                }
                for entry in entries
            ],
        }
        if canonical_json_bytes(expected_document) != payload:
            raise TLEFormatError("normalized catalog has ambiguous or extra fields")
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TLEFormatError,
        ValueError,
    ) as error:
        raise TrackingEphemerisMaterializationError(
            "normalized ephemeris schema, source, scope, or TLE data differ"
        ) from error
    ids = [entry.norad_id for entry in entries]
    epochs = [entry.epoch_utc_ns for entry in entries]
    if (
        snapshot.satellite_count != len(entries)
        or snapshot.norad_id_set_digest != Digest.sha256(canonical_json_bytes(ids))
        or snapshot.element_epoch_min_utc_ns != min(epochs)
        or snapshot.element_epoch_max_utc_ns != max(epochs)
    ):
        raise TrackingEphemerisMaterializationError(
            "normalized catalog projection differs from archived snapshot"
        )
    return entries


def _verify_temporal_policy(
    entry: TrackingInputEntry, snapshot: EphemerisSnapshot
) -> None:
    selection = entry.ephemeris_link.selection
    retrieved = int(snapshot.retrieved_at_utc_ns)
    if retrieved > int(selection.as_of_utc_ns):
        raise TrackingEphemerisMaterializationError(
            "ephemeris snapshot was not available at the frozen as-of time"
        )
    interval = entry.recording_interval
    if selection.policy is EphemerisSelectionPolicy.AVAILABLE_THEN and retrieved > int(
        interval.started_utc_ns
    ):
        raise TrackingEphemerisMaterializationError(
            "available_then snapshot was retrieved after recording start"
        )
    if selection.policy is EphemerisSelectionPolicy.FIRST_AFTER and retrieved <= int(
        interval.finished_utc_ns
    ):
        raise TrackingEphemerisMaterializationError(
            "first_after snapshot was not retrieved after recording finish"
        )


def _snapshot_ref(snapshot: EphemerisSnapshot) -> EphemerisSnapshotRef:
    return EphemerisSnapshotRef(
        snapshot.snapshot_id,
        snapshot.source,
        snapshot.raw_object_ref.digest,
        snapshot.normalized_object_ref.digest,
    )


def _carrier_key(
    item: ReferencedCarrierHypothesis,
) -> tuple[int, str, str]:
    return item.hypothesis.norad_id, item.ref.artifact_id, str(item.ref.digest)


def _snapshot_ref_key(ref: EphemerisSnapshotRef) -> tuple[str, str, str, str]:
    return (
        ref.source.value,
        str(ref.snapshot_id),
        str(ref.raw_digest),
        str(ref.normalized_digest),
    )


def _artifact_key(ref: ArtifactRef) -> tuple[str, str, str]:
    schema = (
        "" if ref.schema is None else f"{ref.schema.schema_id}/{ref.schema.version}"
    )
    return ref.artifact_id, str(ref.digest), schema
