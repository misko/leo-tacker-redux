from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from leo_flow.contracts.core import ArtifactRef, EphemerisSnapshotId, JobId, SchemaRef
from leo_flow.contracts.dashboard import TimeRangeQuery
from leo_flow.contracts.ephemeris import (
    EphemerisSelection,
    EphemerisSelectionPolicy,
    EphemerisSnapshotRef,
    EphemerisSource,
)
from leo_flow.jobs import JobLease, JobPayload, JobType
from testkit import digest


def test_ephemeris_selection_never_silently_crosses_provider() -> None:
    policy = ArtifactRef("policy_available-v1", digest("policy"))
    ref = EphemerisSnapshotRef(
        EphemerisSnapshotId("eph_01"),
        EphemerisSource.SPACE_TRACK,
        digest("raw"),
        digest("normalized"),
    )
    selected = EphemerisSelection(
        EphemerisSource.SPACE_TRACK,
        EphemerisSelectionPolicy.AVAILABLE_THEN,
        policy,
        ref,
        1_700_000_000_000_000_000,
    )
    assert selected.policy is EphemerisSelectionPolicy.AVAILABLE_THEN
    with pytest.raises(ValueError, match="cross providers"):
        replace(selected, source=EphemerisSource.HUGGING_FACE)


def test_job_payload_is_deeply_immutable_and_lease_is_fenced() -> None:
    payload = JobPayload.create(
        SchemaRef("org.leo-flow.job.recording"), {"ids": ["rec_01"]}
    )
    lease = JobLease(
        JobId("job_01"),
        JobType.RECORDING_ANALYSIS,
        payload,
        1,
        "lease_token-01",
        2,
        1_700_000_100_000_000_000,
    )
    assert lease.lease_generation == 2
    assert payload.value == (("ids", ("rec_01",)),)
    with pytest.raises(FrozenInstanceError):
        lease.attempt = 2  # type: ignore[misc]


def test_dashboard_time_ranges_are_half_open() -> None:
    query = TimeRangeQuery(100, 200)
    assert query.start_utc_ns == 100 and query.stop_utc_ns == 200
    with pytest.raises(ValueError, match="half-open"):
        TimeRangeQuery(100, 100)
