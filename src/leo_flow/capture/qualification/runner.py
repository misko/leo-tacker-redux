"""Measure paired capture throughput, disk backlog, alignment, and interruption."""

from __future__ import annotations

import hashlib
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from leo_flow.contracts.capture import GainSetting, SegmentRequest
from leo_flow.contracts.core import ReceiverChainId, SegmentId
from leo_flow.contracts.ports import RadioDevice

PAIRED_CI16_FRAME_BYTES = 8


@dataclass(frozen=True)
class QualificationProfile:
    name: str
    center_frequency_hz: float
    sample_rate_hz: float
    bandwidth_hz: float
    duration_s: float
    gain: GainSetting

    def __post_init__(self) -> None:
        if (
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", self.name) is None
            or min(
                self.center_frequency_hz,
                self.sample_rate_hz,
                self.bandwidth_hz,
                self.duration_s,
            )
            <= 0
        ):
            raise ValueError("qualification profile values must be positive")


@dataclass(frozen=True)
class ThroughputResult:
    profile: str
    success: bool
    error: str | None
    expected_samples: int
    captured_samples: int
    byte_count: int
    elapsed_s: float
    end_to_end_bytes_s: float
    required_input_bytes_s: float
    end_to_end_ratio: float
    disk_throughput_bytes_s: float
    disk_headroom_ratio: float
    end_to_end_pass: bool
    disk_headroom_pass: bool
    max_disk_operation_s: float
    estimated_max_disk_backlog_bytes: int
    drop_telemetry_available: bool | None
    alignment_checked: bool
    alignment_matches: bool | None
    alignment_expected_sha256: str | None
    alignment_observed_sha256: str | None


@dataclass(frozen=True)
class InterruptionResult:
    success: bool
    mode: str
    partial_identifiable: bool
    final_absent: bool
    durable_partial_bytes: int
    elapsed_s: float
    error: str | None = None


@dataclass(frozen=True)
class QualificationResult:
    schema: str
    radio_id: str
    receiver_chain_ids: tuple[str, str]
    profiles: tuple[ThroughputResult, ...]
    interruption: InterruptionResult
    all_profiles_succeeded: bool


