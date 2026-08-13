from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from leo_flow.capture.qualification import (
    CaptureQualificationRunner,
    QualificationProfile,
)
from leo_flow.contracts.capture import GainMode, GainSetting, SegmentManifest
from leo_flow.contracts.core import RadioId, ReceiverChainId, UtcNs

RADIO_ID = RadioId("radio_qualification")
RECEIVERS = (ReceiverChainId("rx_a"), ReceiverChainId("rx_b"))


class QualificationRadio:
    radio_id = RADIO_ID

    def __init__(
        self, *, corrupt_prefix: bool = False, fail_wide: bool = False
    ) -> None:
        self.corrupt_prefix = corrupt_prefix
        self.fail_wide = fail_wide

    def acquire_segment(self, request, write_ci16):
        if self.fail_wide and str(request.segment_id).endswith("wide"):
            raise OSError("injected wide failure")
        assert request.sample_count is not None
        remaining = request.sample_count
        sample_offset = 0
        while remaining:
            count = min(3, remaining)
            block = bytearray()
            for sample in range(sample_offset, sample_offset + count):
                block.extend(
                    (sample % 256, 0, sample % 256, 0, sample % 256, 0, sample % 256, 0)
                )
            if self.corrupt_prefix and sample_offset == 0:
                block[0] ^= 0xFF
            write_ci16(bytes(block))
            remaining -= count
            sample_offset += count
        return SegmentManifest(
            request.segment_id,
            request,
            request.center_frequency_hz,
            request.sample_rate_hz,
            request.bandwidth_hz,
            request.gain,
            UtcNs(1_700_000_000_000_000_000),
            100,
            request.sample_count,
            (request.sample_count, 2, 2),
        )


def profiles() -> tuple[QualificationProfile, QualificationProfile]:
    gain = GainSetting(GainMode.MANUAL, 40.0)
    return (
        QualificationProfile("narrow", 1.5e9, 10.0, 10.0, 1.0, gain),
        QualificationProfile("wide", 1.5e9, 20.0, 20.0, 1.0, gain),
    )


def expected_prefix(samples: int = 2) -> bytes:
    result = bytearray()
    for sample in range(samples):
        result.extend(
            (sample % 256, 0, sample % 256, 0, sample % 256, 0, sample % 256, 0)
        )
    return bytes(result)


def test_profile_name_cannot_escape_output_directory() -> None:
    with pytest.raises(ValueError):
        QualificationProfile(
            "../outside", 1.5e9, 10.0, 10.0, 1.0, GainSetting(GainMode.AGC)
        )


def test_runner_emits_narrow_wide_throughput_alignment_backlog_and_interruption(
    tmp_path: Path,
) -> None:
    runner = CaptureQualificationRunner(
        QualificationRadio(),
        RECEIVERS,
        tmp_path,
        minimum_end_to_end_ratio=1e-9,
        minimum_disk_headroom_ratio=1e-9,
    )
    result = runner.run(
        profiles(), alignment_expected=expected_prefix(), interruption_bytes=4096
    )
    assert result.all_profiles_succeeded
    assert [item.profile for item in result.profiles] == ["narrow", "wide"]
    assert [item.byte_count for item in result.profiles] == [80, 160]
    assert all(
        item.alignment_checked and item.alignment_matches for item in result.profiles
    )
    assert all(item.end_to_end_bytes_s > 0 for item in result.profiles)
    assert all(item.disk_throughput_bytes_s > 0 for item in result.profiles)
    assert all(item.required_input_bytes_s > 0 for item in result.profiles)
    assert all(item.end_to_end_pass for item in result.profiles)
    assert all(item.disk_headroom_pass for item in result.profiles)
    assert all(item.estimated_max_disk_backlog_bytes >= 0 for item in result.profiles)
    assert result.interruption.success
    assert result.interruption.partial_identifiable
    assert result.interruption.final_absent
    assert result.interruption.durable_partial_bytes == 4096
    assert (tmp_path / "narrow.ci16").stat().st_size == 80
    assert (tmp_path / "wide.ci16").stat().st_size == 160
    assert (tmp_path / "interruption.ci16.partial").exists()
    assert not (tmp_path / "interruption.ci16").exists()


def test_alignment_mismatch_and_profile_failure_are_machine_readable(
    tmp_path: Path,
) -> None:
    result = CaptureQualificationRunner(
        QualificationRadio(corrupt_prefix=True, fail_wide=True), RECEIVERS, tmp_path
    ).run(profiles(), alignment_expected=expected_prefix(), interruption_bytes=64)
    assert not result.all_profiles_succeeded
    assert not result.profiles[0].success
    assert result.profiles[0].alignment_matches is False
    assert "alignment" in result.profiles[0].error
    assert not result.profiles[1].success
    assert result.profiles[1].alignment_matches is None
    assert "injected wide failure" in result.profiles[1].error


def test_cli_failure_is_exactly_one_json_document_without_loading_hardware(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[3]
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "leo_flow.capture.qualification",
            "--uri",
            "ip:test",
            "--serial",
            "serial",
            "--radio-id",
            "invalid",
            "--receiver-a",
            "rx_a",
            "--receiver-b",
            "rx_b",
            "--output-directory",
            str(tmp_path),
            "--center-frequency-hz",
            "1500000000",
        ],
        env={"PYTHONPATH": str(root / "src")},
        check=False,
        capture_output=True,
    )
    assert completed.returncode == 2
    documents = completed.stdout.splitlines()
    assert len(documents) == 1
    payload = json.loads(documents[0])
    assert payload["schema"] == "org.leo-flow.capture-qualification/v1"
    assert payload["error"]["type"] == "ValueError"
