"""Offline verification for one immutable V5 radio firmware package receipt."""

from __future__ import annotations

import binascii
import hashlib
import json
import stat
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_RECEIPT_SCHEMA = "leo-flow.v5-radio-firmware-build-receipt/v1"
_MAX_RECEIPT_BYTES = 1_048_576
_MAX_ARTIFACT_BYTES = 134_217_728
_DFU_SUFFIX = struct.Struct("<HHHH3sBI")


class V5RadioFirmwareVerificationError(ValueError):
    """A candidate receipt or one of its exact files fails closed."""


@dataclass(frozen=True, slots=True)
class VerifiedV5RadioFirmware:
    candidate_id: str
    release_identity: str
    receipt_sha256: str
    itb_sha256: str
    dfu_sha256: str
    frm_sha256: str


def verify_v5_radio_firmware_receipt(
    path: Path, expected_receipt_sha256: str
) -> VerifiedV5RadioFirmware:
    """Verify exact linked receipts and flashable artifacts without hardware I/O."""

    document, content = _json_document(path)
    receipt_digest = hashlib.sha256(content).hexdigest()
    if receipt_digest != _sha256(expected_receipt_sha256):
        raise V5RadioFirmwareVerificationError("firmware receipt digest differs")
    _exact_keys(
        document,
        {
            "schema",
            "candidate_id",
            "release_identity",
            "candidate_runtime_manifest",
            "candidate_rootfs_receipt",
            "platform_source",
            "fit_components",
            "artifacts",
            "verification",
        },
        "firmware receipt",
    )
    if _string(document["schema"], "schema") != _RECEIPT_SCHEMA:
        raise V5RadioFirmwareVerificationError("unsupported firmware receipt schema")
    candidate_id = _string(document["candidate_id"], "candidate_id")
    release_identity = _string(document["release_identity"], "release_identity")
    _verify_link(path, _object(document["candidate_runtime_manifest"], "runtime"))
    _verify_link(path, _object(document["candidate_rootfs_receipt"], "rootfs"))

    verification = _object(document["verification"], "verification")
    required_proofs = (
        "fit_all_six_components_extracted",
        "platform_components_match_exact_release",
        "ramdisk_matches_candidate_rootfs",
        "dfu_suffix_valid",
        "frm_footer_valid",
        "flashable_format",
    )
    if any(verification.get(name) is not True for name in required_proofs):
        raise V5RadioFirmwareVerificationError(
            "firmware receipt lacks a required packaging proof"
        )
    if verification.get("radio_installed") is not False:
        raise V5RadioFirmwareVerificationError(
            "offline candidate receipt must not claim radio installation"
        )
    if verification.get("hardware_qualified") is not False:
        raise V5RadioFirmwareVerificationError(
            "offline candidate receipt must not claim hardware qualification"
        )

    artifacts = _object(document["artifacts"], "artifacts")
    _exact_keys(artifacts, {"itb", "dfu", "frm"}, "artifacts")
    itb = _artifact(artifacts["itb"], "itb")
    dfu = _artifact(artifacts["dfu"], "dfu")
    frm = _artifact(artifacts["frm"], "frm")
    itb_bytes = _verify_artifact(itb, "itb")
    dfu_bytes = _verify_artifact(dfu, "dfu")
    frm_bytes = _verify_artifact(frm, "frm")
    _verify_dfu_suffix(dfu, dfu_bytes)
    _verify_frm_footer(frm, frm_bytes, itb_bytes)
    return VerifiedV5RadioFirmware(
        candidate_id,
        release_identity,
        receipt_digest,
        _string(itb["sha256"], "itb.sha256"),
        _string(dfu["sha256"], "dfu.sha256"),
        _string(frm["sha256"], "frm.sha256"),
    )


