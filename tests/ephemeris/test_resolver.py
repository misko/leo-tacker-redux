from __future__ import annotations

import pytest

from leo_flow.analysis.ephemeris.resolver import (
    NoEphemerisSelectionError,
    SnapshotRecord,
    TemporalEphemerisResolver,
    UnsupportedSelectionPolicyError,
)
from leo_flow.contracts.core import ArtifactRef, EphemerisSnapshotId, UtcNs
from leo_flow.contracts.ephemeris import (
    EphemerisSelectionPolicy,
    EphemerisSnapshotRef,
    EphemerisSource,
    RecordingInterval,
)
from testkit import digest


def snapshot(identity: str, source: EphemerisSource, completed: int) -> SnapshotRecord:
    return SnapshotRecord(
        EphemerisSnapshotRef(
            EphemerisSnapshotId(identity),
            source,
            digest(identity + "raw"),
            digest(identity + "norm"),
        ),
        UtcNs(completed),
    )


POLICY_REF = ArtifactRef("selection-policy-v1", digest("selection"))
INTERVAL = RecordingInterval(UtcNs(100), UtcNs(200))


def test_available_then_includes_exact_start_boundary_and_never_future() -> None:
    history = (
        snapshot("eph_early", EphemerisSource.SPACE_TRACK, 99),
        snapshot("eph_boundary", EphemerisSource.SPACE_TRACK, 100),
        snapshot("eph_future", EphemerisSource.SPACE_TRACK, 101),
    )
    selected = TemporalEphemerisResolver(
        history, EphemerisSelectionPolicy.AVAILABLE_THEN
    ).resolve(EphemerisSource.SPACE_TRACK, INTERVAL, POLICY_REF, UtcNs(1_000))
    assert selected.snapshot_ref.snapshot_id == EphemerisSnapshotId("eph_boundary")


def test_first_after_is_strictly_after_finish_and_respects_as_of() -> None:
    history = (
        snapshot("eph_at_finish", EphemerisSource.SPACE_TRACK, 200),
        snapshot("eph_first", EphemerisSource.SPACE_TRACK, 201),
        snapshot("eph_later", EphemerisSource.SPACE_TRACK, 202),
    )
    resolver = TemporalEphemerisResolver(history, EphemerisSelectionPolicy.FIRST_AFTER)
    with pytest.raises(NoEphemerisSelectionError):
        resolver.resolve(EphemerisSource.SPACE_TRACK, INTERVAL, POLICY_REF, UtcNs(200))
    selected = resolver.resolve(
        EphemerisSource.SPACE_TRACK, INTERVAL, POLICY_REF, UtcNs(201)
    )
    assert selected.snapshot_ref.snapshot_id == EphemerisSnapshotId("eph_first")


def test_provider_histories_remain_independent() -> None:
    history = (
        snapshot("eph_space", EphemerisSource.SPACE_TRACK, 90),
        snapshot("eph_hf", EphemerisSource.HUGGING_FACE, 95),
    )
    resolver = TemporalEphemerisResolver(
        history, EphemerisSelectionPolicy.AVAILABLE_THEN
    )
    space = resolver.resolve(
        EphemerisSource.SPACE_TRACK, INTERVAL, POLICY_REF, UtcNs(1_000)
    )
    hf = resolver.resolve(
        EphemerisSource.HUGGING_FACE, INTERVAL, POLICY_REF, UtcNs(1_000)
    )
    assert space.snapshot_ref.source is EphemerisSource.SPACE_TRACK
    assert hf.snapshot_ref.source is EphemerisSource.HUGGING_FACE


def test_best_ephemeris_fails_until_policy_is_scientifically_defined() -> None:
    with pytest.raises(UnsupportedSelectionPolicyError, match="objective"):
        TemporalEphemerisResolver((), EphemerisSelectionPolicy.BEST_EPHEMERIS)
