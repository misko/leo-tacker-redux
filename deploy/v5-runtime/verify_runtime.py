#!/usr/bin/env python3
"""Fail-closed verification for the immutable Pluto V5 host runtime."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import pathlib
import sys
from collections.abc import Mapping, Sequence
from typing import Any


class VerificationError(RuntimeError):
    """The runtime does not match its reviewed manifest."""


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise VerificationError(f"{name} must be an object")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise VerificationError(f"{name} must be a non-empty string")
    return value


def load_manifest(path: pathlib.Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VerificationError(f"cannot read runtime manifest: {error}") from error
    manifest = _mapping(value, "manifest")
    if manifest.get("schema") != "leo-flow.v5-runtime/v1":
        raise VerificationError("unsupported runtime manifest schema")
    for key in ("runtime_id", "firmware", "libiio", "pyadi", "numpy", "spf"):
        if key not in manifest:
            raise VerificationError(f"manifest lacks {key}")
    libiio = _mapping(manifest["libiio"], "libiio")
    version = libiio.get("version")
    if (
        not isinstance(version, Sequence)
        or isinstance(version, str)
        or len(version) != 3
    ):
        raise VerificationError("libiio.version must contain major, minor, and git")
    files = _mapping(_mapping(manifest["spf"], "spf").get("files"), "spf.files")
    if not files:
        raise VerificationError("spf.files must not be empty")
    for relative, digest in files.items():
        _string(relative, "SPF relative path")
        text = _string(digest, f"digest for {relative}")
        if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
            raise VerificationError(f"invalid SHA-256 for {relative}")
    return manifest


def _distribution_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError as error:
        raise VerificationError(f"required distribution is absent: {name}") from error


def _verify_spf_files(manifest: Mapping[str, Any]) -> None:
    spf = _mapping(manifest["spf"], "spf")
    package = importlib.import_module("spf")
    roots = list(getattr(package, "__path__", ()))
    if len(roots) != 1:
        raise VerificationError(
            "SPF must resolve from exactly one installed package root"
        )
    package_root = pathlib.Path(roots[0]).parent
    for relative, expected in _mapping(spf["files"], "spf.files").items():
        source = package_root / str(relative)
        try:
            actual = hashlib.sha256(source.read_bytes()).hexdigest()
        except OSError as error:
            raise VerificationError(
                f"cannot read pinned SPF source {source}: {error}"
            ) from error
        if actual != expected:
            raise VerificationError(f"SPF source digest mismatch: {relative}")


def _loaded_libiio_paths() -> tuple[pathlib.Path, ...]:
    try:
        lines = pathlib.Path("/proc/self/maps").read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise VerificationError(
            f"cannot inspect loaded native libraries: {error}"
        ) from error
    return tuple(
        sorted(
            {
                pathlib.Path(line.rsplit(maxsplit=1)[-1]).resolve()
                for line in lines
                if "/" in line and "libiio.so" in line.rsplit(maxsplit=1)[-1]
            }
        )
    )


def _module_path(module: Any) -> str:
    value = getattr(module, "__file__", None)
    if not isinstance(value, str):
        raise VerificationError(f"loaded module lacks a source path: {module!r}")
    return str(pathlib.Path(value).resolve())


def verify_runtime(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    libiio = _mapping(manifest["libiio"], "libiio")
    pyadi = _mapping(manifest["pyadi"], "pyadi")
    numpy = _mapping(manifest["numpy"], "numpy")
    iio = importlib.import_module("iio")
    expected_version = tuple(libiio["version"])
    if tuple(iio.version) != expected_version:
        raise VerificationError(
            f"libiio version mismatch: observed {tuple(iio.version)!r}, expected {expected_version!r}"
        )
    if not hasattr(iio, "MetadataBuffer"):
        raise VerificationError("patched iio.MetadataBuffer is absent")
    binding_path = _module_path(iio)
    expected_binding_path = _string(libiio["binding_path"], "binding path")
    if binding_path != expected_binding_path:
        raise VerificationError(
            f"iio binding path mismatch: observed {binding_path}, expected {expected_binding_path}"
        )
    observed_backends = frozenset(str(item) for item in iio.backends)
    aliases = {"network": "ip"}
    required_backends = frozenset(
        aliases.get(str(item), str(item)) for item in libiio["required_backends"]
    )
    if not required_backends <= observed_backends:
        raise VerificationError(
            f"libiio backends are incomplete: observed {sorted(observed_backends)!r}, "
            f"required {sorted(required_backends)!r}"
        )
    for dependency in (pyadi, numpy):
        name = _string(dependency["distribution"], "distribution")
        expected = _string(dependency["version"], f"{name} version")
        observed = _distribution_version(name)
        if observed != expected:
            raise VerificationError(
                f"{name} version mismatch: observed {observed}, expected {expected}"
            )
    # The generated binding is installed as pylibiio by the libiio build.
    _distribution_version(
        _string(libiio["python_distribution"], "binding distribution")
    )
    prefix = pathlib.Path(_string(libiio["prefix"], "libiio prefix")).resolve()
    paths = _loaded_libiio_paths()
    if not paths or any(prefix not in path.parents for path in paths):
        raise VerificationError(
            f"libiio was not loaded exclusively from {prefix}: {paths!r}"
        )
    _verify_spf_files(manifest)
    adi = importlib.import_module("adi")
    pyadi_path = _module_path(adi)
    expected_pyadi_path = _string(pyadi["module_path"], "pyadi module path")
    if pyadi_path != expected_pyadi_path:
        raise VerificationError(
            f"pyadi module path mismatch: observed {pyadi_path}, expected {expected_pyadi_path}"
        )
    spf = _mapping(manifest["spf"], "spf")
    module_name, separator, symbol = _string(spf["import"], "SPF import").partition(":")
    spf_module = importlib.import_module(module_name)
    if not separator or not hasattr(spf_module, symbol):
        raise VerificationError("pinned SPF integration symbol is absent")
    spf_path = _module_path(spf_module)
    expected_spf_path = _string(spf["module_path"], "SPF module path")
    if spf_path != expected_spf_path:
        raise VerificationError(
            f"SPF module path mismatch: observed {spf_path}, expected {expected_spf_path}"
        )
    firmware = _mapping(manifest["firmware"], "firmware")
    return {
        "runtime_id": manifest["runtime_id"],
        "manifest_schema": manifest["schema"],
        "firmware_release": firmware["release"],
        "metadata_protocol": firmware["metadata_protocol"],
        "libiio_source_commit": libiio["commit"],
        "libiio_version": list(expected_version),
        "libiio_binding_path": binding_path,
        "libiio_native_paths": sorted(str(path) for path in paths),
        "libiio_backends": sorted(observed_backends),
        "pyadi_version": pyadi["version"],
        "pyadi_module_path": pyadi_path,
        "spf_source_commit": spf["commit"],
        "spf_import": spf["import"],
        "spf_module_path": spf_path,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest",
        type=pathlib.Path,
        default=pathlib.Path("/opt/leo-v5/runtime-manifest.json"),
    )
    parser.add_argument("--manifest-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        manifest = load_manifest(arguments.manifest)
        observation = None if arguments.manifest_only else verify_runtime(manifest)
    except VerificationError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    if arguments.json:
        print(
            json.dumps(
                observation or {"runtime_id": manifest["runtime_id"]}, sort_keys=True
            )
        )
    else:
        print(f"PASS: {manifest['runtime_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
