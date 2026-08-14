from __future__ import annotations

import ast
import inspect
import json
from contextlib import nullcontext
from dataclasses import dataclass, replace
from importlib.util import find_spec
from pathlib import Path

import pytest

import leo_flow.analysis.orbit.tracking_ephemeris as materialization_module
from leo_flow.analysis.ephemeris.normalization import NormalizedTLE, parse_tle_catalog
from leo_flow.analysis.orbit.association import (
    SatelliteCarrierHypothesis,
    StationGeometrySnapshot,
)
from leo_flow.analysis.orbit.tracking_ephemeris import (
    ExactArchivedEphemerisReader,
    TrackingEphemerisMaterializationError,
    TrackingPredictionRequest,
    materialize_tracking_ephemerides,
    referenced_carrier_hypothesis,
)
from leo_flow.contracts.core import (
    V0_1,
    ArtifactRef,
    Digest,
    EphemerisRetrievalId,
    EphemerisSnapshotId,
    SchemaRef,
    StationId,
    UtcNs,
    canonical_digest,
    canonical_json_bytes,
)
from leo_flow.contracts.ephemeris import (
    EphemerisSelection,
    EphemerisSelectionPolicy,
    EphemerisSnapshot,
    EphemerisSnapshotRef,
    EphemerisSource,
    RecordingEphemerisLink,
    ValidationResult,
)
from leo_flow.contracts.storage import ObjectRef
from leo_flow.contracts.tracking_input import (
    TrackingInputSnapshot,
    tracking_input_membership_digest,
    tracking_input_snapshot_digest,
)
from tests.model_analysis.test_tracking_input_builder import _case

SGP4_FIXTURE = Path(__file__).with_name("fixtures") / "sgp4_vallado_reference.json"
pytestmark = pytest.mark.skipif(
    find_spec("sgp4") is None,
    reason="tracking ephemeris verification requires the optional orbit extra",
)


def _digest(label: str) -> Digest:
    return Digest.sha256(label.encode())


def _artifact(label: str) -> ArtifactRef:
    return ArtifactRef(label, _digest(label), SchemaRef(f"org.leo-flow.{label}", V0_1))


def _tle() -> NormalizedTLE:
    document = json.loads(SGP4_FIXTURE.read_text())
    satellite = document["reference_satellite"]
    assert isinstance(satellite, dict)
    return parse_tle_catalog(f"{satellite['line1']}\n{satellite['line2']}\n".encode())[
        0
    ]


