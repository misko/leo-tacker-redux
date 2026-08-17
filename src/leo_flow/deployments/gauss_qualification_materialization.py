"""Offline validator for the checked Gauss qualification materialization.

This boundary reads immutable JSON only.  It does not acquire pipeline locks,
open state databases, construct radio adapters, or emit qualification receipts.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from leo_flow.capture.campaign import (
    CAMPAIGN_CELLS,
    MAXIMUM_OBSERVED_START_SKEW_NS,
    PREFLIGHT_LEAD_NS,
    QUALIFICATION_SLOT_PERIOD_DENOMINATOR,
    QUALIFICATION_SLOT_PERIOD_NUMERATOR_NS,
    build_campaign_unit,
    materialize_campaign_station,
)
from leo_flow.capture.campaign_codec import decode_campaign_definition
from leo_flow.capture.drivers.v5_preflight import (
    TX1_DDS_CHANNEL_IDS,
    TX2_DDS_CHANNEL_IDS,
    ExpectedV5Radio,
    StandardLibiioTransport,
)
from leo_flow.capture.v5_station import (
    V5CaptureStation,
    load_v5_capture_station,
    require_disjoint_station_pair,
)
from leo_flow.contracts.core import Digest, UtcNs, canonical_json_bytes

MATERIALIZATION_SCHEMA = "org.leo-flow.gauss-v5-qualification-materialization/v2"
MAXIMUM_MATERIALIZATION_BYTES = 1_048_576
EXPECTED_RUNTIME_ID = "gauss-pluto-v5-rx-integrity-close-barrier-1"
EXPECTED_RUNTIME_MANIFEST_DIGEST = (
    "sha256:0a9cf278bf836655afbf7a9a324a21c5dc41235d1b251386a0013eb0f299f123"
)
EXPECTED_FIRMWARE = (
    "v0.38-plutoplus-spf-libiio-metadata-v5",
    "d7c87a9a28094ee6f0b23cb47df9ff737b5a69d8",
)


@dataclass(frozen=True, slots=True)
class QualificationProfile:
    campaign_id: str
    radios: Mapping[str, tuple[str, str, str, tuple[str, str]]]


QUALIFICATION_PROFILES = {
    "qual_gauss_r20_r21_20260816_v5": QualificationProfile(
        "qual_gauss_r20_r21_20260816_v5",
        {
            "a": (
                "ip:192.168.1.20",
                "1040005e0b100007100010000bf33a5d4d",
                "radio_pluto_5d4d",
                ("rx_lnb_a", "rx_lnb_b"),
            ),
            "b": (
                "ip:192.168.1.21",
                "10400056f695001322002d0010ad1719f2",
                "radio_pluto_19f2",
                ("rx_lnb_c", "rx_lnb_d"),
            ),
        },
    ),
}


@dataclass(frozen=True, slots=True)
class QualificationMaterializationSummary:
    campaign_id: str
    definition_digest: Digest
    manifest_digest: Digest
    station_count: int
    qualification_receipt_output: Path

    def document(self) -> dict[str, object]:
        return {
            "event": "qualification_materialization_valid",
            "campaign_id": self.campaign_id,
            "definition_digest": str(self.definition_digest),
            "manifest_digest": str(self.manifest_digest),
            "unit_count": len(CAMPAIGN_CELLS),
            "station_count": self.station_count,
            "qualification_status": "not_asserted",
            "qualification_receipt_output": str(self.qualification_receipt_output),
        }


def validate_qualification_materialization(
    manifest_path: Path,
) -> QualificationMaterializationSummary:
    """Prove the exact checked inventory using filesystem reads only."""

    payload = manifest_path.read_bytes()
    if len(payload) > MAXIMUM_MATERIALIZATION_BYTES:
        raise ValueError("qualification materialization exceeds the size limit")
    value = json.loads(payload)
    root = _mapping(value, "materialization")
    _exact_keys(
        root,
        {
            "schema",
            "definition",
            "definition_digest",
            "source_stations",
            "campaign_state_root",
            "qualification_receipt_output",
            "preflight_policy",
            "units",
        },
        "materialization",
    )
    if root["schema"] != MATERIALIZATION_SCHEMA:
        raise ValueError("qualification materialization schema differs")
    if canonical_json_bytes(root) + b"\n" != payload:
        raise ValueError("qualification materialization is not canonical")

    definition_path = _absolute_path(root["definition"], "definition")
    definition = decode_campaign_definition(definition_path.read_bytes().rstrip(b"\n"))
    try:
        profile = QUALIFICATION_PROFILES[definition.campaign_id]
    except KeyError as error:
        raise ValueError("qualification campaign is not a checked profile") from error
    if (
        not definition.qualification
        or not definition.analysis_after_each_capture
        or definition.qualification_receipt_digest is not None
        or definition.maximum_observed_start_skew_ns != MAXIMUM_OBSERVED_START_SKEW_NS
        or definition.preflight_lead_ns != PREFLIGHT_LEAD_NS
    ):
        raise ValueError("qualification definition policy differs")
    if root["definition_digest"] != str(definition.digest):
        raise ValueError("qualification definition digest differs")

    sources = _mapping(root["source_stations"], "source_stations")
    _exact_keys(sources, {"a", "b"}, "source_stations")
    stations = {
        side: _load_source_station(side, sources[side], profile) for side in ("a", "b")
    }
    require_disjoint_station_pair(stations["a"], stations["b"])
    if (
        definition.radio_a_id != stations["a"].radio.radio_id
        or definition.radio_b_id != stations["b"].radio.radio_id
        or definition.station_a_digest != stations["a"].specification_digest
        or definition.station_b_digest != stations["b"].specification_digest
    ):
        raise ValueError("qualification definition source binding differs")
    _validate_preflight_policy(root["preflight_policy"], stations)

    state_root = _absolute_path(root["campaign_state_root"], "campaign_state_root")
    receipt_output = _absolute_path(
        root["qualification_receipt_output"], "qualification_receipt_output"
    )
    units = root["units"]
    if not isinstance(units, list) or len(units) != len(CAMPAIGN_CELLS):
        raise ValueError("qualification materialization must contain all nine cells")
    plan_ids: set[object] = set()
    activity_ids: set[object] = set()
    specification_digests: set[Digest] = set()
    state_roots: set[Path] = set()
    recording_roots: set[Path] = set()
    spool_databases: set[Path] = set()
    radio_locks: set[Path] = set()
    for index, value in enumerate(units):
        entry = _mapping(value, f"unit {index}")
        _exact_keys(
            entry,
            {
                "success_index",
                "slot_index",
                "retry_index",
                "requested_start_utc_ns",
                "cell",
                "station_a",
                "station_b",
            },
            f"unit {index}",
        )
        if (
            entry["success_index"] != index
            or entry["slot_index"] != index
            or entry["retry_index"] != 0
            or entry["requested_start_utc_ns"]
            != int(definition.start_utc_ns)
            + index
            * QUALIFICATION_SLOT_PERIOD_NUMERATOR_NS
            // QUALIFICATION_SLOT_PERIOD_DENOMINATOR
            or entry["cell"] != CAMPAIGN_CELLS[index].document()
        ):
            raise ValueError("qualification unit schedule differs")
        unit = build_campaign_unit(
            definition,
            success_index=index,
            slot_index=index,
            retry_index=0,
            requested_start_utc_ns=UtcNs(
                int(definition.start_utc_ns)
                + index
                * QUALIFICATION_SLOT_PERIOD_NUMERATOR_NS
                // QUALIFICATION_SLOT_PERIOD_DENOMINATOR
            ),
        )
        materialized: dict[str, V5CaptureStation] = {}
        for side in ("a", "b"):
            expected = materialize_campaign_station(
                definition,
                stations[side],
                unit,
                side=side,
                campaign_state_root=state_root,
            )
            actual = _load_materialized_station(entry[f"station_{side}"])
            if actual.document() != expected.document():
                raise ValueError("materialized station differs from campaign policy")
            materialized[side] = actual
            plan_ids.add(actual.plan.plan_id)
            activity_ids.add(actual.capture_plan().activities[0].activity_id)
            specification_digests.add(actual.specification_digest)
            state_roots.add(actual.state.state_root)
            recording_roots.add(actual.state.recording_root)
            spool_databases.add(actual.state.spool_database)
            radio_locks.add(actual.state.lock_path)
            if (
                actual.state.cas_root != stations[side].state.cas_root
                or actual.state.mode_lock_path != stations[side].state.mode_lock_path
            ):
                raise ValueError("materialized station shared storage policy differs")
        require_disjoint_station_pair(materialized["a"], materialized["b"])
    expected_station_count = len(CAMPAIGN_CELLS) * 2
    unique_station_fields = (
        plan_ids,
        activity_ids,
        specification_digests,
        state_roots,
        recording_roots,
        spool_databases,
        radio_locks,
    )
    if any(len(values) != expected_station_count for values in unique_station_fields):
        raise ValueError("materialized station identities or state roots are reused")
    return QualificationMaterializationSummary(
        profile.campaign_id,
        definition.digest,
        Digest.sha256(payload),
        expected_station_count,
        receipt_output,
    )


def _load_source_station(
    side: str, value: object, profile: QualificationProfile
) -> V5CaptureStation:
    entry = _mapping(value, f"source station {side}")
    _exact_keys(entry, {"path", "specification_digest"}, f"source station {side}")
    station = load_v5_capture_station(
        _absolute_path(entry["path"], f"source station {side} path")
    )
    expected_uri, expected_serial, expected_radio_id, expected_receivers = (
        profile.radios[side]
    )
    if (
        entry["specification_digest"] != str(station.specification_digest)
        or (
            station.radio.uri,
            station.radio.expected_serial,
            str(station.radio.radio_id),
        )
        != (expected_uri, expected_serial, expected_radio_id)
        or tuple(map(str, station.radio.receiver_chain_ids)) != expected_receivers
        or not station.radio.require_both_tx_muted
        or station.expected_runtime.runtime_id != EXPECTED_RUNTIME_ID
        or str(station.runtime_manifest_digest) != EXPECTED_RUNTIME_MANIFEST_DIGEST
        or (
            station.radio.firmware_release,
            station.radio.firmware_commit,
        )
        != EXPECTED_FIRMWARE
        or Digest.sha256(station.runtime_manifest.read_bytes())
        != station.runtime_manifest_digest
    ):
        raise ValueError(f"source station {side} identity differs")
    return station


def _load_materialized_station(value: object) -> V5CaptureStation:
    entry = _mapping(value, "materialized station")
    _exact_keys(
        entry,
        {"path", "content_digest", "specification_digest"},
        "materialized station",
    )
    path = _absolute_path(entry["path"], "materialized station path")
    payload = path.read_bytes()
    if entry["content_digest"] != str(Digest.sha256(payload)):
        raise ValueError("materialized station content digest differs")
    station = load_v5_capture_station(path)
    if entry["specification_digest"] != str(station.specification_digest):
        raise ValueError("materialized station specification digest differs")
    return station


def _validate_preflight_policy(
    value: object, stations: Mapping[str, V5CaptureStation]
) -> None:
    policy = _mapping(value, "preflight_policy")
    expected_radio = ExpectedV5Radio(
        serial=stations["a"].radio.expected_serial,
        firmware_release=stations["a"].radio.firmware_release,
        firmware_commit=stations["a"].radio.firmware_commit,
        require_both_tx_muted=True,
    )
    expected: dict[str, object] = {
        "transport": StandardLibiioTransport.IP.value,
        "metadata_capability": expected_radio.metadata_capability,
        "enabled_scan_mask": expected_radio.enabled_scan_mask,
        "channel_count": expected_radio.channel_count,
        "component_layout": list(expected_radio.component_layout),
        "maximum_tx2_hardware_gain_db": expected_radio.maximum_tx2_hardware_gain_db,
        "tx2_dds_channel_ids": list(TX2_DDS_CHANNEL_IDS),
        "maximum_tx1_hardware_gain_db": expected_radio.maximum_tx1_hardware_gain_db,
        "tx1_dds_channel_ids": list(TX1_DDS_CHANNEL_IDS),
        "required_dds_scale": 0.0,
        "preflight_lead_ns": PREFLIGHT_LEAD_NS,
        "maximum_observed_start_skew_ns": MAXIMUM_OBSERVED_START_SKEW_NS,
    }
    if policy != expected:
        raise ValueError("qualification preflight/TX-mute policy differs")


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise TypeError(f"{field} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{field} fields differ")


def _absolute_path(value: object, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise TypeError(f"{field} must be a non-empty string")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field} must be absolute and normalized")
    return path


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m leo_flow.deployments.gauss_qualification_materialization"
    )
    parser.add_argument("validate", choices=("validate",))
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        summary = validate_qualification_materialization(args.manifest)
    except Exception:  # noqa: BLE001 - sanitized offline operator boundary
        stderr.write('{"event":"qualification_materialization_error"}\n')
        stderr.flush()
        return 2
    stdout.write(json.dumps(summary.document(), sort_keys=True, separators=(",", ":")))
    stdout.write("\n")
    stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
