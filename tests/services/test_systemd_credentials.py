from __future__ import annotations

import os

import pytest

from leo_flow.adapters.systemd_credentials import (
    CredentialError,
    SystemdCredentialProvider,
)


def test_reads_only_an_explicit_named_credential(tmp_path) -> None:
    credential = tmp_path / "catalog-dsn"
    credential.write_text("postgresql://dashboard@db/catalog\n", encoding="utf-8")
    provider = SystemdCredentialProvider(tmp_path)
    assert provider.resolve("catalog-dsn") == "postgresql://dashboard@db/catalog"


def test_regular_file_read_continues_across_short_os_reads(
    tmp_path, monkeypatch
) -> None:
    credential = tmp_path / "catalog-dsn"
    credential.write_text("postgresql://dashboard@db/catalog", encoding="utf-8")
    real_read = os.read
    calls = 0

    def short_read(fd: int, byte_count: int) -> bytes:
        nonlocal calls
        calls += 1
        return real_read(fd, min(3, byte_count))

    monkeypatch.setattr(os, "read", short_read)
    assert SystemdCredentialProvider(tmp_path).resolve("catalog-dsn") == (
        "postgresql://dashboard@db/catalog"
    )
    assert calls > 2


def test_standard_systemd_directory_requires_explicit_process_environment(
    tmp_path, monkeypatch
) -> None:
    (tmp_path / "catalog-dsn").write_text("dsn", encoding="utf-8")
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    with pytest.raises(CredentialError, match="unavailable"):
        SystemdCredentialProvider().resolve("catalog-dsn")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", os.fspath(tmp_path))
    assert SystemdCredentialProvider().resolve("catalog-dsn") == "dsn"


@pytest.mark.parametrize("name", ["", ".", "..", "../catalog-dsn", "a/b", "a\x00b"])
def test_credential_names_cannot_escape_the_provider_directory(tmp_path, name) -> None:
    with pytest.raises(CredentialError, match="name"):
        SystemdCredentialProvider(tmp_path).resolve(name)


def test_symlinks_multiline_and_empty_credentials_are_rejected(tmp_path) -> None:
    target = tmp_path / "target"
    target.write_text("secret", encoding="utf-8")
    (tmp_path / "linked").symlink_to(target)
    (tmp_path / "multiline").write_text("first\nsecond", encoding="utf-8")
    (tmp_path / "empty").write_text("", encoding="utf-8")
    provider = SystemdCredentialProvider(tmp_path)
    with pytest.raises(CredentialError, match="cannot be read"):
        provider.resolve("linked")
    with pytest.raises(CredentialError, match="text"):
        provider.resolve("multiline")
    with pytest.raises(CredentialError, match="text"):
        provider.resolve("empty")
