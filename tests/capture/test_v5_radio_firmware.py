from __future__ import annotations

import binascii
import hashlib
import json
import struct
from pathlib import Path

import pytest

from leo_flow.deployments.v5_radio_firmware import (
    V5RadioFirmwareVerificationError,
    verify_v5_radio_firmware_receipt,
)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_fixture(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    linked = tmp_path / "linked"
    linked.mkdir(parents=True)
    runtime = linked / "runtime.json"
    rootfs = linked / "rootfs.json"
    runtime.write_bytes(b'{"runtime":"candidate"}\n')
    rootfs.write_bytes(b'{"rootfs":"candidate"}\n')

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    itb_bytes = b"candidate-itb"
    dfu_without_crc = b"candidate-dfu" + struct.pack(
        "<HHHH3sB", 0xFFFF, 0xB673, 0x0456, 0x0100, b"UFD", 16
    )
    dfu_crc = (binascii.crc32(dfu_without_crc) ^ 0xFFFFFFFF) & 0xFFFFFFFF
    dfu_bytes = dfu_without_crc + struct.pack("<I", dfu_crc)
    itb_md5 = hashlib.md5(itb_bytes, usedforsecurity=False).hexdigest()
    frm_bytes = itb_bytes + itb_md5.encode("ascii") + b"\n"
    itb = artifacts / "candidate.itb"
    dfu = artifacts / "candidate.dfu"
    frm = artifacts / "candidate.frm"
    itb.write_bytes(itb_bytes)
    dfu.write_bytes(dfu_bytes)
    frm.write_bytes(frm_bytes)

    receipt_dir = tmp_path / "deploy" / "v5-runtime"
    receipt_dir.mkdir(parents=True)
    document: dict[str, object] = {
        "schema": "leo-flow.v5-radio-firmware-build-receipt/v1",
        "candidate_id": "candidate-1",
        "release_identity": "release-candidate-1",
        "candidate_runtime_manifest": {
            "path": "linked/runtime.json",
            "sha256": _sha256(runtime.read_bytes()),
        },
        "candidate_rootfs_receipt": {
            "path": "linked/rootfs.json",
            "sha256": _sha256(rootfs.read_bytes()),
        },
        "platform_source": {},
        "fit_components": [],
        "artifacts": {
            "itb": {
                "path": str(itb),
                "size_bytes": len(itb_bytes),
                "sha256": _sha256(itb_bytes),
                "md5": itb_md5,
            },
            "dfu": {
                "path": str(dfu),
                "size_bytes": len(dfu_bytes),
                "sha256": _sha256(dfu_bytes),
                "suffix": {
                    "bcd_device": "ffff",
                    "product_id": "b673",
                    "vendor_id": "0456",
                    "bcd_dfu": "0100",
                    "length_bytes": 16,
                    "crc32": f"{dfu_crc:08x}",
                },
            },
            "frm": {
                "path": str(frm),
                "size_bytes": len(frm_bytes),
                "sha256": _sha256(frm_bytes),
                "embedded_itb_md5": itb_md5,
            },
        },
        "verification": {
            "fit_all_six_components_extracted": True,
            "platform_components_match_exact_release": True,
            "ramdisk_matches_candidate_rootfs": True,
            "dfu_suffix_valid": True,
            "frm_footer_valid": True,
            "flashable_format": True,
            "radio_installed": False,
            "hardware_qualified": False,
        },
    }
    receipt = receipt_dir / "receipt.json"
    receipt.write_text(json.dumps(document), encoding="utf-8")
    return receipt, document


def test_firmware_verifier_closes_linked_receipts_and_three_artifacts(
    tmp_path: Path,
) -> None:
    receipt, _ = _write_fixture(tmp_path)

    receipt_digest = _sha256(receipt.read_bytes())
    verified = verify_v5_radio_firmware_receipt(receipt, receipt_digest)

    assert verified.candidate_id == "candidate-1"
    assert verified.release_identity == "release-candidate-1"
    assert verified.receipt_sha256 == receipt_digest


@pytest.mark.parametrize("artifact_name", ("itb", "dfu", "frm"))
def test_firmware_verifier_rejects_mutated_artifact(
    tmp_path: Path,
    artifact_name: str,
) -> None:
    receipt, document = _write_fixture(tmp_path)
    artifact = document["artifacts"][artifact_name]  # type: ignore[index]
    path = Path(artifact["path"])  # type: ignore[index]
    path.write_bytes(path.read_bytes() + b"mutated")

    with pytest.raises(V5RadioFirmwareVerificationError, match="size differs"):
        verify_v5_radio_firmware_receipt(receipt, _sha256(receipt.read_bytes()))


def test_firmware_verifier_rejects_link_mutation_and_false_packaging_proof(
    tmp_path: Path,
) -> None:
    receipt, document = _write_fixture(tmp_path)
    (tmp_path / "linked" / "runtime.json").write_bytes(b"changed")
    with pytest.raises(V5RadioFirmwareVerificationError, match="digest differs"):
        verify_v5_radio_firmware_receipt(receipt, _sha256(receipt.read_bytes()))

    (tmp_path / "linked" / "runtime.json").write_bytes(b'{"runtime":"candidate"}\n')
    document["verification"]["flashable_format"] = False  # type: ignore[index]
    receipt.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(V5RadioFirmwareVerificationError, match="packaging proof"):
        verify_v5_radio_firmware_receipt(receipt, _sha256(receipt.read_bytes()))


def test_firmware_verifier_rejects_symlink_artifact(tmp_path: Path) -> None:
    receipt, document = _write_fixture(tmp_path)
    artifact = document["artifacts"]["itb"]  # type: ignore[index]
    path = Path(artifact["path"])  # type: ignore[index]
    target = tmp_path / "real.itb"
    path.rename(target)
    path.symlink_to(target)

    with pytest.raises(V5RadioFirmwareVerificationError, match="regular file"):
        verify_v5_radio_firmware_receipt(receipt, _sha256(receipt.read_bytes()))


def test_firmware_verifier_rejects_wrong_receipt_digest(tmp_path: Path) -> None:
    receipt, _ = _write_fixture(tmp_path)

    with pytest.raises(V5RadioFirmwareVerificationError, match="receipt digest"):
        verify_v5_radio_firmware_receipt(receipt, "0" * 64)
