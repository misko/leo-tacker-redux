from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).parents[2]
RUNTIME = ROOT / "deploy" / "v5-runtime"
MANIFEST = RUNTIME / "manifest.json"
CANDIDATE_SOURCES = RUNTIME / "rx-integrity-candidate.sources.json"
CANDIDATE_MANIFEST = RUNTIME / "gauss-rx-integrity-candidate.manifest.json"
CANDIDATE_RADIO_ROOTFS = RUNTIME / "rx-integrity-candidate.radio-rootfs.json"
CANDIDATE_RADIO_FIRMWARE = RUNTIME / "rx-integrity-candidate.radio-firmware.json"


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
    assert manifest["psycopg"] == {
        "distribution": "psycopg",
        "version": "3.3.4",
    }
    assert manifest["spf"]["commit"] == "c40ee4116546889effd72056115adaaa1bc3fd40"
    assert manifest["supported_transports"] == ["ip", "usb"]
    assert manifest["unsupported_transports"] == ["direct-ip", "direct-usb"]


def test_rx_integrity_candidate_closes_over_exact_base_and_patch_bytes() -> None:
    candidate = json.loads(CANDIDATE_SOURCES.read_text(encoding="utf-8"))
    assert candidate["schema"] == "leo-flow.v5-runtime-source-overlay/v1"
    assert candidate["candidate_id"] == "gauss-v5-rx-integrity-close-barrier-1"
    assert candidate["base_runtime_manifest"] == {
        "path": "deploy/v5-runtime/gauss-development.manifest.json",
        "sha256": "1544c390d66a2a53c9b86dc0cf7a2fab63e9fca0a08563638744121b107f431f",
    }
    assert candidate["candidate_runtime_manifest"] == {
        "path": "deploy/v5-runtime/gauss-rx-integrity-candidate.manifest.json",
        "sha256": hashlib.sha256(CANDIDATE_MANIFEST.read_bytes()).hexdigest(),
    }
    assert candidate["libiio"]["base_commit"] == (
        "c26258bfa33098c2b215e19cf85d448e89499b1a"
    )
    assert candidate["libiio"]["required_builds"] == ["host", "radio"]
    assert candidate["spf"]["base_commit"] == (
        "c40ee4116546889effd72056115adaaa1bc3fd40"
    )
    assert candidate["spf"]["required_builds"] == ["host"]
    for component_name in ("libiio", "spf"):
        patch = candidate[component_name]["patch"]
        patch_path = ROOT / patch["path"]
        assert patch_path.is_file()
        assert hashlib.sha256(patch_path.read_bytes()).hexdigest() == patch["sha256"]
    assert candidate["promotion_requirements"] == {
        "new_firmware_release_identity": True,
        "new_host_runtime_identity": True,
        "radio_flash_required": True,
        "fresh_single_refill_qualification_required": True,
        "fresh_nine_cell_qualification_required": True,
        "reuse_prior_science_results": False,
    }


def test_rx_integrity_candidate_manifest_requires_distinct_runtime_and_firmware() -> (
    None
):
    module = _verifier_module()
    candidate = module.load_manifest(CANDIDATE_MANIFEST)
    base = module.load_manifest(RUNTIME / "gauss-development.manifest.json")
    assert candidate["runtime_id"] == "gauss-pluto-v5-rx-integrity-close-barrier-1"
    assert candidate["runtime_id"] != base["runtime_id"]
    assert candidate["firmware"]["release"] != base["firmware"]["release"]
    assert candidate["libiio"]["base_commit"] == base["libiio"]["commit"]
    assert candidate["libiio"]["patch_sha256"] == (
        "195bddceada230ef32b662cfd7149186a623d1d4cfac234b0660770f32f901d4"
    )
    assert candidate["spf"]["base_commit"] == base["spf"]["commit"]
    assert candidate["spf"]["patch_sha256"] == (
        "c9113a6d75466b4d1de38b45ccaee785c6a13a677b1e02c0e7c39919b66669b1"
    )


def test_rx_integrity_candidate_radio_rootfs_receipt_is_exact_and_not_flashable() -> (
    None
):
    receipt = json.loads(CANDIDATE_RADIO_ROOTFS.read_text(encoding="utf-8"))

    assert receipt["schema"] == "leo-flow.v5-radio-rootfs-build-receipt/v1"
    assert receipt["candidate_id"] == "gauss-v5-rx-integrity-close-barrier-1"
    assert receipt["release_identity"] == (
        "v0.38-plutoplus-spf-libiio-metadata-v5-rx-integrity-candidate1"
    )
    assert receipt["source"] == {
        "repository": "git@github.com:misko/plutosdr-fw.git",
        "commit": "de830094a177daf4f577b60b9d3324b41f99ae58",
        "buildroot_commit": "684ecbcbe44bc82043caae7091f12c02cbd02d8b",
        "libiio_base_commit": "c26258bfa33098c2b215e19cf85d448e89499b1a",
        "libiio_patch_sha256": (
            "195bddceada230ef32b662cfd7149186a623d1d4cfac234b0660770f32f901d4"
        ),
        "spf_metadata_source_commit": "ab270f9e3128187372f27de887be65353f9e195d",
    }
    assert receipt["build"]["artifact_kind"] == "rootfs.cpio.gz"
    assert receipt["build"]["artifact_size_bytes"] == 7_115_868
    assert receipt["build"]["artifact_sha256"] == (
        "a57ed73a07693b3ac94a456a87392897d49430dc8a6d9cc7aa10b0ed37642269"
    )
    assert receipt["contents"]["usr/sbin/iiod"]["sha256"] == (
        "87a65a439b323ac6aa75cc52004f398796b4336e5c50c73c7a1c4c62a0995fd5"
    )
    assert receipt["contents"]["usr/lib/libiio.so.0.25"]["sha256"] == (
        "29e24ce1f175a1c35ee25ee601ca95bca85689eb33686711fbe1df98e1eb827c"
    )
    assert (
        receipt["contents"]["opt/VERSIONS"]["device_fw"] == receipt["release_identity"]
    )
    assert receipt["qualification"] == {
        "source_patch_applied": True,
        "arm_compile_succeeded": True,
        "rootfs_packaging_succeeded": True,
        "full_itb_or_frm_built": False,
        "flashable": False,
        "radio_installed": False,
        "hardware_qualified": False,
        "blockers": [
            "No attested system_top.xsa/system_top.bit is present for full ITB/FRM packaging",
            "The candidate has not been installed on either radio",
            "Fresh signal-integrity and nine-cell hardware qualification have not run",
        ],
    }


