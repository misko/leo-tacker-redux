"""Root-confined access, recovery, and cleanup for local SigMF pairs."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from leo_flow.contracts.capture import CompletedLocalRecording
from leo_flow.contracts.core import PlanId, RecordingId

from .recording_codec import recover_completed_local_recording

_DATA_FILENAME = "recording.data"
_METADATA_FILENAME = "recording.meta"
_DIRECTORY_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


class LocalRecordingSecurityError(RuntimeError):
    """A locator could escape or mutate the configured local recording root."""


class LocalRecordingNotFinalizedError(RuntimeError):
    """An allocation has no complete, recoverable final pair."""


class RootedSigMFRecordingStore:
    """Access only exact two-file recording directories directly below one root.

    File opens are relative to no-follow directory descriptors. Persisted
    locators therefore cannot redirect publication or cleanup through symlinks,
    traversal components, or a sibling directory.
    """

    def __init__(self, recording_root: Path) -> None:
        supplied = Path(recording_root)
        try:
            if supplied.is_symlink():
                raise LocalRecordingSecurityError("recording root cannot be a symlink")
            self.recording_root = supplied.resolve(strict=True)
        except OSError as error:
            raise LocalRecordingSecurityError(
                "recording root is unavailable"
            ) from error
        if not self.recording_root.is_dir():
            raise LocalRecordingSecurityError("recording root must be a directory")

    def recover_finalized(
        self,
        recording_id: RecordingId,
        plan_id: PlanId,
        destination: str,
    ) -> CompletedLocalRecording:
        slot = self._slot_for_destination(destination)
        with self._open_pair(slot) as (data_stream, metadata_stream):
            return recover_completed_local_recording(
                data_stream,
                metadata_stream,
                data_locator=str(self.recording_root / slot / _DATA_FILENAME),
                metadata_locator=str(self.recording_root / slot / _METADATA_FILENAME),
                expected_recording_id=recording_id,
                expected_plan_id=plan_id,
            )

    def quarantine_incomplete(
        self, recording_id: RecordingId, destination: str
    ) -> Path | None:
        """Atomically isolate one exact partial directory; never touch a final pair."""

        slot = self._slot_for_destination(destination)
        partial = f"{slot}.partial"
        root_fd = self._open_root()
        quarantine_fd: int | None = None
        try:
            if self._entry_exists(root_fd, slot):
                raise LocalRecordingSecurityError(
                    "cannot quarantine while a final recording directory exists"
                )
            try:
                partial_stat = os.stat(partial, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                return None
            if not stat.S_ISDIR(partial_stat.st_mode):
                raise LocalRecordingSecurityError(
                    "partial recording entry is not a directory"
                )
            try:
                os.mkdir(".quarantine", mode=0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
            quarantine_fd = os.open(".quarantine", _DIRECTORY_FLAGS, dir_fd=root_fd)
            target = f"{recording_id}.{slot}.partial"
            if self._entry_exists(quarantine_fd, target):
                raise LocalRecordingSecurityError(
                    "quarantine destination already exists"
                )
            os.rename(
                partial,
                target,
                src_dir_fd=root_fd,
                dst_dir_fd=quarantine_fd,
            )
            os.fsync(quarantine_fd)
            os.fsync(root_fd)
            return self.recording_root / ".quarantine" / target
        except OSError as error:
            raise LocalRecordingSecurityError(
                "could not safely quarantine incomplete recording"
            ) from error
        finally:
            if quarantine_fd is not None:
                os.close(quarantine_fd)
            os.close(root_fd)

    @contextmanager
    def open_data(self, recording: CompletedLocalRecording) -> Iterator[BinaryIO]:
        slot = self._slot_for_recording(recording)
        with self._open_named(slot, _DATA_FILENAME) as stream:
            self._require_size(stream, recording.data_object.byte_count)
            yield stream

    @contextmanager
    def open_metadata(self, recording: CompletedLocalRecording) -> Iterator[BinaryIO]:
        slot = self._slot_for_recording(recording)
        with self._open_named(slot, _METADATA_FILENAME) as stream:
            self._require_size(stream, recording.metadata_object.byte_count)
            yield stream

    def cleanup(self, recording: CompletedLocalRecording) -> None:
        """Idempotently remove only an exact, root-confined finalized pair."""

        slot = self._slot_for_recording(recording)
        root_fd = self._open_root()
        parent_fd: int | None = None
        try:
            try:
                parent_fd = os.open(slot, _DIRECTORY_FLAGS, dir_fd=root_fd)
            except FileNotFoundError:
                return
            entries = set(os.listdir(parent_fd))
            unexpected = entries - {_DATA_FILENAME, _METADATA_FILENAME}
            if unexpected:
                raise LocalRecordingSecurityError(
                    "recording directory contains unexpected entries"
                )
            expected_sizes = {
                _DATA_FILENAME: recording.data_object.byte_count,
                _METADATA_FILENAME: recording.metadata_object.byte_count,
            }
            for name in entries:
                item = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (
                    not stat.S_ISREG(item.st_mode)
                    or item.st_size != expected_sizes[name]
                ):
                    raise LocalRecordingSecurityError(
                        "local recording object changed before cleanup"
                    )
            for name in (_DATA_FILENAME, _METADATA_FILENAME):
                try:
                    os.unlink(name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            os.fsync(parent_fd)
            os.rmdir(slot, dir_fd=root_fd)
            os.fsync(root_fd)
        except OSError as error:
            raise LocalRecordingSecurityError(
                "could not safely clean local recording"
            ) from error
        finally:
            if parent_fd is not None:
                os.close(parent_fd)
            os.close(root_fd)

    def _slot_for_recording(self, recording: CompletedLocalRecording) -> str:
        data = Path(recording.data_object.locator)
        metadata = Path(recording.metadata_object.locator)
        if (
            data.name != _DATA_FILENAME
            or metadata.name != _METADATA_FILENAME
            or data.parent != metadata.parent
        ):
            raise LocalRecordingSecurityError(
                "recording locators are not an exact SigMF pair"
            )
        slot = self._slot_for_destination(str(data.parent))
        expected_metadata = self.recording_root / slot / _METADATA_FILENAME
        if metadata != expected_metadata:
            raise LocalRecordingSecurityError("metadata locator escapes recording slot")
        return slot

    def _slot_for_destination(self, destination: str) -> str:
        path = Path(destination)
        if not path.is_absolute():
            raise LocalRecordingSecurityError("recording destination must be absolute")
        try:
            relative = path.relative_to(self.recording_root)
        except ValueError as error:
            raise LocalRecordingSecurityError(
                "recording destination escapes configured root"
            ) from error
        if len(relative.parts) != 1 or relative.parts[0] in {"", ".", ".."}:
            raise LocalRecordingSecurityError(
                "recording destination must be one direct child of its root"
            )
        slot = relative.parts[0]
        if slot == ".quarantine" or slot.endswith(".partial"):
            raise LocalRecordingSecurityError(
                "recording destination uses a reserved name"
            )
        return slot

    def _open_root(self) -> int:
        try:
            return os.open(self.recording_root, _DIRECTORY_FLAGS)
        except OSError as error:
            raise LocalRecordingSecurityError("recording root changed") from error

    @contextmanager
    def _open_named(self, slot: str, name: str) -> Iterator[BinaryIO]:
        root_fd = self._open_root()
        parent_fd: int | None = None
        file_fd: int | None = None
        try:
            try:
                parent_fd = os.open(slot, _DIRECTORY_FLAGS, dir_fd=root_fd)
                file_fd = self._open_regular_file(parent_fd, name)
            except FileNotFoundError as error:
                raise LocalRecordingNotFinalizedError(
                    "finalized local recording pair is absent"
                ) from error
            except OSError as error:
                raise LocalRecordingSecurityError(
                    "local recording path failed no-follow validation"
                ) from error
            stream = os.fdopen(file_fd, "rb", buffering=0)
            file_fd = None
            with stream:
                yield stream
        finally:
            if file_fd is not None:
                os.close(file_fd)
            if parent_fd is not None:
                os.close(parent_fd)
            os.close(root_fd)

    @contextmanager
    def _open_pair(self, slot: str) -> Iterator[tuple[BinaryIO, BinaryIO]]:
        root_fd = self._open_root()
        parent_fd: int | None = None
        data_fd: int | None = None
        metadata_fd: int | None = None
        try:
            try:
                parent_fd = os.open(slot, _DIRECTORY_FLAGS, dir_fd=root_fd)
                if set(os.listdir(parent_fd)) != {_DATA_FILENAME, _METADATA_FILENAME}:
                    raise LocalRecordingSecurityError(
                        "final recording directory is not an exact pair"
                    )
                data_fd = self._open_regular_file(parent_fd, _DATA_FILENAME)
                metadata_fd = self._open_regular_file(parent_fd, _METADATA_FILENAME)
            except FileNotFoundError as error:
                raise LocalRecordingNotFinalizedError(
                    "finalized local recording pair is absent"
                ) from error
            except OSError as error:
                raise LocalRecordingSecurityError(
                    "local recording pair failed no-follow validation"
                ) from error
            data_stream = os.fdopen(data_fd, "rb", buffering=0)
            data_fd = None
            metadata_stream = os.fdopen(metadata_fd, "rb", buffering=0)
            metadata_fd = None
            with data_stream, metadata_stream:
                yield data_stream, metadata_stream
        finally:
            if data_fd is not None:
                os.close(data_fd)
            if metadata_fd is not None:
                os.close(metadata_fd)
            if parent_fd is not None:
                os.close(parent_fd)
            os.close(root_fd)

    @staticmethod
    def _require_size(stream: BinaryIO, expected_bytes: int) -> None:
        if os.fstat(stream.fileno()).st_size != expected_bytes:
            raise LocalRecordingSecurityError(
                "local recording byte count changed before publication"
            )

    @staticmethod
    def _entry_exists(directory_fd: int, name: str) -> bool:
        try:
            os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    @staticmethod
    def _open_regular_file(directory_fd: int, name: str) -> int:
        descriptor = os.open(name, _FILE_FLAGS, dir_fd=directory_fd)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise LocalRecordingSecurityError(
                "local recording object is not a regular file"
            )
        return descriptor
