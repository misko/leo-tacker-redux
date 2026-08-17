from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from leo_flow.deployments.release_verifier import (
    ReleaseVerificationError,
    verify_release,
)


def _sha(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _release(tmp_path: Path) -> tuple[Path, Path, str, str]:
    native = tmp_path / "native" / "lib"
    native.mkdir(parents=True)
    library = native / "libiio.so.0.25"
    library.write_bytes(b"reviewed-libiio")
    (native / "libiio.so.0").symlink_to("libiio.so.0.25")
    manifest = tmp_path / "release.manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "sealed_inventory": {
                    "native/lib/libiio.so.0": {"symlink_target": "libiio.so.0.25"},
                    "native/lib/libiio.so.0.25": {
                        "sha256": _sha(library),
                        "size_bytes": library.stat().st_size,
                    },
                }
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    manifest_digest = _sha(manifest)
    receipt = tmp_path / "validation.receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "checks": [{"name": "offline", "status": "passed"}],
                "contact": {"live_database": False, "radio": False, "services": False},
                "offline_only": True,
                "release_manifest_digest": manifest_digest,
            }
        )
    )
    return manifest, receipt, manifest_digest, _sha(receipt)


def _reseal(manifest: Path, receipt: Path) -> tuple[str, str]:
    root = manifest.parent
    inventory: dict[str, object] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path in {manifest, receipt}:
            continue
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            inventory[relative] = {"symlink_target": path.readlink().as_posix()}
        else:
            inventory[relative] = {
                "sha256": _sha(path),
                "size_bytes": path.stat().st_size,
            }
    manifest.write_text(
        json.dumps(
            {"sealed_inventory": inventory}, sort_keys=True, separators=(",", ":")
        )
    )
    manifest_digest = _sha(manifest)
    receipt.write_text(
        json.dumps(
            {
                "checks": [{"name": "offline", "status": "passed"}],
                "contact": {"live_database": False, "radio": False, "services": False},
                "offline_only": True,
                "release_manifest_digest": manifest_digest,
            }
        )
    )
    return manifest_digest, _sha(receipt)


def test_verifier_accepts_exact_closed_offline_release(tmp_path: Path) -> None:
    manifest, receipt, digest, receipt_digest = _release(tmp_path)

    verify_release(manifest, receipt, digest, receipt_digest)


@pytest.mark.parametrize("mutation", ["file", "extra", "receipt", "symlink"])
def test_verifier_fails_closed_on_release_mutation(
    tmp_path: Path, mutation: str
) -> None:
    manifest, receipt, digest, receipt_digest = _release(tmp_path)
    if mutation == "file":
        (tmp_path / "native/lib/libiio.so.0.25").write_bytes(b"changed")
    elif mutation == "extra":
        (tmp_path / "unlisted").write_text("shadow")
    elif mutation == "receipt":
        receipt.write_text("{}")
    else:
        (tmp_path / "native/lib/libiio.so.0").unlink()
        (tmp_path / "native/lib/libiio.so.0").symlink_to("../../outside")

    with pytest.raises(ReleaseVerificationError):
        verify_release(manifest, receipt, digest, receipt_digest)


def test_verifier_rejects_unarmed_manifest_digest(tmp_path: Path) -> None:
    manifest, receipt, _digest_value, receipt_digest = _release(tmp_path)

    with pytest.raises(ReleaseVerificationError, match="armed digest"):
        verify_release(manifest, receipt, "sha256:" + "0" * 64, receipt_digest)


def test_verifier_rejects_unarmed_receipt_digest(tmp_path: Path) -> None:
    manifest, receipt, manifest_digest, _receipt_digest = _release(tmp_path)

    with pytest.raises(ReleaseVerificationError, match="receipt digest"):
        verify_release(manifest, receipt, manifest_digest, "sha256:" + "0" * 64)


