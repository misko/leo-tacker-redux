"""Bounded synchronized one-tuning capture with immediate exact analysis."""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from leo_flow.adapters.focused_analysis_postgres import (
    PostgresRegisteredAnalysisSafetyGateV3,
)
from leo_flow.capture.campaign import CampaignUnit
from leo_flow.capture.focused_monitor import materialize_focused_monitor_station
from leo_flow.capture.v5_station import load_v5_capture_station
from leo_flow.contracts.capture_batch import (
    CaptureAttemptState,
    CaptureBatchDefinition,
    CaptureBatchMode,
    ExpectedCaptureAttempt,
)
from leo_flow.contracts.core import (
    CaptureAttemptId,
    CaptureBatchId,
    Digest,
    PlanId,
    SchemaRef,
    UtcNs,
    canonical_digest,
)
from leo_flow.deployments.gauss_campaign_runtime import (
    LinuxExternalRadioOwnershipGate,
    ProcessIsolatedCampaignCapture,
)
from leo_flow.deployments.gauss_focused_analysis_runtime import analyze_focused_pair
from leo_flow.services.config import AnalysisServiceConfig, load_service_config

SAMPLE_RATE_HZ = 2_500_000
BANDWIDTH_HZ = 2_500_000
DURATION_NS = 20_000_000_000
CHANNEL = 4
EDGE = "lower"
MAXIMUM_SKEW_NS = 100_000_000
MAXIMUM_LATENESS_NS = 5_000_000_000
MINIMUM_LEAD_NS = 30_000_000_000
ANALYSIS_DEADLINE_NS = 3_600_000_000_000
SCHEMA = "org.leo-flow.gauss-focused-capture/v1"


@dataclass(frozen=True, slots=True)
class _Timing:
    maximum_start_lateness_ns: int = MAXIMUM_LATENESS_NS


@dataclass(frozen=True, slots=True)
class FocusedCaptureDefinition:
    monitor_id: str
    requested_start_utc_ns: UtcNs
    station_a_digest: Digest
    station_b_digest: Digest

    def document(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "monitor_id": self.monitor_id,
            "requested_start_utc_ns": int(self.requested_start_utc_ns),
            "station_digests": [
                str(self.station_a_digest),
                str(self.station_b_digest),
            ],
            "channel": CHANNEL,
            "edge": EDGE,
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "bandwidth_hz": BANDWIDTH_HZ,
            "duration_ns": DURATION_NS,
            "hardware_block_duration_ns": 40_000_000,
            "maximum_observed_start_skew_ns": MAXIMUM_SKEW_NS,
            "maximum_start_lateness_ns": MAXIMUM_LATENESS_NS,
            "analysis_submission": "immediate_after_terminal_pair",
        }

    @property
    def digest(self) -> Digest:
        return canonical_digest(self.document())


