"""Offline verifier for one sealed Gauss release tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import NoReturn

_OPERATIVE_CONFIGS = frozenset(
    {
        "config/analysis.json",
        "config/native-runtime.json",
        "config/r20.station.json",
        "config/r21.station.json",
        "config/runtime.json",
        "config/science.json",
    }
)
_RELEASE_LOCAL_PATH_KEYS = frozenset(
    {
        "analysis_config",
        "binding_path",
        "iio_module_path",
        "module_path",
        "native_libiio_prefix",
        "native_runtime_config",
        "prefix",
        "pyadi_module_path",
        "runtime_manifest",
        "runtime_spec",
        "runtime_spec_path",
        "spf_module_path",
    }
)
_EXTERNAL_SERVICE_PATH_KEYS = frozenset(
    {
        "analysis_credential_directory",
        "capture_credential_directory",
        "dashboard_credential_directory",
        "cas_root",
        "lock_path",
        "mode_lock_path",
        "recording_root",
        "spool_database",
        "state_root",
    }
)
_CREDENTIAL_PATH_KEYS = frozenset(
    {
        "analysis_credential_directory",
        "capture_credential_directory",
        "dashboard_credential_directory",
    }
)


class ReleaseVerificationError(RuntimeError):
    """The release is not the exact sealed tree requested by the caller."""


def _fail(message: str) -> NoReturn:
    raise ReleaseVerificationError(message)


def _document(path: Path, label: str) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseVerificationError(f"{label} is unreadable") from error
    if not isinstance(value, dict):
        _fail(f"{label} must be an object")
    return value


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise ReleaseVerificationError(f"cannot hash {path.name}") from error
    return f"sha256:{digest.hexdigest()}"


def _relative_path(value: object) -> str:
    if not isinstance(value, str):
        _fail("inventory path must be a string")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        _fail("inventory path is not canonical and relative")
    return path.as_posix()


def _inventory(manifest: Mapping[str, object]) -> Mapping[str, object]:
    value = manifest.get("sealed_inventory")
    if not isinstance(value, dict) or not value:
        _fail("release manifest has no sealed inventory")
    return value


def _actual_entries(root: Path) -> dict[str, os.stat_result]:
    entries: dict[str, os.stat_result] = {}
    for directory, names, files in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in (*names, *files):
            path = base / name
            relative = path.relative_to(root).as_posix()
            mode = path.lstat()
            if stat.S_ISDIR(mode.st_mode) and not path.is_symlink():
                continue
            entries[relative] = mode
    return entries


def _config_path_values(
    value: object, *, keys: tuple[str, ...] = ()
) -> list[tuple[tuple[str, ...], str]]:
    found: list[tuple[tuple[str, ...], str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                _fail("operative config keys must be strings")
            if (
                key in _RELEASE_LOCAL_PATH_KEYS | _EXTERNAL_SERVICE_PATH_KEYS
                and not isinstance(child, str)
            ):
                _fail(
                    f"operative config path must be a string: {'.'.join((*keys, key))}"
                )
            found.extend(_config_path_values(child, keys=(*keys, key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_config_path_values(child, keys=(*keys, str(index))))
    elif isinstance(value, str) and (
        Path(value).is_absolute()
        or (keys and keys[-1] in _RELEASE_LOCAL_PATH_KEYS)
        or (keys and keys[-1] in _EXTERNAL_SERVICE_PATH_KEYS)
    ):
        found.append((keys, value))
    return found


def _canonical_absolute(value: str, relative: str, keys: tuple[str, ...]) -> Path:
    path = Path(value)
    if (
        not path.is_absolute()
        or str(PurePosixPath(value)) != value
        or ".." in PurePosixPath(value).parts
    ):
        _fail(
            "operative config path is not canonical and absolute: "
            f"{relative}:{'.'.join(keys)}"
        )
    return path


def _verify_pyvenv(root: Path, inventory: Mapping[str, object]) -> None:
    relative = "venv/pyvenv.cfg"
    if relative not in inventory:
        return
    home_values: list[str] = []
    try:
        for line in (root / relative).read_text().splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == "home":
                home_values.append(value.strip())
    except OSError as error:
        raise ReleaseVerificationError(
            "operative pyvenv config is unreadable"
        ) from error
    if len(home_values) != 1:
        _fail("operative pyvenv config has no unique home")
    home = _canonical_absolute(home_values[0], relative, ("home",))
    try:
        resolved = home.resolve(strict=True)
    except OSError as error:
        raise ReleaseVerificationError("operative pyvenv home is missing") from error
    if not resolved.is_relative_to(root):
        _fail("operative pyvenv home escapes final release root")


def _verify_operative_config_paths(root: Path, inventory: Mapping[str, object]) -> None:
    """Reject sealed configs that still name a build/cache/checkout tree.

    Only the small, explicit service-state allowlist may point outside a release.
    Runtime manifests, Python modules, native libraries, SPF modules, and other
    executable configuration must resolve to an existing object below the final
    (already sealed) release root.
    """

    root = root.resolve(strict=True)
    for relative in sorted(_OPERATIVE_CONFIGS.intersection(inventory)):
        document = _document(root / relative, f"operative config {relative}")
        for keys, value in _config_path_values(document):
            leaf = keys[-1] if keys else ""
            path = _canonical_absolute(value, relative, keys)
            if leaf in _RELEASE_LOCAL_PATH_KEYS:
                try:
                    resolved = path.resolve(strict=True)
                except OSError as error:
                    raise ReleaseVerificationError(
                        f"operative release path is missing: {relative}:{'.'.join(keys)}"
                    ) from error
                if not resolved.is_relative_to(root):
                    _fail(
                        "operative release path escapes final release root: "
                        f"{relative}:{'.'.join(keys)}"
                    )
                continue
            if leaf not in _EXTERNAL_SERVICE_PATH_KEYS:
                _fail(
                    "unclassified absolute path in operative config: "
                    f"{relative}:{'.'.join(keys)}"
                )
            if any(
                part in {"tmp", ".cache", "gits", "checkout"} for part in path.parts
            ):
                _fail(
                    "external service path names a temporary/cache/checkout tree: "
                    f"{relative}:{'.'.join(keys)}"
                )
            if leaf in _CREDENTIAL_PATH_KEYS and (
                "credentials" not in path.parts or "leo-flow" not in path.parts
            ):
                _fail(
                    "credential directory is not an explicit leo-flow service path: "
                    f"{relative}:{'.'.join(keys)}"
                )
    _verify_pyvenv(root, inventory)


def verify_release(
    manifest_path: Path,
    receipt_path: Path,
    expected_manifest_sha256: str,
    expected_receipt_sha256: str,
) -> None:
    """Rehash a closed release tree without contacting services or hardware."""

    manifest_path = manifest_path.resolve(strict=True)
    receipt_path = receipt_path.resolve(strict=True)
    root = manifest_path.parent
    if receipt_path.parent != root:
        _fail("manifest and receipt must share one release root")
    if manifest_path.name != "release.manifest.json":
        _fail("manifest must use the canonical release filename")
    if receipt_path.name != "validation.receipt.json":
        _fail("receipt must use the canonical validation filename")
    manifest_digest = _digest(manifest_path)
    if manifest_digest != expected_manifest_sha256:
        _fail("release manifest digest differs from the armed digest")
    if _digest(receipt_path) != expected_receipt_sha256:
        _fail("validation receipt digest differs from the armed digest")

    manifest = _document(manifest_path, "release manifest")
    receipt = _document(receipt_path, "validation receipt")
    if receipt.get("release_manifest_digest") != manifest_digest:
        _fail("validation receipt does not bind the release manifest")
    if receipt.get("offline_only") is not True:
        _fail("validation receipt is not offline-only")
    contact = receipt.get("contact")
    if not isinstance(contact, dict) or any(
        contact.get(key) is not False for key in ("live_database", "radio", "services")
    ):
        _fail("validation receipt records external contact")
    checks = receipt.get("checks")
    if not isinstance(checks, list) or not checks:
        _fail("validation receipt has no checks")
    if any(
        not isinstance(check, dict) or check.get("status") != "passed"
        for check in checks
    ):
        _fail("validation receipt contains a non-passing check")

    expected: dict[str, Mapping[str, object]] = {}
    for raw_path, raw_entry in _inventory(manifest).items():
        relative = _relative_path(raw_path)
        if relative in {manifest_path.name, receipt_path.name}:
            _fail("control files cannot appear in the sealed inventory")
        if not isinstance(raw_entry, dict) or relative in expected:
            _fail("sealed inventory entry is invalid")
        expected[relative] = raw_entry

    actual = _actual_entries(root)
    actual.pop(manifest_path.name, None)
    actual.pop(receipt_path.name, None)
    if set(actual) != set(expected):
        _fail("release tree differs from the closed inventory")

    for relative, entry in expected.items():
        path = root / relative
        mode = actual[relative]
        if "symlink_target" in entry:
            if not stat.S_ISLNK(mode.st_mode) or set(entry) != {"symlink_target"}:
                _fail(f"inventory type differs for {relative}")
            target = entry["symlink_target"]
            if not isinstance(target, str) or os.readlink(path) != target:
                _fail(f"symlink target differs for {relative}")
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root):
                _fail(f"symlink escapes release root: {relative}")
            continue
        if not stat.S_ISREG(mode.st_mode) or set(entry) != {"sha256", "size_bytes"}:
            _fail(f"inventory type differs for {relative}")
        digest = entry["sha256"]
        size = entry["size_bytes"]
        if not isinstance(digest, str) or not isinstance(size, int) or size < 0:
            _fail(f"inventory metadata is invalid for {relative}")
        if mode.st_size != size or _digest(path) != digest:
            _fail(f"release file differs from inventory: {relative}")

    _verify_operative_config_paths(root, expected)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--expected-receipt-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        verify_release(
            args.manifest,
            args.receipt,
            args.expected_manifest_sha256,
            args.expected_receipt_sha256,
        )
    except (OSError, ReleaseVerificationError) as error:
        parser.exit(4, f"release verification failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
