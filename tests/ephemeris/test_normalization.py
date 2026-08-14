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
from leo_flow.contracts.core import ArtifactRef, SchemaRef, UtcNs
from leo_flow.contracts.ephemeris import EphemerisSource
from testkit import digest

from ._fixtures import MemoryArchive, tle


def refs(raw: bytes, source: EphemerisSource = EphemerisSource.SPACE_TRACK):
    archive = MemoryArchive()
    raw_ref = archive.put(raw, source=source)
    return archive, raw_ref


def parser_ref() -> ArtifactRef:
    return ArtifactRef("tle-parser-v1", digest("parser"))


def policy_ref(name: str = "policy") -> ArtifactRef:
    return ArtifactRef(
        f"tle-validation-{name}-v1",
        digest(name),
        SchemaRef("org.leo-flow.tle-validation-policy"),
    )


def candidate():
    archive, raw_ref = refs(tle())
    return TLECatalogNormalizer(
        archive,
        archive,
        source=EphemerisSource.SPACE_TRACK,
        attribution="Space-Track.org",
    ).normalize(raw_ref, parser_ref())


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
    value = candidate()
    reference = UtcNs(int(value.element_epoch_min_utc_ns) + 1_000_000_001)
    reference_policy = policy_ref()
    invalid_policy = TLEValidationPolicy(
        reference_policy,
        2,
        10,
        1,
        0,
    )
    result = TLEValidator((invalid_policy,)).validate(
        value,
        reference_policy,
        retrieval_completed_utc_ns=reference,
    )
    assert not result.valid
    assert result.reason_codes == (
        "satellite_count_below_minimum",
        "element_epoch_too_old",
    )


def test_relative_epoch_boundaries_are_inclusive_and_one_ns_excursions_fail() -> None:
    value = candidate()
    reference = UtcNs(2_000_000_000_000_000_000)
    reference_policy = policy_ref("boundaries")
    validator = TLEValidator((TLEValidationPolicy(reference_policy, 1, 10, 10, 2),))
    at_boundaries = replace(
        value,
        element_epoch_min_utc_ns=UtcNs(int(reference) - 10_000_000_000),
        element_epoch_max_utc_ns=UtcNs(int(reference) + 2_000_000_000),
    )

    valid = validator.validate(
        at_boundaries,
        reference_policy,
        retrieval_completed_utc_ns=reference,
    )
    old = validator.validate(
        replace(
            at_boundaries,
            element_epoch_min_utc_ns=UtcNs(
                int(at_boundaries.element_epoch_min_utc_ns) - 1
            ),
        ),
        reference_policy,
        retrieval_completed_utc_ns=reference,
    )
    future = validator.validate(
        replace(
            at_boundaries,
            element_epoch_max_utc_ns=UtcNs(
                int(at_boundaries.element_epoch_max_utc_ns) + 1
            ),
        ),
        reference_policy,
        retrieval_completed_utc_ns=reference,
    )

    assert valid.valid
    assert old.reason_codes == ("element_epoch_too_old",)
    assert future.reason_codes == ("element_epoch_too_new",)


def test_one_relative_policy_remains_valid_at_widely_separated_retrievals() -> None:
    value = candidate()
    reference_policy = policy_ref("long-running")
    validator = TLEValidator((TLEValidationPolicy(reference_policy, 1, 10, 3600, 60),))

    for reference in (
        UtcNs(1_700_000_000_000_000_000),
        UtcNs(2_015_576_000_000_000_000),
    ):
        relative = replace(
            value,
            element_epoch_min_utc_ns=UtcNs(int(reference) - 3_600_000_000_000),
            element_epoch_max_utc_ns=UtcNs(int(reference) + 60_000_000_000),
        )
        assert validator.validate(
            relative,
            reference_policy,
            retrieval_completed_utc_ns=reference,
        ).valid


@pytest.mark.parametrize(
    ("maximum_age", "maximum_future", "exception"),
    (
        (0, 0, ValueError),
        (-1, 0, ValueError),
        (True, 0, TypeError),
        (1.5, 0, TypeError),
        (316_224_001, 0, ValueError),
        (1, -1, ValueError),
        (1, True, TypeError),
        (1, 0.5, TypeError),
        (1, 316_224_001, ValueError),
    ),
)
def test_relative_policy_rejects_unbounded_or_non_integer_configuration(
    maximum_age: object, maximum_future: object, exception: type[Exception]
) -> None:
    with pytest.raises(exception):
        TLEValidationPolicy(
            policy_ref("invalid"),
            1,
            10,
            maximum_age,  # type: ignore[arg-type]
            maximum_future,  # type: ignore[arg-type]
        )


def test_policy_requires_schema_and_validator_requires_exact_ref_and_utc() -> None:
    without_schema = ArtifactRef("tle-validation-v1", digest("without-schema"))
    with pytest.raises(ValueError, match="requires a schema"):
        TLEValidationPolicy(without_schema, 1, 10, 1, 0)

    value = candidate()
    configured_ref = policy_ref("configured")
    validator = TLEValidator((TLEValidationPolicy(configured_ref, 1, 10, 1, 0),))
    with pytest.raises(ValueError, match="unknown"):
        validator.validate(
            value,
            policy_ref("other"),
            retrieval_completed_utc_ns=value.element_epoch_max_utc_ns,
        )
    with pytest.raises(ValueError, match="non-negative UTC"):
        validator.validate(
            value,
            configured_ref,
            retrieval_completed_utc_ns=UtcNs(-1),
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
