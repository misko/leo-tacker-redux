"""Deterministic TLE parsing, normalization, and bounded validation."""

from __future__ import annotations

import json
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import BinaryIO, Protocol

from leo_flow.contracts._validation import require_utc_ns
from leo_flow.contracts.core import ArtifactRef, Digest, UtcNs, canonical_json_bytes
from leo_flow.contracts.ephemeris import (
    EphemerisSnapshotCandidate,
    EphemerisSource,
    ValidationResult,
)
from leo_flow.contracts.storage import ObjectRef


class TLEFormatError(ValueError):
    pass


_MAX_RELATIVE_EPOCH_BOUND_S = 10 * 366 * 24 * 60 * 60
_NS_PER_SECOND = 1_000_000_000


class ObjectReader(Protocol):
    def open(self, ref: ObjectRef) -> AbstractContextManager[BinaryIO]: ...


class NormalizedEphemerisArchive(Protocol):
    def put(self, data: bytes, *, source: EphemerisSource) -> ObjectRef: ...


@dataclass(frozen=True)
class TLEValidationPolicy:
    policy_ref: ArtifactRef
    minimum_satellites: int
    maximum_satellites: int
    maximum_epoch_age_s: int
    maximum_future_skew_s: int

    def __post_init__(self) -> None:
        if self.policy_ref.schema is None:
            raise ValueError("TLE validation policy reference requires a schema")
        if not 0 < self.minimum_satellites <= self.maximum_satellites:
            raise ValueError("satellite bounds are invalid")
        _relative_seconds(
            self.maximum_epoch_age_s,
            "maximum_epoch_age_s",
            allow_zero=False,
        )
        _relative_seconds(
            self.maximum_future_skew_s,
            "maximum_future_skew_s",
            allow_zero=True,
        )


@dataclass(frozen=True)
class NormalizedTLE:
    norad_id: int
    name: str | None
    line1: str
    line2: str
    epoch_utc_ns: UtcNs