def test_rx_integrity_candidate_radio_firmware_receipt_closes_flashable_bytes() -> None:
    receipt = json.loads(CANDIDATE_RADIO_FIRMWARE.read_text(encoding="utf-8"))

    assert receipt["schema"] == "leo-flow.v5-radio-firmware-build-receipt/v1"
    assert receipt["candidate_id"] == "gauss-v5-rx-integrity-close-barrier-1"
    assert receipt["candidate_runtime_manifest"]["sha256"] == (
        "0a9cf278bf836655afbf7a9a324a21c5dc41235d1b251386a0013eb0f299f123"
    )
    assert (
        receipt["candidate_rootfs_receipt"]["sha256"]
        == hashlib.sha256(CANDIDATE_RADIO_ROOTFS.read_bytes()).hexdigest()
    )
    assert receipt["platform_source"]["release_tag"] == (
        "v0.38-plutoplus-spf-libiio-metadata-v5"
    )
    assert [component["index"] for component in receipt["fit_components"]] == list(
        range(6)
    )
    assert [component["sha256"] for component in receipt["fit_components"]] == [
        "43767e4d8d0cfbfd4f9b97a22a61719d1fffae587f90269fb85aa8fd851df116",
        "31c552e28736e1f7bebf4feacabca7460e0386dbb61cdeb93cc313f990f81591",
        "298ea3a35c655f1fd47e4bdb10901707bee1a1d166c30ac70c8e0abf0d1e54a3",
        "365780eef2c99e4f09d1bed94ad093a2284163893f6cb619c1313b3f05bd58ee",
        "67ab0fba439980cee4e2d3c1a674b3274a899f10694d357d3dee02055a2bc5da",
        "a57ed73a07693b3ac94a456a87392897d49430dc8a6d9cc7aa10b0ed37642269",
    ]
    assert receipt["artifacts"] == {
        "itb": {
            "path": "/home/mouse9911/gits/plutosdr-fw/build/"
            "plutoplus-spf-libiio-metadata-v5-rx-integrity-candidate1.itb",
            "size_bytes": 12_712_727,
            "sha256": (
                "7e78616a52deea6a4055e5ec51ab71b59c431e24b359e3ab16e0fac000717efd"
            ),
            "md5": "650a7c07ba6f30cf5b71437d6c25821d",
        },
        "dfu": {
            "path": "/home/mouse9911/gits/plutosdr-fw/build/"
            "plutoplus-spf-libiio-metadata-v5-rx-integrity-candidate1.dfu",
            "size_bytes": 12_712_743,
            "sha256": (
                "4118a4f3a7130e407f4314e76415bbcf9183501e74faae1824ef2b52be616503"
            ),
            "suffix": {
                "bcd_device": "ffff",
                "product_id": "b673",
                "vendor_id": "0456",
                "bcd_dfu": "0100",
                "length_bytes": 16,
                "crc32": "7d976188",
            },
        },
        "frm": {
            "path": "/home/mouse9911/gits/plutosdr-fw/build/"
            "plutoplus-spf-libiio-metadata-v5-rx-integrity-candidate1.frm",
            "size_bytes": 12_712_760,
            "sha256": (
                "df391788052ef9a647d0b7b530e33dffea874f53f0e2beff9cd5f0228c25b8bb"
            ),
            "embedded_itb_md5": "650a7c07ba6f30cf5b71437d6c25821d",
        },
    }
    assert receipt["verification"] == {
        "fit_all_six_components_extracted": True,
        "platform_components_match_exact_release": True,
        "ramdisk_matches_candidate_rootfs": True,
        "dfu_suffix_valid": True,
        "frm_footer_valid": True,
        "flashable_format": True,
        "radio_installed": False,
        "hardware_qualified": False,
        "blockers": [
            "The candidate has not been installed and re-attested on either radio",
            "Fresh single-refill signal-integrity qualification has not run",
            "Fresh synchronized nine-cell qualification has not run",
        ],
    }


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
    assert '"psycopg[binary]==${PSYCOPG_VERSION}"' in dockerfile
    assert "import psycopg" in dockerfile
    assert "FROM runtime AS dependency-refresh-test" in dockerfile
    assert "--force-reinstall pylibiio==0.25" in dockerfile
    assert "ordinary PyPI pylibiio escaped runtime verification" in dockerfile
    assert (
        "COPY deploy/v5-canary/capture.json /opt/leo-v5/deploy/v5-canary-capture.json"
    ) in dockerfile
    assert 'ENTRYPOINT ["/opt/leo-v5/bin/runtime-entrypoint"]' in dockerfile
