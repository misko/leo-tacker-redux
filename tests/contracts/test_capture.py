from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.contracts.capture import (
    ActivityKind,
    GainMode,
    GainSetting,
)
from leo_flow.contracts.core import canonical_json_bytes
from testkit import capture_plan, recording_manifest


def test_scan_and_dwell_are_declared_not_inferred() -> None:
    plan = capture_plan()
    assert plan.activities[0].kind is ActivityKind.DWELL
    assert plan.activities[0].segments[0].sample_count == 8


def test_segment_requires_exactly_one_stop_condition() -> None:
    base = capture_plan().activities[0].segments[0]
    with pytest.raises(ValueError, match="exactly one"):
        replace(base, duration_s=1.0)
    with pytest.raises(ValueError, match="exactly one"):
        replace(base, sample_count=None)


def test_receiver_shape_and_manifest_ownership_are_validated() -> None:
    manifest = recording_manifest()
    with pytest.raises(ValueError, match="shape"):
        replace(manifest.segments[0], shape=(8, 1, 2))
    with pytest.raises(ValueError, match="exactly one activity"):
        replace(manifest, activities=())


def test_embedded_manifest_has_no_raw_object_self_digest() -> None:
    manifest = recording_manifest()
    encoded = canonical_json_bytes(manifest)
    assert b"raw_object" not in encoded
    assert b"object_digest" not in encoded
    assert b"manifest_digest" not in encoded


def test_gain_mode_contract_prevents_ambiguous_gain() -> None:
    with pytest.raises(ValueError):
        GainSetting(GainMode.MANUAL)
    with pytest.raises(ValueError):
        GainSetting(GainMode.AGC, 50.0)