class TLECatalogNormalizer:
    def __init__(
        self,
        raw_reader: ObjectReader,
        normalized_archive: NormalizedEphemerisArchive,
        *,
        source: EphemerisSource,
        scope: str = "starlink",
        attribution: str,
        max_raw_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        self._reader = raw_reader
        self._archive = normalized_archive
        self._source = source
        self._scope = scope
        self._attribution = attribution
        self._max_raw_bytes = max_raw_bytes

    def normalize(
        self, raw_ref: ObjectRef, parser_ref: ArtifactRef
    ) -> EphemerisSnapshotCandidate:
        if raw_ref.byte_count > self._max_raw_bytes:
            raise TLEFormatError("raw catalog exceeds parser bound")
        context = self._reader.open(raw_ref)
        with context as stream:
            raw = stream.read(self._max_raw_bytes + 1)
        if len(raw) != raw_ref.byte_count or len(raw) > self._max_raw_bytes:
            raise TLEFormatError("raw catalog is truncated or oversized")
        if Digest.sha256(raw) != raw_ref.digest:
            raise TLEFormatError("raw catalog digest differs")
        entries = parse_tle_catalog(raw)
        normalized = canonical_json_bytes(
            {
                "schema": "org.leo-flow.normalized-tle-catalog",
                "version": "1.0",
                "source": self._source.value,
                "scope": self._scope,
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
        normalized_ref = self._archive.put(normalized, source=self._source)
        ids = canonical_json_bytes([entry.norad_id for entry in entries])
        epochs = [entry.epoch_utc_ns for entry in entries]
        return EphemerisSnapshotCandidate(
            self._source,
            self._scope,
            raw_ref,
            normalized_ref,
            parser_ref,
            len(entries),
            Digest.sha256(ids),
            min(epochs),
            max(epochs),
            self._attribution,
        )


class TLEValidator:
    def __init__(self, policies: tuple[TLEValidationPolicy, ...]) -> None:
        self._policies = {policy.policy_ref: policy for policy in policies}
        if len(self._policies) != len(policies):
            raise ValueError("validation policy references must be unique")

    def validate(
        self,
        candidate: EphemerisSnapshotCandidate,
        policy_ref: ArtifactRef,
        *,
        retrieval_completed_utc_ns: UtcNs,
    ) -> ValidationResult:
        require_utc_ns(retrieval_completed_utc_ns, "retrieval_completed_utc_ns")
        try:
            policy = self._policies[policy_ref]
        except KeyError as error:
            raise ValueError("unknown TLE validation policy") from error
        reasons: list[str] = []
        if candidate.satellite_count < policy.minimum_satellites:
            reasons.append("satellite_count_below_minimum")
        if candidate.satellite_count > policy.maximum_satellites:
            reasons.append("satellite_count_above_maximum")
        if (
            int(retrieval_completed_utc_ns) - int(candidate.element_epoch_min_utc_ns)
            > policy.maximum_epoch_age_s * _NS_PER_SECOND
        ):
            reasons.append("element_epoch_too_old")
        if (
            int(candidate.element_epoch_max_utc_ns) - int(retrieval_completed_utc_ns)
            > policy.maximum_future_skew_s * _NS_PER_SECOND
        ):
            reasons.append("element_epoch_too_new")
        return ValidationResult(not reasons, policy_ref, tuple(reasons))


def _relative_seconds(value: object, field: str, *, allow_zero: bool) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    minimum = 0 if allow_zero else 1
    if not minimum <= value <= _MAX_RELATIVE_EPOCH_BOUND_S:
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(
            f"{field} must be {qualifier} and at most "
            f"{_MAX_RELATIVE_EPOCH_BOUND_S} seconds"
        )
    return value


def parse_tle_catalog(raw: bytes) -> tuple[NormalizedTLE, ...]:
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as error:
        raise TLEFormatError("TLE catalog must be ASCII") from error
    lines = [line.rstrip("\r") for line in text.splitlines() if line.strip()]
    entries: list[NormalizedTLE] = []
    index = 0
    while index < len(lines):
        name: str | None = None
        if lines[index].startswith("0 "):
            name = lines[index][2:].strip()
            index += 1
        if index + 1 >= len(lines):
            raise TLEFormatError("partial TLE at end of catalog")
        line1, line2 = lines[index], lines[index + 1]
        index += 2
        _validate_tle_line(line1, "1")
        _validate_tle_line(line2, "2")
        norad1 = _norad_id(line1)
        norad2 = _norad_id(line2)
        if norad1 != norad2:
            raise TLEFormatError("TLE line NORAD IDs differ")
        entries.append(NormalizedTLE(norad1, name, line1, line2, _tle_epoch(line1)))
    if not entries:
        raise TLEFormatError("TLE catalog is empty")
    ids = [entry.norad_id for entry in entries]
    if len(ids) != len(set(ids)):
        raise TLEFormatError("duplicate NORAD ID")
    return tuple(sorted(entries, key=lambda entry: entry.norad_id))


def decode_normalized_catalog(
    data: bytes, expected_source: EphemerisSource
) -> tuple[NormalizedTLE, ...]:
    """Offline replay parser for an exact normalized catalog blob."""
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TLEFormatError("normalized catalog is not JSON") from error
    if canonical_json_bytes(value) != data or not isinstance(value, dict):
        raise TLEFormatError("normalized catalog is not canonical")
    if (
        value.get("schema") != "org.leo-flow.normalized-tle-catalog"
        or value.get("version") != "1.0"
    ):
        raise TLEFormatError("unsupported normalized catalog schema")
    if value.get("source") != expected_source.value:
        raise TLEFormatError("normalized catalog provider differs")
    raw_entries = value.get("entries")
    if not isinstance(raw_entries, list):
        raise TLEFormatError("normalized entries must be an array")
    entries: list[NormalizedTLE] = []
    for item in raw_entries:
        if not isinstance(item, dict):
            raise TLEFormatError("normalized entry must be an object")
        try:
            entry = NormalizedTLE(
                int(item["norad_id"]),
                item["name"],
                str(item["line1"]),
                str(item["line2"]),
                UtcNs(int(item["epoch_utc_ns"])),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise TLEFormatError("malformed normalized entry") from error
        _validate_tle_line(entry.line1, "1")
        _validate_tle_line(entry.line2, "2")
        if (
            _norad_id(entry.line1) != entry.norad_id
            or _norad_id(entry.line2) != entry.norad_id
            or _tle_epoch(entry.line1) != entry.epoch_utc_ns
        ):
            raise TLEFormatError("normalized entry facts differ from TLE lines")
        entries.append(entry)
    ids = [entry.norad_id for entry in entries]
    if ids != sorted(ids):
        raise TLEFormatError("normalized entries are not deterministically ordered")
    if len(ids) != len(set(ids)):
        raise TLEFormatError("normalized catalog has duplicate NORAD IDs")
    return tuple(entries)


def _validate_tle_line(line: str, expected_number: str) -> None:
    if len(line) != 69 or not line.startswith(expected_number + " "):
        raise TLEFormatError(f"malformed TLE line {expected_number}")
    if not line[-1].isdigit() or _tle_checksum(line[:-1]) != int(line[-1]):
        raise TLEFormatError(f"checksum failure on TLE line {expected_number}")


def _tle_checksum(body: str) -> int:
    return (
        sum(int(char) if char.isdigit() else 1 if char == "-" else 0 for char in body)
        % 10
    )


def _norad_id(line: str) -> int:
    try:
        return int(line[2:7])
    except ValueError as error:
        raise TLEFormatError("invalid NORAD ID") from error


def _tle_epoch(line1: str) -> UtcNs:
    field = line1[18:32]
    try:
        year2 = int(field[:2])
        day_text, fraction_text = field[2:].split(".", 1)
        day = int(day_text)
        fraction_day_ns = (
            int(fraction_text) * 86_400 * 1_000_000_000 // (10 ** len(fraction_text))
        )
    except ValueError as error:
        raise TLEFormatError("invalid TLE epoch") from error
    year = 1900 + year2 if year2 >= 57 else 2000 + year2
    if not 1 <= day <= 366:
        raise TLEFormatError("TLE epoch day is outside year")
    instant = datetime(year, 1, 1, tzinfo=UTC) + timedelta(days=day - 1)
    if instant.year != year:
        raise TLEFormatError("TLE epoch day exceeds calendar year")
    return UtcNs(int(instant.timestamp()) * 1_000_000_000 + fraction_day_ns)
