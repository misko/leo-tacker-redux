"""Pure provider-preserving ephemeris snapshot time selection."""

from __future__ import annotations

from dataclasses import dataclass

from leo_flow.contracts.core import ArtifactRef, UtcNs
from leo_flow.contracts.ephemeris import (
    EphemerisSelection,
    EphemerisSelectionPolicy,
    EphemerisSnapshotRef,
    EphemerisSource,
    RecordingInterval,
)


class NoEphemerisSelectionError(LookupError):
    pass


class UnsupportedSelectionPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class SnapshotRecord:
    snapshot_ref: EphemerisSnapshotRef
    retrieval_completed_utc_ns: UtcNs


class TemporalEphemerisResolver:
    def __init__(
        self,
        history: tuple[SnapshotRecord, ...],
        policy: EphemerisSelectionPolicy,
    ) -> None:
        if policy is EphemerisSelectionPolicy.BEST_EPHEMERIS:
            raise UnsupportedSelectionPolicyError(
                "best_ephemeris lacks a frozen objective, lookahead, and tie-break"
            )
        self._history = history
        self._policy = policy

    def resolve(
        self,
        source: EphemerisSource,
        recording_interval: RecordingInterval,
        policy_ref: ArtifactRef,
        as_of_utc_ns: UtcNs,
    ) -> EphemerisSelection:
        history = [
            record
            for record in self._history
            if record.snapshot_ref.source is source
            and record.retrieval_completed_utc_ns <= as_of_utc_ns
        ]
        if self._policy is EphemerisSelectionPolicy.AVAILABLE_THEN:
            eligible = [
                record
                for record in history
                if record.retrieval_completed_utc_ns
                <= recording_interval.started_utc_ns
            ]
            selected = max(
                eligible,
                key=lambda record: (
                    int(record.retrieval_completed_utc_ns),
                    str(record.snapshot_ref.snapshot_id),
                ),
                default=None,
            )
        elif self._policy is EphemerisSelectionPolicy.FIRST_AFTER:
            eligible = [
                record
                for record in history
                if record.retrieval_completed_utc_ns
                > recording_interval.finished_utc_ns
            ]
            selected = min(
                eligible,
                key=lambda record: (
                    int(record.retrieval_completed_utc_ns),
                    str(record.snapshot_ref.snapshot_id),
                ),
                default=None,
            )
        else:
            raise UnsupportedSelectionPolicyError(str(self._policy))
        if selected is None:
            raise NoEphemerisSelectionError(
                f"no {source.value} snapshot satisfies {self._policy.value}"
            )
        return EphemerisSelection(
            source,
            self._policy,
            policy_ref,
            selected.snapshot_ref,
            as_of_utc_ns,
        )