def _normalized(
    entries: tuple[NormalizedTLE, ...],
    *,
    source: str = EphemerisSource.SPACE_TRACK.value,
    scope: str = "active-leo",
    schema: str = "org.leo-flow.normalized-tle-catalog",
) -> bytes:
    return canonical_json_bytes(
        {
            "schema": schema,
            "version": "1.0",
            "source": source,
            "scope": scope,
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
    )


@dataclass(frozen=True)
class _View:
    snapshot: EphemerisSnapshot
    payload: bytes

    def normalized_bytes(self) -> bytes:
        return self.payload


class _Reader(ExactArchivedEphemerisReader):
    def __init__(self, values: dict[EphemerisSnapshotRef, _View]) -> None:
        self.values = values
        self.calls: list[EphemerisSnapshotRef] = []

    def open(self, ref: EphemerisSnapshotRef):  # type: ignore[no-untyped-def]
        self.calls.append(ref)
        try:
            return nullcontext(self.values[ref])
        except KeyError as error:
            raise LookupError("missing exact snapshot") from error


def _archive(
    payload: bytes,
    *,
    locator: str = "cas://normalized/one",
    retrieved_at: int = 900,
    source: EphemerisSource = EphemerisSource.SPACE_TRACK,
    scope: str = "active-leo",
    raw_digest: Digest | None = None,
    media_type: str = "application/json",
    format_id: str = "tle-normalized-v1",
) -> tuple[EphemerisSnapshotRef, _View]:
    entries = parse_entries(payload, source)
    normalized_ref = ObjectRef(
        Digest.sha256(payload),
        len(payload),
        media_type,
        format_id,
        locator,
    )
    raw_ref = ObjectRef(
        raw_digest or _digest("raw-exact"),
        100,
        "text/plain",
        "tle-raw-v1",
        "cas://raw/never-opened",
    )
    snapshot = EphemerisSnapshot(
        SchemaRef(EphemerisSnapshot.SCHEMA_ID, V0_1),
        EphemerisSnapshotId("eph_materialized_exact"),
        EphemerisRetrievalId("ephret_materialized_exact"),
        source,
        scope,
        UtcNs(retrieved_at),
        raw_ref,
        normalized_ref,
        _artifact("tle-parser-v1"),
        len(entries),
        Digest.sha256(canonical_json_bytes([entry.norad_id for entry in entries])),
        min(entry.epoch_utc_ns for entry in entries),
        max(entry.epoch_utc_ns for entry in entries),
        ValidationResult(True, _artifact("tle-validation-v1")),
        "test fixture",
    )
    ref = EphemerisSnapshotRef(
        snapshot.snapshot_id,
        snapshot.source,
        raw_ref.digest,
        normalized_ref.digest,
    )
    return ref, _View(snapshot, payload)


def parse_entries(payload: bytes, source: EphemerisSource) -> tuple[NormalizedTLE, ...]:
    # Fixture construction intentionally tolerates malformed catalog metadata so
    # the materializer, not this helper, owns those rejection tests.
    document = json.loads(payload)
    values = document["entries"]
    assert isinstance(values, list)
    result = []
    for value in values:
        assert isinstance(value, dict)
        result.append(
            NormalizedTLE(
                int(value["norad_id"]),
                None if value["name"] is None else str(value["name"]),
                str(value["line1"]),
                str(value["line2"]),
                UtcNs(int(value["epoch_utc_ns"])),
            )
        )
    del source
    return tuple(result)


def _link(
    ref: EphemerisSnapshotRef,
    *,
    policy: EphemerisSelectionPolicy = EphemerisSelectionPolicy.AVAILABLE_THEN,
) -> RecordingEphemerisLink:
    original = _case().source.ephemeris_link
    selection = EphemerisSelection(
        ref.source,
        policy,
        original.selection.policy_ref,
        ref,
        original.selection.as_of_utc_ns,
    )
    identity = {
        "recording_identity_digest": str(original.recording_identity_digest),
        "recording_interval": original.recording_interval,
        "source": selection.source.value,
        "scope": original.scope,
        "policy": selection.policy.value,
        "policy_ref": selection.policy_ref,
        "as_of_utc_ns": selection.as_of_utc_ns,
        "snapshot_ref": selection.snapshot_ref,
    }
    digest = canonical_digest(identity)
    return RecordingEphemerisLink(
        f"ephlink_{digest.value[:32]}",
        original.recording_id,
        original.recording_identity_digest,
        original.recording_interval,
        original.scope,
        selection,
        digest,
    )


def _tracking_input(
    ref: EphemerisSnapshotRef,
    *,
    policy: EphemerisSelectionPolicy = EphemerisSelectionPolicy.AVAILABLE_THEN,
) -> TrackingInputSnapshot:
    case = _case()
    original = case.freeze()
    entries = tuple(
        replace(entry, ephemeris_link=_link(ref, policy=policy))
        for entry in original.entries
    )
    membership = tracking_input_membership_digest(entries)
    provenance = replace(
        original.provenance,
        input_digests=(original.durable_dataset.snapshot_digest, membership),
    )
    snapshot_digest = tracking_input_snapshot_digest(
        original.schema,
        original.durable_dataset,
        original.builder_ref,
        original.selector_ref,
        provenance,
        entries,
        membership,
    )
    return TrackingInputSnapshot(
        original.schema,
        f"trackinput_{snapshot_digest.value[:32]}",
        original.durable_dataset,
        original.builder_ref,
        original.selector_ref,
        provenance,
        entries,
        membership,
        snapshot_digest,
    )


def _carriers(
    *frequencies: float,
    norad_id: int | None = None,
) -> tuple[object, ...]:
    actual_norad = norad_id or _tle().norad_id
    values = tuple(
        referenced_carrier_hypothesis(
            SatelliteCarrierHypothesis(actual_norad, frequency, 4.0)
        )
        for frequency in frequencies
    )
    return tuple(
        sorted(
            values,
            key=lambda item: (
                item.hypothesis.norad_id,
                item.ref.artifact_id,
                str(item.ref.digest),
            ),
        )
    )


def _station() -> StationGeometrySnapshot:
    identity = {
        "station_id": "station_tracking",
        "frame": "ITRF",
        "position_m": (6_378_135.0, 0.0, 0.0),
    }
    return StationGeometrySnapshot(
        StationId("station_tracking"),
        "ITRF",
        (6_378_135.0, 0.0, 0.0),
        canonical_digest(identity),
    )


def test_exact_materialization_and_multiple_carriers_share_orbit_not_identity() -> None:
    payload = _normalized((_tle(),))
    ref, view = _archive(payload)
    tracking = _tracking_input(ref)
    carriers = _carriers(1_000_000_000.0, 1_100_000_000.0)
    reader = _Reader({ref: view})

    predictor = materialize_tracking_ephemerides(tracking, carriers, reader)  # type: ignore[arg-type]
    entry = tracking.entries[0]
    predictions = tuple(
        predictor.predict(
            TrackingPredictionRequest(
                entry.feature_set.feature_set_id,
                entry.measurement.feature_id,
                carrier.ref,
                _station(),
            )
        )
        for carrier in carriers  # type: ignore[union-attr]
    )

    assert reader.calls == [ref]
    assert predictor.evidence.snapshots[0].selected_norad_ids == (_tle().norad_id,)
    assert predictions[0].norad_id == predictions[1].norad_id == _tle().norad_id
    assert (
        predictions[0].carrier_hypothesis_ref != predictions[1].carrier_hypothesis_ref
    )
    assert (
        predictions[0].predicted_frequency_hz != predictions[1].predicted_frequency_hz
    )
    assert predictions[0].snapshot_ref == predictions[1].snapshot_ref == ref
    assert predictions[0].covariance.basis == (
        "frequency_hz",
        "drift_hz_s",
    )
    assert predictions[0].ephemeris_link_digest == entry.ephemeris_link.link_digest


def test_relocated_archive_has_the_same_materialization_identity() -> None:
    payload = _normalized((_tle(),))
    ref, first_view = _archive(payload, locator="file:///old")
    same_ref, relocated_view = _archive(payload, locator="s3://new")
    assert same_ref == ref
    tracking = _tracking_input(ref)
    carriers = _carriers(1_000_000_000.0)

    first = materialize_tracking_ephemerides(
        tracking, carriers, _Reader({ref: first_view})
    )  # type: ignore[arg-type]
    relocated = materialize_tracking_ephemerides(
        tracking,
        carriers,
        _Reader({ref: relocated_view}),  # type: ignore[arg-type]
    )

    assert first.evidence == relocated.evidence
    assert (
        first.evidence.materialization_digest
        == relocated.evidence.materialization_digest
    )


def test_only_referenced_snapshot_is_opened_and_prediction_is_allow_listed() -> None:
    payload = _normalized((_tle(),))
    ref, view = _archive(payload)
    other_ref = replace(ref, snapshot_id=EphemerisSnapshotId("eph_unreferenced"))
    reader = _Reader({ref: view, other_ref: view})
    tracking = _tracking_input(ref)
    carriers = _carriers(1_000_000_000.0)

    predictor = materialize_tracking_ephemerides(tracking, carriers, reader)  # type: ignore[arg-type]

    assert reader.calls == [ref]
    entry = tracking.entries[0]
    unknown = referenced_carrier_hypothesis(
        SatelliteCarrierHypothesis(_tle().norad_id, 1_200_000_000.0, 4.0)
    )
    with pytest.raises(LookupError, match="carrier hypothesis"):
        predictor.predict(
            TrackingPredictionRequest(
                entry.feature_set.feature_set_id,
                entry.measurement.feature_id,
                unknown.ref,
                _station(),
            )
        )


def test_missing_and_substituted_archived_snapshots_fail_closed() -> None:
    payload = _normalized((_tle(),))
    ref, view = _archive(payload)
    tracking = _tracking_input(ref)
    carriers = _carriers(1_000_000_000.0)

    with pytest.raises(TrackingEphemerisMaterializationError, match="missing"):
        materialize_tracking_ephemerides(tracking, carriers, _Reader({}))  # type: ignore[arg-type]

    substituted_snapshot = replace(
        view.snapshot, snapshot_id=EphemerisSnapshotId("eph_substituted")
    )
    with pytest.raises(TrackingEphemerisMaterializationError, match="substituted"):
        materialize_tracking_ephemerides(
            tracking,
            carriers,
            _Reader({ref: replace(view, snapshot=substituted_snapshot)}),  # type: ignore[arg-type]
        )
    with pytest.raises(
        TrackingEphemerisMaterializationError, match="bytes or metadata"
    ):
        materialize_tracking_ephemerides(
            tracking,
            carriers,
            _Reader({ref: replace(view, payload=payload + b"\n")}),  # type: ignore[arg-type]
        )


def test_tampered_frozen_link_is_revalidated_before_archive_access() -> None:
    payload = _normalized((_tle(),))
    ref, view = _archive(payload)
    tracking = _tracking_input(ref)
    object.__setattr__(
        tracking.entries[0].ephemeris_link,
        "link_digest",
        _digest("tampered-link"),
    )
    reader = _Reader({ref: view})

    with pytest.raises(TrackingEphemerisMaterializationError, match="link identity"):
        materialize_tracking_ephemerides(
            tracking,
            _carriers(1_000_000_000.0),
            reader,  # type: ignore[arg-type]
        )

    assert reader.calls == []


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("schema", "schema, source, scope"),
        ("source", "schema, source, scope"),
        ("scope", "schema, source, scope"),
        ("duplicate", "schema, source, scope"),
        ("projection", "projection"),
    ],
)
def test_schema_source_scope_duplicates_and_projection_are_verified(
    change: str, message: str
) -> None:
    entry = _tle()
    entries = (entry, entry) if change == "duplicate" else (entry,)
    payload = _normalized(
        entries,
        schema="wrong" if change == "schema" else "org.leo-flow.normalized-tle-catalog",
        source="huggingface" if change == "source" else "space-track",
        scope="wrong-scope" if change == "scope" else "active-leo",
    )
    ref, view = _archive(payload)
    if change == "projection":
        view = replace(
            view,
            snapshot=replace(view.snapshot, satellite_count=2),
        )
    tracking = _tracking_input(ref)

    with pytest.raises(TrackingEphemerisMaterializationError, match=message):
        materialize_tracking_ephemerides(
            tracking,
            _carriers(1_000_000_000.0),
            _Reader({ref: view}),  # type: ignore[arg-type]
        )


