"""Shared, bounded process lifecycle with machine-readable diagnostics."""

from __future__ import annotations

import json
import signal
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import Enum
from types import FrameType
from typing import Any, Protocol, TextIO


class ServiceState(str, Enum):
    STARTING = "starting"
    READY = "ready"
    DRAINING = "draining"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True)
class HealthSnapshot:
    service: str
    instance_id: str
    state: ServiceState
    ready: bool
    completed_units: int
    failed_units: int
    detail: str | None = None


@dataclass(frozen=True)
class DiagnosticEvent:
    event: str
    service: str
    instance_id: str
    state: str
    completed_units: int
    failed_units: int
    detail: str | None = None


class DiagnosticSink(Protocol):
    def emit(self, event: DiagnosticEvent) -> None: ...


class JsonLineDiagnosticSink:
    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def emit(self, event: DiagnosticEvent) -> None:
        self._stream.write(
            json.dumps(asdict(event), sort_keys=True, separators=(",", ":")) + "\n"
        )
        self._stream.flush()


class NullDiagnosticSink:
    def emit(self, event: DiagnosticEvent) -> None:
        del event


class ServiceLifecycleError(RuntimeError):
    pass


class ServiceLoop:
    """Run one bounded unit at a time and drain on SIGTERM/SIGINT.

    ``step`` returns true when it completed useful work. ``start`` must be
    idempotent. ``close`` receives the configured deadline; it is additionally
    isolated in a daemon thread so a broken adapter cannot exceed that deadline.
    """

    def __init__(
        self,
        *,
        service: str,
        instance_id: str,
        step: Callable[[], bool],
        start: Callable[[], None] = lambda: None,
        close: Callable[[float], None] = lambda timeout_s: None,
        poll_interval_s: float = 1.0,
        shutdown_timeout_s: float = 10.0,
        diagnostics: DiagnosticSink | None = None,
    ) -> None:
        if poll_interval_s <= 0 or shutdown_timeout_s <= 0:
            raise ValueError("service intervals must be positive")
        self._service = service
        self._instance_id = instance_id
        self._step = step
        self._start = start
        self._close = close
        self._poll_interval_s = poll_interval_s
        self._shutdown_timeout_s = shutdown_timeout_s
        self._diagnostics = diagnostics or NullDiagnosticSink()
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._state = ServiceState.STOPPED
        self._started = False
        self._completed = 0
        self._failed = 0
        self._detail: str | None = None

    def health(self) -> HealthSnapshot:
        with self._lock:
            return HealthSnapshot(
                self._service,
                self._instance_id,
                self._state,
                self._state is ServiceState.READY,
                self._completed,
                self._failed,
                self._detail,
            )

    def request_stop(self) -> None:
        self._stop.set()
        changed = False
        with self._lock:
            if self._state in (ServiceState.STARTING, ServiceState.READY):
                self._state = ServiceState.DRAINING
                self._detail = None
                changed = True
        if changed:
            self._emit("draining")

    def run_once(self) -> bool:
        if self._stop.is_set():
            return False
        self._ensure_started()
        try:
            worked = self._step()
        except Exception as error:
            with self._lock:
                self._failed += 1
                self._state = ServiceState.FAILED
                self._detail = f"{type(error).__name__}: unit failed"
            self._emit("unit_failed")
            raise
        if worked:
            with self._lock:
                self._completed += 1
            self._emit("unit_completed")
        return worked

    def run_forever(self, *, max_iterations: int | None = None) -> None:
        if max_iterations is not None and max_iterations <= 0:
            raise ValueError("max_iterations must be positive")
        previous = self._install_signal_handlers()
        try:
            self._ensure_started()
            iterations = 0
            while not self._stop.is_set():
                worked = self.run_once()
                iterations += 1
                if max_iterations is not None and iterations >= max_iterations:
                    break
                if not worked:
                    self._stop.wait(self._poll_interval_s)
        finally:
            self.shutdown()
            self._restore_signal_handlers(previous)

    def shutdown(self) -> None:
        emit_draining = False
        with self._lock:
            if self._state is ServiceState.STOPPED:
                return
            was_failed = self._state is ServiceState.FAILED
            if self._state is not ServiceState.DRAINING:
                self._state = ServiceState.DRAINING
                emit_draining = True
            if not was_failed:
                self._detail = None
        if emit_draining:
            self._emit("draining")
        finished = threading.Event()
        failure: list[BaseException] = []

        def close() -> None:
            try:
                self._close(self._shutdown_timeout_s)
            except BaseException as error:  # noqa: BLE001 - lifecycle boundary
                failure.append(error)
            finally:
                finished.set()

        threading.Thread(
            target=close, name=f"{self._service}-shutdown", daemon=True
        ).start()
        completed = finished.wait(self._shutdown_timeout_s)
        with self._lock:
            if not completed:
                self._state = ServiceState.FAILED
                self._detail = "shutdown deadline exceeded"
            elif failure:
                self._state = ServiceState.FAILED
                self._detail = f"shutdown failed: {type(failure[0]).__name__}"
            elif was_failed:
                self._state = ServiceState.FAILED
            else:
                self._state = ServiceState.STOPPED
                self._detail = None
        self._emit(
            "stopped"
            if completed and not failure and not was_failed
            else "shutdown_failed"
        )
        if not completed or failure:
            raise ServiceLifecycleError(self.health().detail or "shutdown failed")

    def _ensure_started(self) -> None:
        with self._lock:
            if self._started:
                if self._state is not ServiceState.READY:
                    raise ServiceLifecycleError(
                        f"{self._state.value} service cannot accept work"
                    )
                return
            self._state = ServiceState.STARTING
        self._emit("starting")
        try:
            self._start()
        except Exception as error:
            with self._lock:
                self._state = ServiceState.FAILED
                self._detail = f"startup failed: {type(error).__name__}"
            self._emit("startup_failed")
            raise
        with self._lock:
            self._started = True
            self._state = ServiceState.READY
            self._detail = None
        self._emit("ready")

    def _emit(self, name: str) -> None:
        snapshot = self.health()
        self._diagnostics.emit(
            DiagnosticEvent(
                name,
                snapshot.service,
                snapshot.instance_id,
                snapshot.state.value,
                snapshot.completed_units,
                snapshot.failed_units,
                snapshot.detail,
            )
        )

    def _install_signal_handlers(self) -> dict[signal.Signals, Any]:
        if threading.current_thread() is not threading.main_thread():
            return {}
        previous: dict[signal.Signals, Any] = {}

        def stop(signum: int, frame: FrameType | None) -> None:
            del signum, frame
            self.request_stop()

        for candidate in (signal.SIGTERM, signal.SIGINT):
            previous[candidate] = signal.getsignal(candidate)
            signal.signal(candidate, stop)
        return previous

    @staticmethod
    def _restore_signal_handlers(
        previous: dict[signal.Signals, Any],
    ) -> None:
        for candidate, handler in previous.items():
            signal.signal(candidate, handler)
