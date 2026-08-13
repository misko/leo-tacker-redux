"""Explicit, narrowly scoped reader for systemd service credentials."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Final

_MAX_CREDENTIAL_BYTES: Final = 65_536
_CREDENTIAL_DIRECTORY_VARIABLE: Final = "CREDENTIALS_DIRECTORY"


class CredentialError(RuntimeError):
    """A named service credential cannot be read safely."""


class SystemdCredentialProvider:
    """Resolve names only beneath one explicit systemd credential directory.

    With no constructor argument, the directory is the one supplied by systemd
    in ``CREDENTIALS_DIRECTORY``. There is deliberately no fallback directory
    and no general environment-variable secret lookup.
    """

    def __init__(self, directory: Path | None = None) -> None:
        self._directory = directory

    def resolve(self, name: str) -> str:
        if not name or name in {".", ".."} or "/" in name or "\x00" in name:
            raise CredentialError("credential name is invalid")
        directory = self._directory
        if directory is None:
            value = os.environ.get(_CREDENTIAL_DIRECTORY_VARIABLE)
            if not value:
                raise CredentialError("systemd credential directory is unavailable")
            directory = Path(value)

        directory_fd: int | None = None
        credential_fd: int | None = None
        try:
            directory_fd = os.open(
                directory, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            credential_fd = os.open(
                name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory_fd
            )
            details = os.fstat(credential_fd)
            if not stat.S_ISREG(details.st_mode):
                raise CredentialError("credential is not a regular file")
            if details.st_size > _MAX_CREDENTIAL_BYTES:
                raise CredentialError("credential exceeds the size limit")
            payload = bytearray()
            while len(payload) <= _MAX_CREDENTIAL_BYTES:
                chunk = os.read(
                    credential_fd,
                    min(8192, _MAX_CREDENTIAL_BYTES + 1 - len(payload)),
                )
                if not chunk:
                    break
                payload.extend(chunk)
        except (OSError, ValueError) as error:
            raise CredentialError("credential cannot be read") from error
        finally:
            if credential_fd is not None:
                os.close(credential_fd)
            if directory_fd is not None:
                os.close(directory_fd)

        if len(payload) > _MAX_CREDENTIAL_BYTES:
            raise CredentialError("credential exceeds the size limit")
        try:
            value = bytes(payload).decode("utf-8")
        except UnicodeDecodeError as error:
            raise CredentialError("credential is not UTF-8 text") from error
        value = value.removesuffix("\n")
        if not value or "\x00" in value or "\r" in value or "\n" in value:
            raise CredentialError("credential text is invalid")
        return value