def test_missing_norad_and_invalid_time_selection_policy_fail_closed() -> None:
    payload = _normalized((_tle(),))
    ref, view = _archive(payload)
    with pytest.raises(TrackingEphemerisMaterializationError, match="NORAD IDs"):
        materialize_tracking_ephemerides(
            _tracking_input(ref),
            _carriers(1_000_000_000.0, norad_id=99_999),
            _Reader({ref: view}),  # type: ignore[arg-type]
        )

    late_ref, late_view = _archive(payload, retrieved_at=1_500)
    with pytest.raises(TrackingEphemerisMaterializationError, match="recording start"):
        materialize_tracking_ephemerides(
            _tracking_input(late_ref),
            _carriers(1_000_000_000.0),
            _Reader({late_ref: late_view}),  # type: ignore[arg-type]
        )
    with pytest.raises(TrackingEphemerisMaterializationError, match="recording finish"):
        materialize_tracking_ephemerides(
            _tracking_input(late_ref, policy=EphemerisSelectionPolicy.FIRST_AFTER),
            _carriers(1_000_000_000.0),
            _Reader({late_ref: late_view}),  # type: ignore[arg-type]
        )


def test_public_boundary_has_no_network_latest_locator_norad_or_raw_capture_input() -> (
    None
):
    source = inspect.getsource(materialization_module)
    tree = ast.parse(source)
    imported_roots = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert imported_roots.isdisjoint(
        {"requests", "httpx", "urllib", "socket", "boto3", "huggingface_hub"}
    )
    assert set(TrackingPredictionRequest.__dataclass_fields__) == {
        "feature_set_id",
        "feature_id",
        "carrier_hypothesis_ref",
        "station",
    }
    assert set(ExactArchivedEphemerisReader.__dict__).isdisjoint(
        {"latest", "history", "list", "fetch", "download", "provider"}
    )
