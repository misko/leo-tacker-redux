"""Local-runtime capture guard and host resource adapters."""

from __future__ import annotations

import fcntl
import os
import stat
from collections.abc import Callable
from pathlib import Path

from leo_flow.contracts.optional_heavy_work_admission import (
    FocusedCaptureGuardV0_1,
    HeavyWorkAdmissionDecisionV0_1,
    HeavyWorkAdmissionPermitV0_1,
    HeavyWorkResourceSnapshotV0_1,
    decode_focused_capture_guard_v0_1,
    encode_focused_capture_guard_v0_1,
)

_MAXIMUM_GUARD_BYTES = 4096


class AtomicFocusedCaptureGuardPublisherV0_1:
    """Publish bounded status under a local systemd runtime directory."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("capture guard path must be canonical absolute")
        self._path = path

    def publish(self, snapshot: FocusedCaptureGuardV0_1) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.{os.getpid()}.tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encode_focused_capture_guard_v0_1(snapshot))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


class LocalCaptureAwareHeavyWorkAdmissionV0_1:
    """Fail-closed policy evaluated immediately before a worker claims work."""

    def __init__(
        self,
        guard_path: Path,
        *,
        clock_ns: Callable[[], int],
        maximum_focused_backlog: int,
        host_cpu_cores: int,
        reserved_cpu_cores: int,
        estimated_claim_cpu_cores: int,
        minimum_memory_available_bytes: int,
        maximum_io_pressure_avg10: float,
        maximum_optional_concurrency: int,
        resource_probe: Callable[[], HeavyWorkResourceSnapshotV0_1] | None = None,
    ) -> None:
        if (
            not guard_path.is_absolute()
            or ".." in guard_path.parts
            or maximum_focused_backlog < 0
            or host_cpu_cores < 1
            or reserved_cpu_cores < 1
            or estimated_claim_cpu_cores < 1
            or minimum_memory_available_bytes < 0
            or maximum_io_pressure_avg10 < 0
            or maximum_optional_concurrency < 1
        ):
            raise ValueError("heavy-work admission configuration is invalid")
        self._path = guard_path
        self._clock_ns = clock_ns
        self._maximum_backlog = maximum_focused_backlog
        self._host_cpu_cores = host_cpu_cores
        self._reserved_cpu_cores = reserved_cpu_cores
        self._claim_cpu_cores = estimated_claim_cpu_cores
        self._minimum_memory = minimum_memory_available_bytes
        self._maximum_io_pressure = maximum_io_pressure_avg10
        self._maximum_concurrency = maximum_optional_concurrency
        self._resource_probe = resource_probe or self._probe_resources

    def acquire(
        self,
    ) -> tuple[HeavyWorkAdmissionDecisionV0_1, HeavyWorkAdmissionPermitV0_1 | None]:
        decision = self._decide()
        if not decision.admitted:
            return decision, None
        try:
            permit = self._acquire_slot()
        except OSError:
            return HeavyWorkAdmissionDecisionV0_1(False, "slot-unavailable"), None
        if permit is None:
            return HeavyWorkAdmissionDecisionV0_1(
                False, "optional-concurrency-full"
            ), None
        return decision, permit

    def _decide(self) -> HeavyWorkAdmissionDecisionV0_1:
        try:
            guard = self._read_guard()
        except (OSError, ValueError):
            return HeavyWorkAdmissionDecisionV0_1(False, "guard-unavailable")
        now = self._clock_ns()
        if now < guard.observed_utc_ns or now > guard.valid_until_utc_ns:
            return HeavyWorkAdmissionDecisionV0_1(False, "guard-stale")
        if (
            guard.continuous_capture_active
            and guard.capture_guard_from_utc_ns
            <= now
            <= guard.capture_guard_until_utc_ns
        ):
            return HeavyWorkAdmissionDecisionV0_1(False, "capture-guard-active")
        if guard.focused_backlog > self._maximum_backlog:
            return HeavyWorkAdmissionDecisionV0_1(False, "focused-backlog-high")
        try:
            resources = self._resource_probe()
        except (OSError, ValueError):
            return HeavyWorkAdmissionDecisionV0_1(False, "resources-unavailable")
        if (
            now < resources.observed_utc_ns
            or now - resources.observed_utc_ns > 5_000_000_000
        ):
            return HeavyWorkAdmissionDecisionV0_1(False, "resources-stale")
        if resources.cpu_count != self._host_cpu_cores:
            return HeavyWorkAdmissionDecisionV0_1(False, "resource-topology-mismatch")
        available_cpu = self._host_cpu_cores - resources.load_1m
        if available_cpu < self._reserved_cpu_cores + self._claim_cpu_cores:
            return HeavyWorkAdmissionDecisionV0_1(False, "cpu-pressure-high")
        if resources.memory_available_bytes < self._minimum_memory:
            return HeavyWorkAdmissionDecisionV0_1(False, "memory-pressure-high")
        if resources.io_pressure_avg10 > self._maximum_io_pressure:
            return HeavyWorkAdmissionDecisionV0_1(False, "io-pressure-high")
        return HeavyWorkAdmissionDecisionV0_1(True, "admitted")

    def _acquire_slot(self) -> HeavyWorkAdmissionPermitV0_1 | None:
        slots = self._path.parent / "optional-heavy-work-slots"
        slots.mkdir(mode=0o700, parents=True, exist_ok=True)
        for index in range(self._maximum_concurrency):
            path = slots / f"slot-{index}.lock"
            descriptor = os.open(
                path,
                os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                continue
            return _FlockPermit(descriptor)
        return None

    def _read_guard(self) -> FocusedCaptureGuardV0_1:
        descriptor = os.open(
            self._path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > _MAXIMUM_GUARD_BYTES
            ):
                raise ValueError("capture guard file is not a bounded regular file")
            payload = os.read(descriptor, _MAXIMUM_GUARD_BYTES + 1)
        finally:
            os.close(descriptor)
        return decode_focused_capture_guard_v0_1(payload)

    def _probe_resources(self) -> HeavyWorkResourceSnapshotV0_1:
        cpu_count = os.cpu_count() or 1
        load_1m = os.getloadavg()[0]
        available_kib: int | None = None
        with Path("/proc/meminfo").open(encoding="ascii") as stream:
            for line in stream:
                if line.startswith("MemAvailable:"):
                    available_kib = int(line.split()[1])
                    break
        if available_kib is None:
            raise ValueError("MemAvailable is missing")
        io_pressure: float | None = None
        with Path("/proc/pressure/io").open(encoding="ascii") as stream:
            for line in stream:
                if line.startswith("some "):
                    fields = dict(item.split("=", 1) for item in line.split()[1:])
                    io_pressure = float(fields["avg10"])
                    break
        if io_pressure is None:
            raise ValueError("I/O pressure is missing")
        return HeavyWorkResourceSnapshotV0_1(
            self._clock_ns(), cpu_count, load_1m, available_kib * 1024, io_pressure
        )


class _FlockPermit:
    def __init__(self, descriptor: int) -> None:
        self._descriptor: int | None = descriptor

    def release(self) -> None:
        if self._descriptor is None:
            return
        os.close(self._descriptor)
        self._descriptor = None