class CaptureQualificationRunner:
    def __init__(
        self,
        radio: RadioDevice,
        receiver_chain_ids: tuple[ReceiverChainId, ReceiverChainId],
        output_directory: Path,
        *,
        perf_counter: Callable[[], float] = time.perf_counter,
        minimum_end_to_end_ratio: float = 0.9,
        minimum_disk_headroom_ratio: float = 2.0,
    ) -> None:
        if minimum_end_to_end_ratio <= 0 or minimum_disk_headroom_ratio <= 0:
            raise ValueError("qualification ratios must be positive")
        self._radio = radio
        self._receiver_chain_ids = receiver_chain_ids
        self._output_directory = Path(output_directory)
        self._perf_counter = perf_counter
        self._minimum_end_to_end_ratio = minimum_end_to_end_ratio
        self._minimum_disk_headroom_ratio = minimum_disk_headroom_ratio

    def run(
        self,
        profiles: tuple[QualificationProfile, ...],
        *,
        alignment_expected: bytes | None = None,
        interruption_bytes: int = 8 * 1024 * 1024,
    ) -> QualificationResult:
        if not profiles:
            raise ValueError("at least one qualification profile is required")
        if interruption_bytes <= 0:
            raise ValueError("interruption_bytes must be positive")
        self._output_directory.mkdir(parents=True, exist_ok=True)
        results = tuple(
            self._run_profile(profile, alignment_expected) for profile in profiles
        )
        interruption = self.measure_interruption(interruption_bytes)
        return QualificationResult(
            schema="org.leo-flow.capture-qualification/v1",
            radio_id=str(self._radio.radio_id),
            receiver_chain_ids=(
                str(self._receiver_chain_ids[0]),
                str(self._receiver_chain_ids[1]),
            ),
            profiles=results,
            interruption=interruption,
            all_profiles_succeeded=all(result.success for result in results),
        )

    def _run_profile(
        self, profile: QualificationProfile, alignment_expected: bytes | None
    ) -> ThroughputResult:
        samples = round(profile.duration_s * profile.sample_rate_hz)
        required_rate = profile.sample_rate_hz * PAIRED_CI16_FRAME_BYTES
        partial = self._output_directory / f"{profile.name}.ci16.partial"
        final = self._output_directory / f"{profile.name}.ci16"
        for path in (partial, final):
            if path.exists():
                raise FileExistsError(f"qualification output already exists: {path}")
        byte_count = 0
        max_operation = 0.0
        total_disk_operation = 0.0
        backlog = 0.0
        max_backlog = 0.0
        alignment_limit = (
            len(alignment_expected) if alignment_expected is not None else 0
        )
        observed_prefix = bytearray()
        manifest_diagnostics: dict[str, object] | None = None
        started = self._perf_counter()
        captured_samples = 0
        try:
            with partial.open("xb", buffering=0) as output:

                def write_block(data: bytes) -> None:
                    nonlocal byte_count, max_operation, total_disk_operation
                    nonlocal backlog, max_backlog
                    operation_started = self._perf_counter()
                    output.write(data)
                    operation_s = self._perf_counter() - operation_started
                    max_operation = max(max_operation, operation_s)
                    total_disk_operation += operation_s
                    backlog = max(
                        0.0, backlog + required_rate * operation_s - len(data)
                    )
                    max_backlog = max(max_backlog, backlog)
                    byte_count += len(data)
                    if len(observed_prefix) < alignment_limit:
                        remaining = alignment_limit - len(observed_prefix)
                        observed_prefix.extend(data[:remaining])

                request = SegmentRequest(
                    segment_id=SegmentId(f"seg_qualification-{profile.name}"),
                    center_frequency_hz=profile.center_frequency_hz,
                    sample_rate_hz=profile.sample_rate_hz,
                    bandwidth_hz=profile.bandwidth_hz,
                    receiver_chain_ids=self._receiver_chain_ids,
                    gain=profile.gain,
                    sample_count=samples,
                )
                manifest = self._radio.acquire_segment(request, write_block)
                captured_samples = manifest.sample_count
                manifest_diagnostics = dict(manifest.diagnostics)
                sync_started = self._perf_counter()
                os.fsync(output.fileno())
                sync_s = self._perf_counter() - sync_started
                max_operation = max(max_operation, sync_s)
                total_disk_operation += sync_s
                backlog = max(0.0, backlog + required_rate * sync_s)
                max_backlog = max(max_backlog, backlog)
            os.replace(partial, final)
            error = None
            success = (
                captured_samples == samples
                and byte_count == samples * PAIRED_CI16_FRAME_BYTES
            )
            if not success:
                error = "manifest or byte count differs from requested profile"
        except Exception as failure:  # noqa: BLE001 - qualification records failures
            error = f"{type(failure).__name__}: {failure}"
            success = False
        elapsed = self._perf_counter() - started
        expected_digest = (
            hashlib.sha256(alignment_expected).hexdigest()
            if alignment_expected is not None
            else None
        )
        observed_digest = (
            hashlib.sha256(observed_prefix).hexdigest()
            if alignment_expected is not None
            else None
        )
        alignment_matches = None
        if alignment_expected is not None and len(observed_prefix) == len(
            alignment_expected
        ):
            alignment_matches = bytes(observed_prefix) == alignment_expected
        if alignment_matches is False:
            success = False
            if error is None:
                error = "alignment input does not match captured receiver order"
        end_to_end_rate = byte_count / elapsed if elapsed > 0 else 0.0
        disk_rate = (
            byte_count / total_disk_operation if total_disk_operation > 0 else 0.0
        )
        end_to_end_ratio = end_to_end_rate / required_rate
        disk_headroom_ratio = disk_rate / required_rate
        end_to_end_pass = end_to_end_ratio >= self._minimum_end_to_end_ratio
        disk_headroom_pass = disk_headroom_ratio >= self._minimum_disk_headroom_ratio
        if success and not end_to_end_pass:
            success = False
            error = "end-to-end capture did not sustain the input rate"
        if success and not disk_headroom_pass:
            success = False
            error = "disk write headroom is below the qualification minimum"
        return ThroughputResult(
            profile=profile.name,
            success=success,
            error=error,
            expected_samples=samples,
            captured_samples=captured_samples,
            byte_count=byte_count,
            elapsed_s=elapsed,
            end_to_end_bytes_s=end_to_end_rate,
            required_input_bytes_s=required_rate,
            end_to_end_ratio=end_to_end_ratio,
            disk_throughput_bytes_s=disk_rate,
            disk_headroom_ratio=disk_headroom_ratio,
            end_to_end_pass=end_to_end_pass,
            disk_headroom_pass=disk_headroom_pass,
            max_disk_operation_s=max_operation,
            estimated_max_disk_backlog_bytes=round(max_backlog),
            drop_telemetry_available=(
                bool(manifest_diagnostics.get("drop_telemetry_available"))
                if manifest_diagnostics is not None
                else None
            ),
            alignment_checked=alignment_expected is not None,
            alignment_matches=alignment_matches,
            alignment_expected_sha256=expected_digest,
            alignment_observed_sha256=observed_digest,
        )

    def measure_interruption(self, byte_count: int) -> InterruptionResult:
        """Measure exception interruption; SIGKILL remains an on-Pi gate."""
        partial = self._output_directory / "interruption.ci16.partial"
        final = self._output_directory / "interruption.ci16"
        for path in (partial, final):
            if path.exists():
                raise FileExistsError(f"qualification output already exists: {path}")
        started = self._perf_counter()
        try:
            with partial.open("xb", buffering=0) as output:
                remaining = byte_count
                block = bytes(min(1024 * 1024, byte_count))
                while remaining:
                    data = block[:remaining]
                    output.write(data)
                    remaining -= len(data)
                os.fsync(output.fileno())
                raise _IntentionalInterruption
        except _IntentionalInterruption:
            pass
        except Exception as error:  # noqa: BLE001 - qualification result boundary
            return InterruptionResult(
                False,
                "simulated_exception",
                partial.exists(),
                not final.exists(),
                partial.stat().st_size if partial.exists() else 0,
                self._perf_counter() - started,
                f"{type(error).__name__}: {error}",
            )
        durable_bytes = partial.stat().st_size if partial.exists() else 0
        return InterruptionResult(
            success=(
                partial.name.endswith(".partial")
                and durable_bytes == byte_count
                and not final.exists()
            ),
            mode="simulated_exception",
            partial_identifiable=partial.name.endswith(".partial") and partial.exists(),
            final_absent=not final.exists(),
            durable_partial_bytes=durable_bytes,
            elapsed_s=self._perf_counter() - started,
        )


class _IntentionalInterruption(Exception):
    pass