@pytest.mark.parametrize(
    "escaped",
    [
        "/tmp/release-build/release/native/iio.py",
        "/home/operator/gits/leo-tracker-redux/.venv/iio.py",
        "/home/operator/.cache/leo-flow/native/iio.py",
        "/opt/outside-release/native/iio.py",
    ],
)
def test_verifier_rejects_hash_valid_deleted_build_and_outside_runtime_paths(
    tmp_path: Path, escaped: str
) -> None:
    manifest, receipt, _digest_value, _receipt_digest = _release(tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    (config / "r20.station.json").write_text(
        json.dumps({"expected_runtime": {"iio_module_path": escaped}})
    )
    digest, receipt_digest = _reseal(manifest, receipt)

    with pytest.raises(ReleaseVerificationError, match="operative release path"):
        verify_release(manifest, receipt, digest, receipt_digest)


def test_verifier_accepts_final_release_paths_and_explicit_service_paths(
    tmp_path: Path,
) -> None:
    manifest, receipt, _digest_value, _receipt_digest = _release(tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    analysis = config / "analysis.json"
    analysis.write_text("{}")
    module = tmp_path / "native/lib/python3.11/site-packages/iio.py"
    module.parent.mkdir(parents=True)
    module.write_text("# sealed binding\n")
    (config / "r20.station.json").write_text(
        json.dumps(
            {
                "runtime_manifest": str(analysis),
                "expected_runtime": {"iio_module_path": str(module)},
                "state": {
                    "cas_root": "/srv/leo-flow/objects",
                    "state_root": "/var/lib/leo-flow/canary",
                },
            }
        )
    )
    (config / "runtime.json").write_text(
        json.dumps(
            {
                "analysis_config": str(analysis),
                "analysis_credential_directory": (
                    "/var/lib/leo-flow/credentials/gauss-analysis"
                ),
            }
        )
    )
    digest, receipt_digest = _reseal(manifest, receipt)

    verify_release(manifest, receipt, digest, receipt_digest)


@pytest.mark.parametrize(
    "invalid",
    ["native/iio.py", "/var/lib/leo-flow/../shadow/objects"],
)
def test_verifier_rejects_relative_or_noncanonical_recognized_paths(
    tmp_path: Path, invalid: str
) -> None:
    manifest, receipt, _digest_value, _receipt_digest = _release(tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    (config / "runtime.json").write_text(json.dumps({"analysis_config": invalid}))
    digest, receipt_digest = _reseal(manifest, receipt)

    with pytest.raises(ReleaseVerificationError, match="canonical and absolute"):
        verify_release(manifest, receipt, digest, receipt_digest)


def test_verifier_rejects_hash_valid_pyvenv_home_from_deleted_build_tree(
    tmp_path: Path,
) -> None:
    manifest, receipt, _digest_value, _receipt_digest = _release(tmp_path)
    venv = tmp_path / "venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text(
        "home = /tmp/leo-release-d-build.deleted/release/python/bin\n"
    )
    digest, receipt_digest = _reseal(manifest, receipt)

    with pytest.raises(ReleaseVerificationError, match="pyvenv home"):
        verify_release(manifest, receipt, digest, receipt_digest)


def test_verifier_accepts_pyvenv_home_materialized_under_final_root(
    tmp_path: Path,
) -> None:
    manifest, receipt, _digest_value, _receipt_digest = _release(tmp_path)
    home = tmp_path / "python/bin"
    home.mkdir(parents=True)
    venv = tmp_path / "venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text(f"home = {home}\n")
    digest, receipt_digest = _reseal(manifest, receipt)

    verify_release(manifest, receipt, digest, receipt_digest)


def test_verifier_recurses_arrays_and_rejects_unclassified_absolute_paths(
    tmp_path: Path,
) -> None:
    manifest, receipt, _digest_value, _receipt_digest = _release(tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    (config / "runtime.json").write_text(
        json.dumps({"nested": [{"shadow_path": "/tmp/deleted-build/shadow"}]})
    )
    digest, receipt_digest = _reseal(manifest, receipt)

    with pytest.raises(ReleaseVerificationError, match="unclassified absolute path"):
        verify_release(manifest, receipt, digest, receipt_digest)


@pytest.mark.parametrize("invalid", [None, 7, ["/tmp/deleted-build"]])
def test_verifier_rejects_non_string_recognized_path_fields(
    tmp_path: Path, invalid: object
) -> None:
    manifest, receipt, _digest_value, _receipt_digest = _release(tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    (config / "runtime.json").write_text(json.dumps({"analysis_config": invalid}))
    digest, receipt_digest = _reseal(manifest, receipt)

    with pytest.raises(ReleaseVerificationError, match="path must be a string"):
        verify_release(manifest, receipt, digest, receipt_digest)


@pytest.mark.parametrize(
    "invalid",
    [
        "/var/lib/credentials/gauss-analysis",
        "/var/lib/leo-flow/secrets/gauss-analysis",
        "/tmp/leo-flow/credentials/gauss-analysis",
    ],
)
def test_verifier_rejects_unscoped_or_temporary_credential_directories(
    tmp_path: Path, invalid: str
) -> None:
    manifest, receipt, _digest_value, _receipt_digest = _release(tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    (config / "runtime.json").write_text(
        json.dumps({"analysis_credential_directory": invalid})
    )
    digest, receipt_digest = _reseal(manifest, receipt)

    with pytest.raises(ReleaseVerificationError, match="credential|temporary"):
        verify_release(manifest, receipt, digest, receipt_digest)
