from __future__ import annotations

import importlib
import json
import math
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

import pytest

from leo_flow.analysis.ephemeris.normalization import (
    NormalizedTLE,
    parse_tle_catalog,
)
from leo_flow.analysis.orbit.association import StationGeometrySnapshot
from leo_flow.analysis.orbit.sgp4_adapter import (
    Sgp4DependencyError,
    Sgp4InputError,
    Sgp4OrbitPropagator,
    sgp4_vallado_wgs72_specification,
)
from leo_flow.contracts.core import (
    Digest,
    EphemerisSnapshotId,
    StationId,
    UtcNs,
    canonical_digest,
    canonical_json_bytes,
)
from leo_flow.contracts.ephemeris import EphemerisSnapshotRef, EphemerisSource

FIXTURE = Path(__file__).with_name("fixtures") / "sgp4_vallado_reference.json"


class _View:
    def __init__(self, ref: EphemerisSnapshotRef, data: bytes) -> None:
        self.ref = ref
        self._data = data

    def normalized_bytes(self) -> bytes:
        return self._data


class _Reader:
    def __init__(self, ref: EphemerisSnapshotRef, data: bytes) -> None:
        self.ref = ref
        self.data = data
        self.returned_ref = ref

    def open(self, ref: EphemerisSnapshotRef):
        assert ref == self.ref
        return nullcontext(_View(self.returned_ref, self.data))


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE.read_text())


def _entry(document: dict[str, object], key: str) -> NormalizedTLE:
    satellite = document[key]
    assert isinstance(satellite, dict)
    line1, line2 = str(satellite["line1"]), str(satellite["line2"])
    return parse_tle_catalog(f"{line1}\n{line2}\n".encode())[0]


def _correct_checksum(line: str) -> str:
    """Create a separately identified strict-input derivative for error-path tests."""

    checksum = (
        sum(
            int(character) if character.isdigit() else 1 if character == "-" else 0
            for character in line[:68]
        )
        % 10
    )
    return line[:68] + str(checksum)


def _catalog(entry: NormalizedTLE) -> bytes:
    return canonical_json_bytes(
        {
            "schema": "org.leo-flow.normalized-tle-catalog",
            "version": "1.0",
            "source": EphemerisSource.SPACE_TRACK.value,
            "scope": "verification",
            "entries": [
                {
                    "norad_id": entry.norad_id,
                    "name": entry.name,
                    "line1": entry.line1,
                    "line2": entry.line2,
                    "epoch_utc_ns": int(entry.epoch_utc_ns),
                }
            ],
        }
    )


def _adapter(
    entry: NormalizedTLE,
) -> tuple[Sgp4OrbitPropagator, EphemerisSnapshotRef, _Reader]:
    data = _catalog(entry)
    ref = EphemerisSnapshotRef(
        EphemerisSnapshotId("eph_sgp4_verification"),
        EphemerisSource.SPACE_TRACK,
        Digest.sha256(b"raw verification TLE"),
        Digest.sha256(data),
    )
    reader = _Reader(ref, data)
    return Sgp4OrbitPropagator(reader), ref, reader


def _station() -> StationGeometrySnapshot:
    identity = {
        "station_id": "station_sgp4_test",
        "frame": "ITRF",
        "position_m": (6_378_135.0, 0.0, 0.0),
    }
    return StationGeometrySnapshot(
        StationId("station_sgp4_test"),
        "ITRF",
        (6_378_135.0, 0.0, 0.0),
        canonical_digest(identity),
    )


def test_native_teme_states_match_published_vallado_verification_vectors() -> None:
    document = _fixture()
    entry = _entry(document, "reference_satellite")
    adapter, _, _ = _adapter(entry)
    satellite = document["reference_satellite"]
    assert isinstance(satellite, dict)
    states = satellite["states"]
    assert isinstance(states, list)

    for expected in states:
        assert isinstance(expected, dict)
        instant = UtcNs(
            int(entry.epoch_utc_ns)
            + int(expected["minutes_since_epoch"]) * 60_000_000_000
        )
        actual = adapter.propagate_teme(entry.line1, entry.line2, instant)
        assert actual.error_code == 0
        assert actual.position_km == pytest.approx(expected["position_km"], abs=2e-8)
        assert actual.velocity_km_s == pytest.approx(
            expected["velocity_km_s"], abs=2e-9
        )