def _json_document(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        details = path.lstat()
        if not stat.S_ISREG(details.st_mode) or details.st_size > _MAX_RECEIPT_BYTES:
            raise V5RadioFirmwareVerificationError(
                "firmware receipt is not a bounded regular file"
            )
        content = path.read_bytes()
        value = json.loads(content.decode("utf-8"))
    except V5RadioFirmwareVerificationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise V5RadioFirmwareVerificationError(
            "firmware receipt cannot be read"
        ) from error
    return _object(value, "firmware receipt"), content


def _verify_link(receipt_path: Path, value: dict[str, Any]) -> None:
    _exact_keys(value, {"path", "sha256"}, "linked receipt")
    linked = _resolve_link(receipt_path, _string(value["path"], "linked path"))
    expected = _sha256(_string(value["sha256"], "linked sha256"))
    content = _bounded_regular_bytes(linked, _MAX_RECEIPT_BYTES, "linked receipt")
    if hashlib.sha256(content).hexdigest() != expected:
        raise V5RadioFirmwareVerificationError("linked receipt digest differs")


def _resolve_link(receipt_path: Path, name: str) -> Path:
    selected = Path(name)
    if selected.is_absolute() or ".." in selected.parts:
        raise V5RadioFirmwareVerificationError(
            "linked receipt path must be repository-relative"
        )
    matches = [
        parent / selected
        for parent in receipt_path.resolve().parents
        if (parent / selected).is_file()
    ]
    if len(matches) != 1:
        raise V5RadioFirmwareVerificationError(
            "linked receipt path does not resolve exactly once"
        )
    return matches[0]


def _artifact(value: object, name: str) -> dict[str, Any]:
    artifact = _object(value, name)
    required = {"path", "size_bytes", "sha256"}
    allowed = required | ({"md5"} if name == "itb" else set())
    if name == "dfu":
        allowed.add("suffix")
    if name == "frm":
        allowed.add("embedded_itb_md5")
    _exact_keys(artifact, allowed, name)
    return artifact


def _verify_artifact(value: dict[str, Any], name: str) -> bytes:
    path = Path(_string(value["path"], f"{name}.path"))
    if not path.is_absolute():
        raise V5RadioFirmwareVerificationError(f"{name} path must be absolute")
    expected_size = _positive_int(value["size_bytes"], f"{name}.size_bytes")
    content = _bounded_regular_bytes(path, _MAX_ARTIFACT_BYTES, name)
    if len(content) != expected_size:
        raise V5RadioFirmwareVerificationError(f"{name} size differs")
    expected_digest = _sha256(_string(value["sha256"], f"{name}.sha256"))
    if hashlib.sha256(content).hexdigest() != expected_digest:
        raise V5RadioFirmwareVerificationError(f"{name} digest differs")
    return content


def _verify_dfu_suffix(value: dict[str, Any], content: bytes) -> None:
    if len(content) < _DFU_SUFFIX.size:
        raise V5RadioFirmwareVerificationError("DFU suffix is truncated")
    suffix = _object(value["suffix"], "dfu.suffix")
    _exact_keys(
        suffix,
        {
            "bcd_device",
            "product_id",
            "vendor_id",
            "bcd_dfu",
            "length_bytes",
            "crc32",
        },
        "dfu.suffix",
    )
    bcd_device, product, vendor, bcd_dfu, signature, length, stored_crc = (
        _DFU_SUFFIX.unpack(content[-_DFU_SUFFIX.size :])
    )
    observed = {
        "bcd_device": f"{bcd_device:04x}",
        "product_id": f"{product:04x}",
        "vendor_id": f"{vendor:04x}",
        "bcd_dfu": f"{bcd_dfu:04x}",
        "length_bytes": length,
        "crc32": f"{stored_crc:08x}",
    }
    if signature != b"UFD" or observed != suffix:
        raise V5RadioFirmwareVerificationError("DFU suffix differs")
    calculated = binascii.crc32(content[:-4]) ^ 0xFFFFFFFF
    if calculated & 0xFFFFFFFF != stored_crc:
        raise V5RadioFirmwareVerificationError("DFU suffix CRC differs")


def _verify_frm_footer(
    value: dict[str, Any], content: bytes, itb_content: bytes
) -> None:
    expected_md5 = _md5(_string(value["embedded_itb_md5"], "frm md5"))
    expected_footer = expected_md5.encode("ascii") + b"\n"
    if not content.endswith(expected_footer):
        raise V5RadioFirmwareVerificationError("FRM footer differs")
    if content[: -len(expected_footer)] != itb_content:
        raise V5RadioFirmwareVerificationError("FRM body differs from ITB")
    if hashlib.md5(itb_content, usedforsecurity=False).hexdigest() != expected_md5:
        raise V5RadioFirmwareVerificationError("FRM embedded ITB MD5 differs")


def _bounded_regular_bytes(path: Path, maximum: int, name: str) -> bytes:
    try:
        details = path.lstat()
        if not stat.S_ISREG(details.st_mode) or details.st_size > maximum:
            raise V5RadioFirmwareVerificationError(
                f"{name} is not a bounded regular file"
            )
        return path.read_bytes()
    except V5RadioFirmwareVerificationError:
        raise
    except OSError as error:
        raise V5RadioFirmwareVerificationError(f"{name} cannot be read") from error


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise V5RadioFirmwareVerificationError(f"{name} must be an object")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise V5RadioFirmwareVerificationError(f"{name} fields are not exact")


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise V5RadioFirmwareVerificationError(f"{name} must be a bounded string")
    return value


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise V5RadioFirmwareVerificationError(f"{name} must be positive")
    return value


def _sha256(value: str) -> str:
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise V5RadioFirmwareVerificationError("SHA-256 value is malformed")
    return value


def _md5(value: str) -> str:
    if len(value) != 32 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise V5RadioFirmwareVerificationError("MD5 value is malformed")
    return value
