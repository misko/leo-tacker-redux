from __future__ import annotations

import hashlib
import json
from pathlib import Path

SOURCE = Path("deploy/gauss-main-r20-r21-v1/native-release-d.source.json")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_native_release_sources_are_exact_reviewed_bytes() -> None:
    document = json.loads(SOURCE.read_text())

    assert document["schema"] == "org.leo-flow.gauss-native-release-source/v1"
    assert len(document["files"]) == 7
    assert all(
        _sha(Path(entry["source"])) == entry["sha256"] for entry in document["files"]
    )
    assert {entry["destination"] for entry in document["files"]} == {
        "native/lib/libiio.so.0.25",
        "native/lib/python3.11/site-packages/iio.py",
        "native/spf/spf/__init__.py",
        "native/spf/spf/direct_radio/__init__.py",
        "native/spf/spf/direct_radio/iio_metadata.py",
        "native/spf/spf/direct_radio/sample_clock.py",
        "native/spf/spf/direct_radio/usb_protocol.py",
    }
    assert document["symlinks"] == {
        "native/lib/libiio.so": "libiio.so.0",
        "native/lib/libiio.so.0": "libiio.so.0.25",
    }


def test_native_release_binds_exact_host_abi_dependencies() -> None:
    document = json.loads(SOURCE.read_text())

    assert len(document["host_dependencies"]) == 7
    assert all(
        _sha(Path(entry["path"])) == entry["sha256"]
        for entry in document["host_dependencies"]
    )