def test_range_rate_acceleration_and_geocentric_elevation_are_frozen() -> None:
    entry = _entry(_fixture(), "reference_satellite")
    adapter, ref, _ = _adapter(entry)
    instant = UtcNs(int(entry.epoch_utc_ns) + 360 * 60_000_000_000)

    state = adapter.propagate(
        ref,
        _station(),
        sgp4_vallado_wgs72_specification(),
        entry.norad_id,
        instant,
    )

    assert state.error_code is None
    # This regression vector applies the profile's documented GMST82 rotation,
    # zero EOP, geocentric-up elevation, and one-second centered difference.
    assert state.range_rate_m_s == pytest.approx(-4141.010162183135, abs=1e-6)
    assert state.range_acceleration_m_s2 == pytest.approx(-1.6238189604009676, abs=1e-6)
    assert state.elevation_deg == pytest.approx(-30.413040715663367, abs=1e-9)


def test_published_sgp4_error_is_returned_as_an_explicit_gate() -> None:
    document = _fixture()
    satellite = document["error_satellite"]
    assert isinstance(satellite, dict)
    # The official legacy file's checksums are invalid under the strict archive
    # parser. Preserve the fixture verbatim; this derived input changes only its
    # two checksum digits and is used exclusively to exercise error handling.
    line1 = _correct_checksum(str(satellite["line1"]))
    line2 = _correct_checksum(str(satellite["line2"]))
    entry = parse_tle_catalog(f"{line1}\n{line2}\n".encode())[0]
    adapter, ref, _ = _adapter(entry)
    instant = UtcNs(
        int(entry.epoch_utc_ns) + int(satellite["minutes_since_epoch"]) * 60_000_000_000
    )

    native = adapter.propagate_teme(entry.line1, entry.line2, instant)
    assert native.error_code == satellite["error_code"]
    assert all(math.isnan(value) for value in native.position_km)
    state = adapter.propagate(
        ref,
        _station(),
        sgp4_vallado_wgs72_specification(),
        entry.norad_id,
        instant,
    )
    assert state.error_code == f"sgp4:{satellite['error_code']}"
    assert state.range_rate_m_s == state.range_acceleration_m_s2 == 0.0


def test_exact_snapshot_identity_digest_norad_and_specification_are_enforced() -> None:
    entry = _entry(_fixture(), "reference_satellite")
    adapter, ref, reader = _adapter(entry)
    specification = sgp4_vallado_wgs72_specification()

    with pytest.raises(Sgp4InputError, match="specification"):
        adapter.propagate(
            ref,
            _station(),
            replace(specification, speed_of_light_m_s=299_792_457.0),
            entry.norad_id,
            entry.epoch_utc_ns,
        )
    reader.data += b"\n"
    with pytest.raises(Sgp4InputError, match="digest"):
        adapter.propagate(
            ref, _station(), specification, entry.norad_id, entry.epoch_utc_ns
        )
    reader.data = _catalog(entry)
    reader.returned_ref = replace(ref, snapshot_id=EphemerisSnapshotId("eph_other"))
    with pytest.raises(Sgp4InputError, match="substituted"):
        adapter.propagate(
            ref, _station(), specification, entry.norad_id, entry.epoch_utc_ns
        )
    reader.returned_ref = ref
    with pytest.raises(LookupError, match="absent"):
        adapter.propagate(ref, _station(), specification, 99999, entry.epoch_utc_ns)


def test_optional_dependency_is_loaded_only_when_adapter_is_constructed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = importlib.import_module

    def missing(name: str, package: str | None = None):
        if name in {"sgp4", "sgp4.api"}:
            raise ImportError("fixture hides optional package")
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", missing)
    # Importing the association core and constructing its data never loads SGP4.
    assert sgp4_vallado_wgs72_specification().propagator_ref.artifact_id
    with pytest.raises(Sgp4DependencyError, match="orbit.*sgp4==2.25"):
        Sgp4OrbitPropagator(object())  # type: ignore[arg-type]


def test_adapter_rejects_an_unpinned_sgp4_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sgp4_module = importlib.import_module("sgp4")
    monkeypatch.setattr(sgp4_module, "__version__", "2.24")

    with pytest.raises(Sgp4DependencyError, match="requires sgp4==2.25, found 2.24"):
        Sgp4OrbitPropagator(object())  # type: ignore[arg-type]
