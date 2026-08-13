from __future__ import annotations

import math
from dataclasses import FrozenInstanceError

import pytest

from leo_flow.contracts.core import (
    Digest,
    RecordingId,
    SchemaVersion,
    canonical_digest,
    canonical_json_bytes,
)
from leo_flow.contracts.storage import ObjectRef


def test_canonical_hash_is_independent_of_mapping_order() -> None:
    left = {"z": [3, 2, 1], "a": {"b": True, "a": "é"}}
    right = {"a": {"a": "é", "b": True}, "z": [3, 2, 1]}
    assert canonical_json_bytes(left) == canonical_json_bytes(right)
    assert canonical_digest(left) == canonical_digest(right)


def test_canonical_json_rejects_nonfinite_and_normalizes_negative_zero() -> None:
    assert canonical_json_bytes({"value": -0.0}) == b'{"value":0}'
    assert canonical_json_bytes([1.0, 1e-6, 1e-7, 1e20, 1e21]) == (
        b"[1,0.000001,1e-7,100000000000000000000,1e+21]"
    )
    for invalid in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError, match="NaN and Infinity"):
            canonical_json_bytes({"value": invalid})


def test_schema_compatibility_is_explicit_within_one_major() -> None:
    reader = SchemaVersion.parse("1.3")
    assert reader.can_read(SchemaVersion.parse("1.0"))
    assert reader.can_read(SchemaVersion.parse("1.3"))
    assert not reader.can_read(SchemaVersion.parse("1.4"))
    assert not reader.can_read(SchemaVersion.parse("2.0"))
    with pytest.raises(ValueError):
        SchemaVersion.parse("v1.0")


def test_ids_are_namespaced_and_object_refs_are_immutable() -> None:
    with pytest.raises(ValueError, match="rec_"):
        RecordingId("wrong_01")
    ref = ObjectRef(
        Digest.sha256(b"raw"), 3, "application/octet-stream", "raw-v1", "opaque:1"
    )
    with pytest.raises(FrozenInstanceError):
        ref.byte_count = 4  # type: ignore[misc]


def test_object_ref_hash_is_exact_bytes_not_locator() -> None:
    digest = Digest.sha256(b"raw")
    a = ObjectRef(digest, 3, "application/octet-stream", "raw-v1", "opaque:a")
    b = ObjectRef(digest, 3, "application/octet-stream", "raw-v1", "opaque:b")
    assert a.digest == b.digest
    assert a.locator != b.locator
