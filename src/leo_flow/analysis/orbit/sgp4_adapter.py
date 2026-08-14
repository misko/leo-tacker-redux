"""Offline SGP4 adapter for archived normalized TLE snapshots.

The adapter deliberately implements only a documented TEME-to-rotating-Earth
approximation.  UT1 is set equal to UTC and polar motion is zero; callers that
need measured Earth-orientation parameters must use a future, differently
identified adapter.
"""

from __future__ import annotations

import importlib
import math
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from leo_flow.analysis.ephemeris.normalization import decode_normalized_catalog
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.ephemeris import EphemerisSnapshotRef
from leo_flow.contracts.ports import EphemerisReader

from .association import (
    PropagatedState,
    PropagationSpecification,
    StationGeometrySnapshot,
)

_DAY_NS = 86_400_000_000_000
_UNIX_EPOCH_JD = 2_440_587.5
_EARTH_ROTATION_RAD_S = 7.29211514670698e-5
_RANGE_RATE_DIFFERENCE_NS = 500_000_000


class Sgp4DependencyError(RuntimeError):
    """The optional, pinned SGP4 implementation is unavailable."""


class Sgp4InputError(ValueError):
    """Archived input or propagation configuration violates the adapter boundary."""


@dataclass(frozen=True)
class TemeState:
    """Raw SGP4 result in its native TEME frame and kilometre units."""

    utc_ns: UtcNs
    position_km: tuple[float, float, float]
    velocity_km_s: tuple[float, float, float]
    error_code: int


def _artifact(artifact_id: str, choices: dict[str, object]) -> ArtifactRef:
    document = canonical_json_bytes({"artifact_id": artifact_id, **choices})
    return ArtifactRef(artifact_id, Digest.sha256(document))


def sgp4_vallado_wgs72_specification() -> PropagationSpecification:
    """Return the one exact scientific configuration accepted by this adapter."""

    return PropagationSpecification(
        propagator_ref=_artifact(
            "python-sgp4-2.25-vallado-improved",
            {
                "package": "sgp4",
                "version": "2.25",
                "algorithm": "Vallado SGP4",
                "operation_mode": "improved",
            },
        ),
        gravity_model_ref=_artifact(
            "sgp4-wgs72",
            {"constant_set": "WGS72", "sgp4_api_constant": 1},
        ),
        time_scale_ref=_artifact(
            "sgp4-utc-as-ut1-v1",
            {
                "input": "integer Unix UTC nanoseconds",
                "sgp4_julian_date": "UTC with UT1 minus UTC fixed to zero",
                "leap_second_table": "not applied",
            },
        ),
        earth_orientation_ref=_artifact(
            "teme-pef-gmst82-no-eop-v1",
            {
                "rotation": "Vallado GMST 1982",
                "ut1_minus_utc_s": 0,
                "polar_motion_rad": [0, 0],
                "station_velocity": "zero in rotating frame",
                "local_vertical": "geocentric station position",
                "earth_rotation_rad_s": _EARTH_ROTATION_RAD_S,
            },
        ),
        error_policy_ref=_artifact(
            "sgp4-explicit-error-central-range-rate-v1",
            {
                "sgp4_error": "return sgp4:<integer-code> and zero observables",
                "missing_norad": "raise LookupError",
                "invalid_archive": "raise Sgp4InputError",
                "range_acceleration": "centered range-rate difference",
                "difference_interval_s": 1.0,
            },
        ),
    )


