from __future__ import annotations

import io
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from leo_flow.analysis.ephemeris.normalization import parse_tle_catalog
from leo_flow.analysis.orbit import (
    DeterministicOrbitSimulator,
    EphemerisLinkEvidence,
    PropagatedState,
    PropagationSpecification,
    RecordingVisibilityInputError,
    RecordingVisibilityRequest,
    StationGeometrySnapshot,
    VisibilityStatus,
    associate_recording_visibility,
    build_recording_visibility_policy,
    encode_recording_visibility_association,
    recording_visibility_algorithm_ref,
)
from leo_flow.contracts.core import (
    V0_1,
    ArtifactRef,
    Digest,
    EphemerisRetrievalId,
    EphemerisSnapshotId,
    RecordingId,
    SchemaRef,
    StationId,
    UtcNs,
    canonical_digest,
    canonical_json_bytes,
)
from leo_flow.contracts.ephemeris import (
    EphemerisSelectionPolicy,
    EphemerisSnapshot,
    EphemerisSnapshotRef,
    EphemerisSource,
    RecordingInterval,
    ValidationResult,
)
from leo_flow.contracts.storage import ObjectRef
from tests.ephemeris._fixtures import tle


def _artifact(name: str) -> ArtifactRef:
    return ArtifactRef(name, Digest.sha256(name.encode()))


@dataclass(frozen=True)
class _View:
    ref: EphemerisSnapshotRef
    payload: bytes

    def normalized_bytes(self) -> bytes:
        return self.payload


class _Reader:
    def __init__(self, view: _View) -> None:
        self.view = view

    def open(self, ref: EphemerisSnapshotRef):  # type: ignore[no-untyped-def]
        assert ref == self.view.ref
        return nullcontext(self.view)


@dataclass(frozen=True)
class _Fixture:
    request: RecordingVisibilityRequest
    reader: _Reader
    simulator: DeterministicOrbitSimulator
    instants: tuple[UtcNs, ...]


