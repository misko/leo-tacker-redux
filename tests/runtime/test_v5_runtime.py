from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).parents[2]
RUNTIME = ROOT / "deploy" / "v5-runtime"
MANIFEST = RUNTIME / "manifest.json"


def _verifier_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(
        "v5_verify_runtime", RUNTIME / "verify_runtime.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_has_the_qualified_immutable_sources() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["schema"] == "leo-flow.v5-runtime/v1"
    assert manifest["libiio"] == {
        "repository": "https://github.com/misko/libiio.git",
        "ref": "spf-frame-metadata-source/v0.25-final-v3",
        "commit": "c26258bfa33098c2b215e19cf85d448e89499b1a",
        "version": [0, 25, "c26258b"],
        "prefix": "/opt/leo-v5",
        "binding_path": "/usr/local/lib/python3.11/dist-packages/iio.py",
        "required_backends": ["local", "ip", "usb"],
        "python_distribution": "pylibiio",
        "python_symbol": "iio.MetadataBuffer",
    }
    assert manifest["pyadi"]["version"] == "0.0.21"
    assert manifest["spf"]["commit"] == "c40ee4116546889effd72056115adaaa1bc3fd40"
    assert manifest["supported_transports"] == ["ip", "usb"]
    assert manifest["unsupported_transports"] == ["direct-ip", "direct-usb"]


def test_manifest_validation_is_fail_closed(tmp_path: pathlib.Path) -> None:
    module = _verifier_module()
    valid = module.load_manifest(MANIFEST)
    assert valid["runtime_id"] == "pluto-v5-libiio-0.25-spfmeta3"

    document = dict(valid)
    document["schema"] = "future"
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(module.VerificationError, match="schema"):
        module.load_manifest(invalid)


def test_manifest_only_verification_needs_no_hardware_or_optional_library() -> None:
    result = subprocess.run(
        [
            "python3",
            str(RUNTIME / "verify_runtime.py"),
            "--manifest",
            str(MANIFEST),
            "--manifest-only",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "pluto-v5-libiio-0.25-spfmeta3" in result.stdout


def test_image_installs_generated_binding_after_pyadi_without_dependencies() -> None:
    dockerfile = (RUNTIME / "Dockerfile").read_text(encoding="utf-8")
    assert "pyadi-iio==${PYADI_VERSION}" in dockerfile
    assert "--no-deps" in dockerfile
    assert "/wheels/pylibiio-*.whl" in dockerfile
    assert dockerfile.index("pyadi-iio==${PYADI_VERSION}") < dockerfile.index(
        "/wheels/pylibiio-*.whl"
    )
    assert "LD_LIBRARY_PATH" not in dockerfile
    assert "PYTHONPATH" not in dockerfile
    assert "pip check" in dockerfile
    assert "FROM runtime AS dependency-refresh-test" in dockerfile
    assert "--force-reinstall pylibiio==0.25" in dockerfile
    assert "ordinary PyPI pylibiio escaped runtime verification" in dockerfile
    assert 'ENTRYPOINT ["/opt/leo-v5/bin/runtime-entrypoint"]' in dockerfile