class Sgp4OrbitPropagator:
    """Read an exact archived TLE snapshot and propagate it without network I/O."""

    def __init__(self, ephemerides: EphemerisReader) -> None:
        self._ephemerides = ephemerides
        self._api = _load_sgp4_api()

    def propagate(
        self,
        snapshot_ref: EphemerisSnapshotRef,
        station: StationGeometrySnapshot,
        specification: PropagationSpecification,
        norad_id: int,
        utc_ns: UtcNs,
    ) -> PropagatedState:
        if specification != sgp4_vallado_wgs72_specification():
            raise Sgp4InputError(
                "propagation specification is not the pinned SGP4 profile"
            )
        if norad_id <= 0:
            raise Sgp4InputError("NORAD ID must be positive")
        if not any(station.position_m):
            raise Sgp4InputError("station position cannot be the ITRF origin")

        satellite = self._satellite(snapshot_ref, norad_id)
        center = self._propagate_satellite(satellite, utc_ns)
        before = self._propagate_satellite(
            satellite, UtcNs(int(utc_ns) - _RANGE_RATE_DIFFERENCE_NS)
        )
        after = self._propagate_satellite(
            satellite, UtcNs(int(utc_ns) + _RANGE_RATE_DIFFERENCE_NS)
        )
        error = next(
            (item.error_code for item in (center, before, after) if item.error_code),
            0,
        )
        if error:
            return PropagatedState(norad_id, utc_ns, 0.0, 0.0, 0.0, f"sgp4:{error}")

        center_rate, elevation = _range_rate_and_elevation(center, station)
        before_rate, _ = _range_rate_and_elevation(before, station)
        after_rate, _ = _range_rate_and_elevation(after, station)
        acceleration = after_rate - before_rate  # the interval is exactly one second
        return PropagatedState(
            norad_id,
            utc_ns,
            center_rate,
            acceleration,
            elevation,
        )

    def propagate_teme(self, line1: str, line2: str, utc_ns: UtcNs) -> TemeState:
        """Expose native state solely for verification and scientific audit."""

        satellite = self._api.Satrec.twoline2rv(line1, line2, self._api.WGS72)
        return self._propagate_satellite(satellite, utc_ns)

    def _satellite(self, ref: EphemerisSnapshotRef, norad_id: int) -> Any:
        with self._ephemerides.open(ref) as view:
            if view.ref != ref:
                raise Sgp4InputError("ephemeris reader substituted snapshot identity")
            normalized = view.normalized_bytes()
        if Digest.sha256(normalized) != ref.normalized_digest:
            raise Sgp4InputError("normalized ephemeris digest differs")
        try:
            entries = decode_normalized_catalog(normalized, ref.source)
        except ValueError as error:
            raise Sgp4InputError("normalized ephemeris catalog is invalid") from error
        try:
            entry = next(item for item in entries if item.norad_id == norad_id)
        except StopIteration as error:
            raise LookupError(
                f"NORAD {norad_id} is absent from the exact snapshot"
            ) from error
        return self._api.Satrec.twoline2rv(entry.line1, entry.line2, self._api.WGS72)

    def _propagate_satellite(self, satellite: Any, utc_ns: UtcNs) -> TemeState:
        jd, fraction = _utc_ns_to_julian_date(utc_ns)
        error, position, velocity = satellite.sgp4(jd, fraction)
        if error:
            nan_vector = (math.nan, math.nan, math.nan)
            return TemeState(utc_ns, nan_vector, nan_vector, int(error))
        return TemeState(
            utc_ns,
            _vector3(position),
            _vector3(velocity),
            0,
        )


def _load_sgp4_api() -> ModuleType:
    try:
        api = importlib.import_module("sgp4.api")
    except ImportError as error:
        raise Sgp4DependencyError(
            "Sgp4OrbitPropagator requires the 'orbit' extra (sgp4==2.25)"
        ) from error
    version = importlib.import_module("sgp4").__version__
    if version != "2.25":
        raise Sgp4DependencyError(
            f"Sgp4OrbitPropagator requires sgp4==2.25, found {version}"
        )
    return api


def _utc_ns_to_julian_date(utc_ns: UtcNs) -> tuple[float, float]:
    days, remainder = divmod(int(utc_ns), _DAY_NS)
    return _UNIX_EPOCH_JD + days, remainder / _DAY_NS


def _vector3(values: Any) -> tuple[float, float, float]:
    return float(values[0]), float(values[1]), float(values[2])


def _gmst82_radians(utc_ns: UtcNs) -> float:
    jd, fraction = _utc_ns_to_julian_date(utc_ns)
    centuries = (jd + fraction - 2_451_545.0) / 36_525.0
    seconds = (
        -6.2e-6 * centuries**3
        + 0.093104 * centuries**2
        + (876_600.0 * 3_600.0 + 8_640_184.812866) * centuries
        + 67_310.54841
    )
    return math.radians((seconds / 240.0) % 360.0)


def _range_rate_and_elevation(
    state: TemeState, station: StationGeometrySnapshot
) -> tuple[float, float]:
    theta = _gmst82_radians(state.utc_ns)
    cosine, sine = math.cos(theta), math.sin(theta)
    x, y, z = state.position_km
    vx, vy, vz = state.velocity_km_s
    position_m = (
        (cosine * x + sine * y) * 1_000.0,
        (-sine * x + cosine * y) * 1_000.0,
        z * 1_000.0,
    )
    rotated_velocity_m_s = (
        (cosine * vx + sine * vy) * 1_000.0,
        (-sine * vx + cosine * vy) * 1_000.0,
        vz * 1_000.0,
    )
    velocity_m_s = (
        rotated_velocity_m_s[0] + _EARTH_ROTATION_RAD_S * position_m[1],
        rotated_velocity_m_s[1] - _EARTH_ROTATION_RAD_S * position_m[0],
        rotated_velocity_m_s[2],
    )
    relative = tuple(
        position_m[index] - station.position_m[index] for index in range(3)
    )
    distance = math.sqrt(sum(value * value for value in relative))
    station_radius = math.sqrt(sum(value * value for value in station.position_m))
    if distance == 0.0:
        raise Sgp4InputError("satellite and station positions coincide")
    range_rate = (
        sum(relative[index] * velocity_m_s[index] for index in range(3)) / distance
    )
    elevation_sine = sum(
        relative[index] * station.position_m[index] for index in range(3)
    ) / (distance * station_radius)
    elevation = math.degrees(math.asin(max(-1.0, min(1.0, elevation_sine))))
    return range_rate, elevation
