from __future__ import annotations

from leo_flow.capture.lifecycle_attempt import LifecycleObservedAttemptWorkV0_1
from leo_flow.capture.radio_lifecycle import InMemoryRadioLifecycleFactRecorderV0_1
from leo_flow.contracts.core import CaptureBatchId
from leo_flow.deployments.radio_lifecycle_v0_1 import (
    LifecycleObservedAttemptWorkFactoryV0_1,
)
from leo_flow.deployments.v5_scan import DEVELOPMENT_STATION


class _DelegateFactory:
    def __init__(self) -> None:
        self.calls = 0

    def build(self, station, catalog_credential, batch_id):
        self.calls += 1
        return object()


class _ObserverBuilder:
    def __init__(self) -> None:
        self.calls = 0

    def build(self, station):
        self.calls += 1
        return object()


class _RepositoryBuilder:
    def __init__(self, value) -> None:
        self.value = value
        self.credentials = []

    def build(self, catalog_credential):
        self.credentials.append(catalog_credential)
        return self.value


def test_explicit_composition_wraps_child_work_without_a_default_transport() -> None:
    delegate = _DelegateFactory()
    observer = _ObserverBuilder()
    repository = _RepositoryBuilder(InMemoryRadioLifecycleFactRecorderV0_1())
    factory = LifecycleObservedAttemptWorkFactoryV0_1(
        delegate,  # type: ignore[arg-type]
        observer,  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
    )
    result = factory.build(
        DEVELOPMENT_STATION,
        "private-test-dsn",
        CaptureBatchId("cbatch_lifecycle_factory"),
    )
    assert isinstance(result, LifecycleObservedAttemptWorkV0_1)
    assert delegate.calls == observer.calls == 1
    assert repository.credentials == ["private-test-dsn"]


def test_malformed_repository_is_rejected_before_work_or_observer_build() -> None:
    delegate = _DelegateFactory()
    observer = _ObserverBuilder()
    factory = LifecycleObservedAttemptWorkFactoryV0_1(
        delegate,  # type: ignore[arg-type]
        observer,  # type: ignore[arg-type]
        _RepositoryBuilder(object()),  # type: ignore[arg-type]
    )
    try:
        factory.build(
            DEVELOPMENT_STATION,
            "private-test-dsn",
            CaptureBatchId("cbatch_lifecycle_factory"),
        )
    except TypeError as error:
        assert "lifecycle repository" in str(error)
    else:  # pragma: no cover - explicit failure message
        raise AssertionError("malformed repository was accepted")
    assert delegate.calls == observer.calls == 0
