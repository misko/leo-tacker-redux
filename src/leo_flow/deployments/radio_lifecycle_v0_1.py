"""Composition seam for lifecycle-observed process-isolated capture work.

Firmware-specific transport and credential loading remain deployment-owned and
must be supplied explicitly.  There is deliberately no unauthenticated default.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

from leo_flow.capture.lifecycle_attempt import LifecycleObservedAttemptWorkV0_1
from leo_flow.capture.v5_station import V5CaptureStation
from leo_flow.contracts.core import CaptureBatchId
from leo_flow.contracts.radio_lifecycle import (
    RadioLifecycleFactRecorderV0_1,
    RadioLifecycleHistoryV0_1,
    RadioLifecycleObserverV0_1,
)
from leo_flow.deployments.process_isolated_capture import (
    IsolatedAttemptWork,
    IsolatedAttemptWorkFactory,
)


class LifecycleObserverBuilderV0_1(Protocol):
    def build(self, station: V5CaptureStation) -> RadioLifecycleObserverV0_1: ...


class LifecycleRepositoryBuilderV0_1(Protocol):
    def build(self, catalog_credential: str) -> LifecycleRepositoryV0_1: ...


class LifecycleRepositoryV0_1(
    RadioLifecycleFactRecorderV0_1,
    RadioLifecycleHistoryV0_1,
    Protocol,
):
    pass


class LifecycleObservedAttemptWorkFactoryV0_1:
    """Decorate the normal child-owned work after explicit authenticated setup."""

    def __init__(
        self,
        delegate: IsolatedAttemptWorkFactory,
        observer_builder: LifecycleObserverBuilderV0_1,
        repository_builder: LifecycleRepositoryBuilderV0_1,
        *,
        utc_now_ns: Callable[[], int] = time.time_ns,
        observation_timeout_ns: int = 2_000_000_000,
    ) -> None:
        self._delegate = delegate
        self._observer_builder = observer_builder
        self._repository_builder = repository_builder
        self._utc_now_ns = utc_now_ns
        self._observation_timeout_ns = observation_timeout_ns

    def build(
        self,
        station: V5CaptureStation,
        catalog_credential: str,
        batch_id: CaptureBatchId,
    ) -> IsolatedAttemptWork:
        repository = self._repository_builder.build(catalog_credential)
        # Explicit method checks keep malformed deployment composition from
        # reaching the radio even when static typing is bypassed.
        for method in ("record_attempt", "record_interval", "latest_terminal"):
            if not callable(getattr(repository, method, None)):
                raise TypeError("lifecycle repository does not implement v0.1")
        return LifecycleObservedAttemptWorkV0_1(
            self._delegate.build(station, catalog_credential, batch_id),
            batch_id=batch_id,
            radio_id=station.radio.radio_id,
            observer=self._observer_builder.build(station),
            recorder=repository,
            history=repository,
            utc_now_ns=self._utc_now_ns,
            observation_timeout_ns=self._observation_timeout_ns,
        )