@dataclass(frozen=True, slots=True)
class _FocusedUnit:
    success_index: int
    slot_index: int
    retry_index: int
    requested_start_utc_ns: UtcNs
    batch: CaptureBatchDefinition
    plan_a_id: PlanId
    plan_b_id: PlanId

    @property
    def unit_id(self) -> str:
        return str(self.batch.batch_id)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="leo-gauss-focused-capture",
        description="Capture one synchronized 20-second CH4-lower pair and analyze it.",
    )
    parser.add_argument("--monitor-id", required=True)
    parser.add_argument("--requested-start-utc-ns", type=int, required=True)
    parser.add_argument("--station-a", type=Path, required=True)
    parser.add_argument("--station-b", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--capture-credential-directory", type=Path, required=True)
    parser.add_argument("--analysis-config", type=Path, required=True)
    parser.add_argument("--analysis-credential-directory", type=Path, required=True)
    parser.add_argument("--dashboard-credential-directory", type=Path, required=True)
    parser.add_argument("--confirm-definition-digest", required=True)
    parser.add_argument("--arm", action="store_true")
    parser.add_argument("--capture-only", action="store_true")
    return parser


def _radio_address(uri: str) -> str:
    prefix, separator, address = uri.partition(":")
    if prefix != "ip" or not separator:
        raise ValueError("focused Gauss capture requires exact IP radios")
    return address


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    first = load_v5_capture_station(args.station_a)
    second = load_v5_capture_station(args.station_b)
    definition = FocusedCaptureDefinition(
        args.monitor_id,
        UtcNs(args.requested_start_utc_ns),
        first.specification_digest,
        second.specification_digest,
    )
    print(
        json.dumps(
            {
                "event": "focused_configuration",
                "definition": definition.document(),
                "definition_digest": str(definition.digest),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    if (
        not args.arm
        or args.confirm_definition_digest != str(definition.digest)
        or not args.state_root.is_absolute()
        or ".." in args.state_root.parts
        or args.state_root.exists()
        or int(definition.requested_start_utc_ns) - time.time_ns() < MINIMUM_LEAD_NS
    ):
        return 3
    os.mkdir(args.state_root, mode=0o700)
    unit_root = args.state_root / "unit-000"
    plan_a_id = PlanId(f"plan_{args.monitor_id}_a")
    plan_b_id = PlanId(f"plan_{args.monitor_id}_b")
    station_a = materialize_focused_monitor_station(
        first,
        plan_id=plan_a_id,
        state_root=unit_root / "radio-a",
        arm_name=f"{args.monitor_id}-ch4-lower-20s",
    )
    station_b = materialize_focused_monitor_station(
        second,
        plan_id=plan_b_id,
        state_root=unit_root / "radio-b",
        arm_name=f"{args.monitor_id}-ch4-lower-20s",
    )
    batch = CaptureBatchDefinition(
        SchemaRef(CaptureBatchDefinition.SCHEMA_ID),
        CaptureBatchId(f"cbatch_{args.monitor_id}_u000"),
        CaptureBatchMode.COORDINATED,
        (
            ExpectedCaptureAttempt(
                CaptureAttemptId(f"cattempt_{args.monitor_id}_u000_a"),
                station_a.radio.radio_id,
                plan_a_id,
                definition.requested_start_utc_ns,
            ),
            ExpectedCaptureAttempt(
                CaptureAttemptId(f"cattempt_{args.monitor_id}_u000_b"),
                station_b.radio.radio_id,
                plan_b_id,
                definition.requested_start_utc_ns,
            ),
        ),
        MAXIMUM_SKEW_NS,
    )
    unit = _FocusedUnit(
        0,
        0,
        0,
        definition.requested_start_utc_ns,
        batch,
        plan_a_id,
        plan_b_id,
    )
    stations = {"a": station_a, "b": station_b}
    capture = ProcessIsolatedCampaignCapture(
        _Timing(),
        first,
        second,
        args.state_root,
        args.state_root / "capture-batches.sqlite3",
        args.capture_credential_directory,
        LinuxExternalRadioOwnershipGate(
            (_radio_address(first.radio.uri), _radio_address(second.radio.uri))
        ),
        station_materializer=lambda _unit, side: stations[side],
        admission_builder=lambda dsn: PostgresRegisteredAnalysisSafetyGateV3(
            dsn, definition.digest
        ),
    )
    snapshot = capture.capture(
        cast(CampaignUnit, unit),
        not_before_utc_ns=definition.requested_start_utc_ns,
        deadline_utc_ns=UtcNs(int(definition.requested_start_utc_ns) + 120_000_000_000),
    )
    if any(
        item.state is not CaptureAttemptState.SUCCEEDED for item in snapshot.outcomes
    ):
        print(json.dumps({"event": "focused_capture_failed"}), flush=True)
        return 4
    print(
        json.dumps(
            {
                "event": "focused_capture_complete",
                "batch_id": str(snapshot.batch_id),
                "recording_ids": [
                    str(item.recording_id) for item in snapshot.successful_recordings
                ],
            },
            sort_keys=True,
        ),
        flush=True,
    )
    if args.capture_only:
        return 0
    config = load_service_config(args.analysis_config)
    if not isinstance(config, AnalysisServiceConfig):
        raise TypeError("focused analysis configuration is not an analysis service")
    receipt = analyze_focused_pair(
        snapshot,
        config,
        args.analysis_credential_directory,
        args.dashboard_credential_directory,
        deadline_utc_ns=UtcNs(time.time_ns() + ANALYSIS_DEADLINE_NS),
        capture_definition_digest=definition.digest,
    )
    print(
        json.dumps(
            {
                "event": "focused_analysis_complete",
                "batch_id": str(receipt.batch_id),
                "recording_ids": [str(item.recording_id) for item in receipt.successes],
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
