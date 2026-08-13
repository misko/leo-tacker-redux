from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.analysis.ephemeris.normalization import (
    TLECatalogNormalizer,
    TLEFormatError,
    TLEValidationPolicy,
    TLEValidator,
    decode_normalized_catalog,
    parse_tle_catalog,
)
from leo_flow.contracts.core import ArtifactRef, UtcNs
from leo_flow.contracts.ephemeris import EphemerisSource
from testkit import digest

from ._fixtures import MemoryArchive, tle


def refs(raw: bytes, source: EphemerisSource = EphemerisSource.SPACE_TRACK):
    archive = MemoryArchive()
    raw_ref = archive.put(raw, source=source)
    return archive, raw_ref


def parser_ref() -> ArtifactRef:
    return ArtifactRef("tle-parser-v1", digest("parser"))


def test_normalization_is_deterministic_and_replays_offline() -> None:
    raw = tle(54321) + tle(12345)
    archive, raw_ref = refs(raw)
    normalizer = TLECatalogNormalizer(
        archive,
        archive,
        source=EphemerisSource.SPACE_TRACK,
        attribution="Space-Track.org",
    )
    first = normalizer.normalize(raw_ref, parser_ref())
    second = normalizer.normalize(raw_ref, parser_ref())
    assert first.normalized_object_ref.digest == second.normalized_object_ref.digest
    normalized = archive.objects[first.normalized_object_ref.digest.value]
    replay = decode_normalized_catalog(normalized, EphemerisSource.SPACE_TRACK)
    assert [entry.norad_id for entry in replay] == [12345, 54321]
    assert first.satellite_count == 2


def test_provider_identity_is_part_of_normalized_bytes() -> None:
    raw = tle()
    space_archive, space_ref = refs(raw, EphemerisSource.SPACE_TRACK)
    hf_archive, hf_ref = refs(raw, EphemerisSource.HUGGING_FACE)
    space = TLECatalogNormalizer(
        space_archive,
        space_archive,
        source=EphemerisSource.SPACE_TRACK,
        attribution="Space-Track.org",
    ).normalize(space_ref, parser_ref())
    hf = TLECatalogNormalizer(
        hf_archive,
        hf_archive,
        source=EphemerisSource.HUGGING_FACE,
        attribution="Hugging Face dataset",
    ).normalize(hf_ref, parser_ref())
    assert space.normalized_object_ref.digest != hf.normalized_object_ref.digest
    with pytest.raises(TLEFormatError, match="provider differs"):
        decode_normalized_catalog(
            space_archive.objects[space.normalized_object_ref.digest.value],
            EphemerisSource.HUGGING_FACE,
        )


def test_checksum_partial_and_duplicate_norad_are_rejected() -> None:
    valid = tle()
    broken = bytearray(valid)
    first_newline = broken.index(ord("\n"))
    line1_checksum = first_newline + 1 + 68
    broken[line1_checksum] = (
        ord("0") if broken[line1_checksum] != ord("0") else ord("1")
    )
    with pytest.raises(TLEFormatError, match="checksum"):
        parse_tle_catalog(bytes(broken))
    with pytest.raises(TLEFormatError, match="partial"):
        parse_tle_catalog(valid.rsplit(b"\n", 2)[0] + b"\n")
    with pytest.raises(TLEFormatError, match="duplicate NORAD"):
        parse_tle_catalog(valid + valid)


def test_count_and_epoch_policy_returns_explicit_reasons() -> None:
    archive, raw_ref = refs(tle())
    candidate = TLECatalogNormalizer(
        archive,
        archive,
        source=EphemerisSource.SPACE_TRACK,
        attribution="Space-Track.org",
    ).normalize(raw_ref, parser_ref())
    policy_ref = ArtifactRef("tle-validation-v1", digest("policy"))
    invalid_policy = TLEValidationPolicy(
        policy_ref,
        2,
        10,
        UtcNs(int(candidate.element_epoch_min_utc_ns) + 1),
        UtcNs(int(candidate.element_epoch_max_utc_ns) + 100),
    )
    result = TLEValidator((invalid_policy,)).validate(candidate, policy_ref)
    assert not result.valid
    assert result.reason_codes == (
        "satellite_count_below_minimum",
        "element_epoch_too_old",
    )


def test_raw_digest_and_length_are_verified_before_parse() -> None:
    archive, raw_ref = refs(tle())
    normalizer = TLECatalogNormalizer(
        archive,
        archive,
        source=EphemerisSource.SPACE_TRACK,
        attribution="Space-Track.org",
    )
    with pytest.raises(TLEFormatError, match="truncated"):
        normalizer.normalize(
            replace(raw_ref, byte_count=raw_ref.byte_count + 1), parser_ref()
        )
    archive.objects[digest("wrong").value] = archive.objects[raw_ref.digest.value]
    with pytest.raises(TLEFormatError, match="digest"):
        normalizer.normalize(replace(raw_ref, digest=digest("wrong")), parser_ref())