def _fixture(
    source: EphemerisSource = EphemerisSource.HUGGING_FACE,
    *,
    elevation_uncertainty_deg: float = 1.0,
    maximum_abs_element_age_s: int = 86_400,
) -> _Fixture:
    raw = b"".join(
        tle(norad=norad, name=f"STARLINK {norad}") for norad in (10001, 10002, 10003)
    )
    entries = parse_tle_catalog(raw)
    normalized = canonical_json_bytes(
        {
            "schema": "org.leo-flow.normalized-tle-catalog",
            "version": "1.0",
            "source": source.value,
            "scope": "starlink",
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
    raw_ref = ObjectRef(
        Digest.sha256(raw), len(raw), "text/plain", "tle-raw-v1", "memory:raw"
    )
    normalized_ref = ObjectRef(
        Digest.sha256(normalized),
        len(normalized),
        "application/json",
        "tle-normalized-v1",
        "memory:normalized",
    )
    element_epoch = entries[0].epoch_utc_ns
    interval = RecordingInterval(
        UtcNs(int(element_epoch) + 10_000_000_000),
        UtcNs(int(element_epoch) + 11_000_000_000),
    )
    retrieved_at = UtcNs(int(interval.started_utc_ns) - 1)
    snapshot = EphemerisSnapshot(
        SchemaRef(EphemerisSnapshot.SCHEMA_ID, V0_1),
        EphemerisSnapshotId(f"eph_visibility_{source.value.replace('-', '_')}"),
        EphemerisRetrievalId(f"ephret_visibility_{source.value.replace('-', '_')}"),
        source,
        "starlink",
        retrieved_at,
        raw_ref,
        normalized_ref,
        _artifact("tle-parser-v1"),
        len(entries),
        Digest.sha256(canonical_json_bytes([entry.norad_id for entry in entries])),
        min(entry.epoch_utc_ns for entry in entries),
        max(entry.epoch_utc_ns for entry in entries),
        ValidationResult(True, _artifact("tle-validation-v1")),
        "offline fixture",
    )
    snapshot_ref = EphemerisSnapshotRef(
        snapshot.snapshot_id,
        source,
        raw_ref.digest,
        normalized_ref.digest,
    )
    policy_ref = _artifact("available-then-v1")
    recording_identity = Digest.sha256(b"recording-identity")
    link_identity = {
        "recording_identity_digest": str(recording_identity),
        "recording_interval": interval,
        "source": source.value,
        "scope": "starlink",
        "policy": EphemerisSelectionPolicy.AVAILABLE_THEN.value,
        "policy_ref": policy_ref,
        "as_of_utc_ns": UtcNs(int(interval.finished_utc_ns) + 1),
        "snapshot_ref": snapshot_ref,
    }
    link_digest = canonical_digest(link_identity)
    link = EphemerisLinkEvidence(
        ArtifactRef(
            f"ephlink_{link_digest.value[:32]}",
            link_digest,
            SchemaRef("org.leo-flow.recording-ephemeris-link"),
        ),
        RecordingId("rec_visibility"),
        recording_identity,
        interval,
        source,
        "starlink",
        EphemerisSelectionPolicy.AVAILABLE_THEN,
        policy_ref,
        UtcNs(int(interval.finished_utc_ns) + 1),
        snapshot_ref,
    )
    station_identity = {
        "station_id": "station_visibility",
        "frame": "ITRF",
        "position_m": (6_378_137.0, 0.0, 0.0),
    }
    station = StationGeometrySnapshot(
        StationId("station_visibility"),
        "ITRF",
        (6_378_137.0, 0.0, 0.0),
        canonical_digest(station_identity),
    )
    propagation = PropagationSpecification(
        _artifact("fixture-propagator-v1"),
        _artifact("fixture-gravity-v1"),
        _artifact("fixture-time-v1"),
        _artifact("fixture-eop-v1"),
        _artifact("fixture-errors-v1"),
    )
    uncertainty = _artifact("operator-reviewed-uncertainty-v1")
    policy = build_recording_visibility_policy(
        uncertainty,
        minimum_elevation_deg=5.0,
        elevation_uncertainty_deg=elevation_uncertainty_deg,
        station_position_uncertainty_m=3.0,
        recording_time_uncertainty_ns=2_000_000,
        maximum_abs_element_age_s=maximum_abs_element_age_s,
    )
    instants = (
        interval.started_utc_ns,
        UtcNs((int(interval.started_utc_ns) + int(interval.finished_utc_ns)) // 2),
        interval.finished_utc_ns,
    )
    elevations = {
        10001: (4.0, 10.0, 6.0),
        10002: (2.0, 5.5, 3.0),
        10003: (-1.0, 2.0, 1.0),
    }
    states = tuple(
        PropagatedState(norad, instant, 0.0, 0.0, elevation)
        for norad, values in elevations.items()
        for instant, elevation in zip(instants, values, strict=True)
    )
    provenance = ObjectRef(
        Digest.sha256(b"provenance"),
        len(b"provenance"),
        "application/json",
        "ephemeris-provenance-v1",
        "memory:provenance",
    )
    request = RecordingVisibilityRequest(
        recording_visibility_algorithm_ref(),
        link,
        snapshot,
        provenance,
        station,
        propagation,
        tuple(entry.norad_id for entry in entries),
        policy,
    )
    return _Fixture(
        request,
        _Reader(_View(snapshot_ref, normalized)),
        DeterministicOrbitSimulator(states),
        instants,
    )


@pytest.mark.parametrize(
    "source", (EphemerisSource.HUGGING_FACE, EphemerisSource.SPACE_TRACK)
)
def test_archived_provider_recording_station_and_algorithm_are_bound(
    source: EphemerisSource,
) -> None:
    fixture = _fixture(source)

    first = associate_recording_visibility(
        fixture.request, fixture.reader, fixture.simulator
    )
    second = associate_recording_visibility(
        fixture.request, fixture.reader, fixture.simulator
    )

    assert first == second
    assert encode_recording_visibility_association(first) == (
        encode_recording_visibility_association(second)
    )
    assert first.source is source
    assert first.recording_id == RecordingId("rec_visibility")
    assert first.snapshot.retrieved_at_utc_ns < first.recording_interval.started_utc_ns
    assert (
        first.snapshot.raw_object_ref.digest
        == fixture.request.snapshot.raw_object_ref.digest
    )
    assert first.snapshot.normalized_object_ref.digest == Digest.sha256(
        fixture.reader.view.payload
    )
    assert first.provenance_object_ref == fixture.request.provenance_object_ref
    assert first.station == fixture.request.station
    assert first.algorithm_ref == recording_visibility_algorithm_ref()
    assert first.evidence_class == "weak-ephemeris-visibility"
    assert first.ground_truth_eligible is False
    assert first.associated_norad_ids == (10001,)
    assert first.possible_norad_ids == (10002,)
    assert [item.status for item in first.candidates] == [
        VisibilityStatus.VISIBLE_AT_SAMPLE_WITH_MARGIN,
        VisibilityStatus.ELEVATION_MARGIN_OVERLAP,
        VisibilityStatus.BELOW_GATE_AT_SAMPLES,
    ]
    assert all(
        item.element_epoch_utc_ns == fixture.request.snapshot.element_epoch_min_utc_ns
        for item in first.candidates
    )


def test_explicit_uncertainty_changes_candidate_class_and_artifact_identity() -> None:
    narrow = _fixture(elevation_uncertainty_deg=0.25)
    wide = _fixture(elevation_uncertainty_deg=2.0)

    narrow_result = associate_recording_visibility(
        narrow.request, narrow.reader, narrow.simulator
    )
    wide_result = associate_recording_visibility(
        wide.request, wide.reader, wide.simulator
    )

    assert (
        narrow_result.candidates[1].status
        is VisibilityStatus.VISIBLE_AT_SAMPLE_WITH_MARGIN
    )
    assert wide_result.candidates[1].status is VisibilityStatus.ELEVATION_MARGIN_OVERLAP
    assert narrow_result.policy.policy_ref != wide_result.policy.policy_ref
    assert narrow_result.association_digest != wide_result.association_digest


def test_element_age_and_propagation_failure_are_explicit_exclusions() -> None:
    aged = _fixture(maximum_abs_element_age_s=1)
    aged_result = associate_recording_visibility(
        aged.request, aged.reader, aged.simulator
    )
    assert {item.status for item in aged_result.candidates} == {
        VisibilityStatus.ELEMENT_EPOCH_OUTSIDE_BOUND
    }

    normal = _fixture()
    failure_states = tuple(
        PropagatedState(10001, instant, 0.0, 0.0, 0.0, "fixture-error")
        for instant in normal.instants
    )
    successful_states = tuple(
        PropagatedState(norad, instant, 0.0, 0.0, elevation)
        for norad, values in {10002: (2.0, 5.5, 3.0), 10003: (-1.0, 2.0, 1.0)}.items()
        for instant, elevation in zip(normal.instants, values, strict=True)
    )
    failed = associate_recording_visibility(
        normal.request,
        normal.reader,
        DeterministicOrbitSimulator(failure_states + successful_states),
    )
    assert failed.candidates[0].status is VisibilityStatus.PROPAGATION_ERROR
    assert "candidate-propagation-error" in failed.reason_codes


def test_exact_normalized_bytes_and_weak_evidence_boundary_fail_closed() -> None:
    fixture = _fixture()
    fixture.reader.view = replace(
        fixture.reader.view, payload=fixture.reader.view.payload + b"\n"
    )
    with pytest.raises(RecordingVisibilityInputError, match="bytes or digest"):
        associate_recording_visibility(
            fixture.request, fixture.reader, fixture.simulator
        )

    clean = _fixture()
    result = associate_recording_visibility(
        clean.request, clean.reader, clean.simulator
    )
    with pytest.raises(ValueError, match="cannot become ground truth"):
        replace(result, ground_truth_eligible=True)


def test_no_network_or_credential_capability_exists_in_association_module() -> None:
    import leo_flow.analysis.orbit.recording_visibility as module

    source = io.StringIO(Path(module.__file__).read_text(encoding="utf-8"))
    text = source.read()
    assert "urllib" not in text
    assert "ProviderCredentials" not in text
