"""Reusable nonblocking process-mode exclusion for one local pipeline host."""

from __future__ import annotations

import fcntl
import os
import stat
from pathlib import Path


class ProcessModeLockError(RuntimeError):
    """The exact local capture/analysis mode cannot be acquired safely."""


class ExclusiveModeLock:
    """Own one advisory lock for the complete capture or analysis boundary."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("mode lock path must be absolute")
        self._path = path
        self._descriptor: int | None = None

    def acquire(self) -> None:
        if self._descriptor is not None:
            return
        descriptor: int | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self._path.parent.is_symlink():
                raise ProcessModeLockError("mode lock parent cannot be a symlink")
            flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self._path, flags, 0o600)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ProcessModeLockError("mode lock is not a regular file")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ProcessModeLockError:
            if descriptor is not None:
                os.close(descriptor)
            raise
        except BlockingIOError as error:
            if descriptor is not None:
                os.close(descriptor)
            raise ProcessModeLockError("pipeline mode is already owned") from error
        except OSError as error:
            if descriptor is not None:
                os.close(descriptor)
            raise ProcessModeLockError("mode lock cannot be opened") from error
        self._descriptor = descriptor

    def release(self) -> None:
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is None:
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
