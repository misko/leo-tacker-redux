"""Audit and plot the public v6 scan/capture evidence.

The script never opens a radio, database, CAS object, or campaign journal.  Its
only live input is the dashboard's public HTTP projection.  The immutable v6
definition and materialization are read as operator artifacts, then reduced to
the checked public-evidence JSON used by the plots.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from typing import Any

CAMPAIGN_ID = "qual_gauss_r20_r21_20260816_v6"
EXPECTED_DEFINITION_DIGEST = (
    "e2d0bd04a569d0d0aa47dbd4da1e6a3bbefe29e3d15507c415ddd204e7144b37"
)
EXPECTED_MATERIALIZATION_DIGEST = (
    "24a3bcaf456511c82304ffcbab450431e56dcdce544aa91fd9012376edf04030"
)
EXPECTED_TERMINAL_AUDIT_DIGEST = (
    "465a1a28ac221e8c802cca9eafb17f738df4ac65236078c2148b588b1d3b1ff7"
)
LNB_LO_HZ = 9_750_000_000.0
SUBCARRIER_SPACING_HZ = 234_375.0
PILOT_BANDWIDTH_HZ = 1_875_000.0
CHANNEL_SPACING_HZ = 250_000_000.0
EDGE_INDICES = {"upper": tuple(range(488, 496)), "lower": tuple(range(528, 536))}
EDGE_ORDERS = {
    "L": tuple(
        (channel, edge) for channel in range(1, 5) for edge in ("lower", "upper")
    ),
    "U": tuple(
        (channel, edge) for channel in range(1, 5) for edge in ("upper", "lower")
    ),
}
EXPECTED_RADIOS = {
    "a": {
        "radio_id": "radio_pluto_5d4d",
        "uri": "ip:192.168.1.20",
        "receiver_chain_ids": ["rx_lnb_a", "rx_lnb_b"],
    },
    "b": {
        "radio_id": "radio_pluto_19f2",
        "uri": "ip:192.168.1.21",
        "receiver_chain_ids": ["rx_lnb_c", "rx_lnb_d"],
    },
}
EXPECTED_CELLS = tuple(
    (rate, duration, rate * duration // 1000)
    for rate in (1_250_000, 2_500_000, 5_000_000)
    for duration in (40, 80, 160)
)


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def channel_center_hz(channel: int) -> float:
    return (
        10_700_000_000.0
        + SUBCARRIER_SPACING_HZ / 2
        + CHANNEL_SPACING_HZ * (channel - 0.5)
    )


def subcarrier_offset_hz(index: int) -> float:
    signed = index if index < 512 else index - 1024
    return signed * SUBCARRIER_SPACING_HZ


def edge_offset_hz(edge: str) -> float:
    return sum(subcarrier_offset_hz(index) for index in EDGE_INDICES[edge]) / 8


def edge_if_hz(channel: int, edge: str) -> float:
    return channel_center_hz(channel) - LNB_LO_HZ + edge_offset_hz(edge)


def _json(path: Path) -> Any:
    return json.loads(path.read_text())


def _get_json(url: str) -> tuple[int, Any]:
    request = urllib.request.Request(url, headers={"accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def audit_materialization(
    definition_path: Path, materialization_path: Path
) -> dict[str, Any]:
    raw = definition_path.read_bytes()
    assert sha256_bytes(raw) == EXPECTED_DEFINITION_DIGEST
    assert (
        sha256_bytes(materialization_path.read_bytes())
        == EXPECTED_MATERIALIZATION_DIGEST
    )
    definition = json.loads(raw)
    materialization = _json(materialization_path)
    assert definition["campaign_id"] == CAMPAIGN_ID
    assert (
        materialization["definition_digest"] == f"sha256:{EXPECTED_DEFINITION_DIGEST}"
    )
    assert definition["radios"] == [
        EXPECTED_RADIOS["a"]["radio_id"],
        EXPECTED_RADIOS["b"]["radio_id"],
    ]
    assert definition["maximum_observed_start_skew_ns"] == 100_000_000
    assert definition["unit_schedule"] == "fixed_nine_cell_no_catch_up_grid"
    assert definition["no_catch_up"] is True
    actual_cells = tuple(
        (cell["sample_rate_hz"], cell["duration_ms"], cell["sample_count"])
        for cell in definition["cells"]
    )
    assert actual_cells == EXPECTED_CELLS
    assert len(materialization["units"]) == 9

    units = []
    for index, unit in enumerate(materialization["units"]):
        stations: dict[str, Any] = {}
        for side in ("a", "b"):
            ref = unit[f"station_{side}"]
            station_path = Path(ref["path"])
            station_raw = station_path.read_bytes()
            assert sha256_bytes(station_raw) == ref["content_digest"].removeprefix(
                "sha256:"
            )
            station = json.loads(station_raw)
            expected = EXPECTED_RADIOS[side]
            assert all(station["radio"][key] == expected[key] for key in expected)
            assert station["radio"]["require_both_tx_muted"] is True
            assert station["plan"]["sample_rate_hz"] == unit["cell"]["sample_rate_hz"]
            assert station["plan"]["bandwidth_hz"] == unit["cell"]["sample_rate_hz"]
            assert station["plan"]["sample_count"] == unit["cell"]["sample_count"]
            if unit["cell"]["sample_rate_hz"] == 1_250_000:
                assert station["plan"]["allow_clipped_pilot"] is True
            else:
                assert "allow_clipped_pilot" not in station["plan"]
            stations[side] = {
                **expected,
                "edge_order": station["plan"]["edge_order"],
                "plan_id": station["plan"]["plan_id"],
                "station_specification_digest": ref["specification_digest"],
            }
        assert stations["a"]["edge_order"] == stations["b"]["edge_order"]
        assert stations["a"]["edge_order"] == ("L" if index % 2 == 0 else "U")
        units.append(
            {
                "unit_index": index,
                "requested_start_utc_ns": unit["requested_start_utc_ns"],
                **unit["cell"],
                "pilot_band_fits": unit["cell"]["sample_rate_hz"] >= PILOT_BANDWIDTH_HZ,
                "pilot_guard_hz": (unit["cell"]["sample_rate_hz"] - PILOT_BANDWIDTH_HZ)
                / 2,
                "edge_order": stations["a"]["edge_order"],
                "pair_geometry": "same-edge",
                "stations": stations,
            }
        )
    return {
        "campaign_id": CAMPAIGN_ID,
        "definition_digest": f"sha256:{EXPECTED_DEFINITION_DIGEST}",
        "materialization_digest": f"sha256:{EXPECTED_MATERIALIZATION_DIGEST}",
        "coordination_claim": "measured_software_coordination",
        "hardware_synchronization_claim": False,
        "maximum_observed_start_skew_ns": 100_000_000,
        "units": units,
    }


def audit_terminal_evidence(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    assert sha256_bytes(raw) == EXPECTED_TERMINAL_AUDIT_DIGEST
    audit = json.loads(raw)
    assert audit["campaign_id"] == CAMPAIGN_ID
    assert audit["definition_digest"] == f"sha256:{EXPECTED_DEFINITION_DIGEST}"
    assert (
        audit["materialization_file_sha256"]
        == f"sha256:{EXPECTED_MATERIALIZATION_DIGEST}"
    )
    assert audit["journal_revision"] == 43
    assert audit["qualification_receipt_eligible"] is False
    assert (
        audit["qualification_receipt_reason"]
        == "final_cell_observed_start_skew_exceeds_100ms"
    )
    assert audit["starlink_campaign_wired"] is False
    assert audit["successful_cell_count"] == 8
    assert audit["terminal_failed_cell_count"] == 1
    assert len(audit["cells"]) == 9
    final = audit["cells"][-1]
    assert final["slot_index"] == 8
    assert final["phase"] == "terminal_failed"
    assert final["paired_analysis_eligible"] is False
    assert final["skew_ns"] == 150_686_531
    assert final["analysis_invocations"] == 0
    assert all(recording["segment_count"] == 8 for recording in final["recordings"])
    assert all(
        recording["analysis_state"] == "pending" for recording in final["recordings"]
    )
    return {
        "digest": f"sha256:{EXPECTED_TERMINAL_AUDIT_DIGEST}",
        "journal_revision": audit["journal_revision"],
        "successful_cell_count": audit["successful_cell_count"],
        "terminal_failed_cell_count": audit["terminal_failed_cell_count"],
        "qualification_receipt_eligible": audit["qualification_receipt_eligible"],
        "qualification_receipt_reason": audit["qualification_receipt_reason"],
        "starlink_campaign_wired": audit["starlink_campaign_wired"],
        "final_cell": {
            "unit_index": final["slot_index"],
            "phase": final["phase"],
            "observed_start_skew_ns": final["skew_ns"],
            "paired_analysis_eligible": final["paired_analysis_eligible"],
            "analysis_invocations": final["analysis_invocations"],
            "recordings": [
                {
                    "radio_id": recording["radio_id"],
                    "recording_id": recording["recording_id"],
                    "analysis_state": recording["analysis_state"],
                    "waterfall_state": recording["waterfall"]["state"],
                }
                for recording in final["recordings"]
            ],
        },
    }


def validate_terminal_public_agreement(evidence: dict[str, Any]) -> None:
    final_public = next(
        batch for batch in evidence["public_batches"] if "_u008_" in batch["batch_id"]
    )
    final_audit = evidence["qualification_outcome"]["final_cell"]
    assert (
        final_public["observed_start_skew_ns"] == final_audit["observed_start_skew_ns"]
    )
    assert final_public["paired_analysis_eligibility"] == "ineligible"
    public_recordings = {
        (attempt["radio_id"], attempt["recording_id"], attempt["analysis_state"])
        for attempt in final_public["attempts"]
    }
    audit_recordings = {
        (recording["radio_id"], recording["recording_id"], recording["analysis_state"])
        for recording in final_audit["recordings"]
    }
    assert public_recordings == audit_recordings


def refresh_public_evidence(base_url: str, evidence: dict[str, Any]) -> dict[str, Any]:
    units = evidence["units"]
    start = units[0]["requested_start_utc_ns"] - 60_000_000_000
    stop = units[-1]["requested_start_utc_ns"] + 120_000_000_000
    status, payload = _get_json(
        f"{base_url}/api/v2/capture-batches?start_utc_ns={start}&stop_utc_ns={stop}"
    )
    assert status == 200
    matches = [item for item in payload["items"] if CAMPAIGN_ID in item["batch_id"]]
    by_unit: dict[int, Any] = {}
    for batch in matches:
        found = re.search(r"_u(\d{3})_", batch["batch_id"])
        assert found
        index = int(found.group(1))
        assert index not in by_unit
        assert batch["coordination_claim"] == "measured_software_coordination"
        assert batch["maximum_observed_start_skew_ns"] == 100_000_000
        by_unit[index] = batch
    public_batches = []
    for unit in units:
        batch = by_unit.get(unit["unit_index"])
        if batch is None:
            continue
        assert len(batch["attempts"]) == 2
        for attempt in batch["attempts"]:
            assert attempt["radio_id"] in {
                EXPECTED_RADIOS["a"]["radio_id"],
                EXPECTED_RADIOS["b"]["radio_id"],
            }
            assert attempt["requested_start_utc_ns"] == unit["requested_start_utc_ns"]
        public_batches.append(batch)
    evidence["public_batches"] = sorted(
        public_batches,
        key=lambda value: int(re.search(r"_u(\d{3})_", value["batch_id"])[1]),
    )
    evidence["terminal_batch_count"] = sum(
        all(
            attempt["capture_state"] in {"succeeded", "failed"}
            for attempt in batch["attempts"]
        )
        for batch in public_batches
    )
    evidence["successful_recording_count"] = sum(
        attempt["capture_state"] == "succeeded"
        for batch in public_batches
        for attempt in batch["attempts"]
    )
    return evidence


def _representative_recordings(evidence: dict[str, Any]) -> dict[str, str]:
    candidates = []
    units = {unit["unit_index"]: unit for unit in evidence["units"]}
    for batch in evidence.get("public_batches", []):
        index = int(re.search(r"_u(\d{3})_", batch["batch_id"])[1])
        unit = units[index]
        if not unit["pilot_band_fits"]:
            continue
        for attempt in batch["attempts"]:
            if (
                attempt["capture_state"] == "succeeded"
                and attempt["analysis_state"] == "complete"
            ):
                candidates.append(
                    (
                        unit["sample_rate_hz"],
                        unit["duration_ms"],
                        attempt["radio_id"],
                        attempt["recording_id"],
                    )
                )
    selected = {}
    for _, _, radio, recording in sorted(candidates, reverse=True):
        selected.setdefault(radio, recording)
    return selected


def refresh_waterfall_sources(
    base_url: str, evidence: dict[str, Any], assets: Path
) -> None:
    selected = _representative_recordings(evidence)
    labels = {
        EXPECTED_RADIOS["a"]["radio_id"]: "r20",
        EXPECTED_RADIOS["b"]["radio_id"]: "r21",
    }
    representatives = {}
    for radio_id, recording_id in selected.items():
        detail_status, detail = _get_json(
            f"{base_url}/api/v3/recordings/{recording_id}"
        )
        waterfall_status, waterfall = _get_json(
            f"{base_url}/api/v3/recordings/{recording_id}/waterfall"
        )
        starlink_status, _ = _get_json(
            f"{base_url}/api/v3/recordings/{recording_id}/starlink"
        )
        assert detail_status == waterfall_status == 200
        assert waterfall["state"] == "complete" and len(waterfall["tiles"]) == 16
        assert detail["radio_id"] == radio_id
        preferred = [
            tile
            for tile in waterfall["tiles"]
            if "ch4_lower" in tile["segment_id"]
            and tile["receiver_chain_id"]
            == EXPECTED_RADIOS["a" if radio_id.endswith("5d4d") else "b"][
                "receiver_chain_ids"
            ][0]
        ]
        assert len(preferred) == 1
        tile = preferred[0]
        compact = {
            "recording_id": recording_id,
            "radio_id": radio_id,
            "starlink_projection_state": "not_evaluated"
            if starlink_status == 404
            else "projected",
            "sample_rate_hz": next(
                segment["sample_rate_hz"]
                for segment in detail["segments"]
                if segment["segment_id"] == tile["segment_id"]
            ),
            "duration_ms": round(
                next(
                    segment["sample_count"] / segment["sample_rate_hz"] * 1000
                    for segment in detail["segments"]
                    if segment["segment_id"] == tile["segment_id"]
                )
            ),
            "tile": tile,
        }
        path = assets / f"waterfall-{labels[radio_id]}-source.json"
        path.write_bytes(canonical_bytes(compact))
        render_waterfall_png(compact, assets / f"waterfall-{labels[radio_id]}.png")
        representatives[radio_id] = {
            "recording_id": recording_id,
            "source": str(path.relative_to(assets.parent.parent)),
            "image": str(
                (assets / f"waterfall-{labels[radio_id]}.png").relative_to(
                    assets.parent.parent
                )
            ),
            "source_sha256": f"sha256:{sha256_bytes(path.read_bytes())}",
            "sample_rate_hz": compact["sample_rate_hz"],
            "duration_ms": compact["duration_ms"],
            "segment_id": tile["segment_id"],
            "receiver_chain_id": tile["receiver_chain_id"],
            "state": waterfall["state"],
            "starlink_projection_state": compact["starlink_projection_state"],
        }
    evidence["representative_waterfalls"] = representatives


def _svg(path: Path, width: int, height: int, body: list[str]) -> None:
    text = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#0b1220"/>',
        "<style>text{font-family:ui-monospace,monospace;fill:#dbeafe}.axis{stroke:#64748b}.muted{fill:#94a3b8}.small{font-size:12px}.label{font-size:14px}.title{font-size:19px;font-weight:700}</style>",
        *body,
        "</svg>",
    ]
    path.write_text("\n".join(text) + "\n")


def render_frequency_map(path: Path) -> None:
    width, height = 1200, 510
    left, right = 100, 1160
    lo, hi = 900.0, 2000.0
    x = lambda mhz: left + (mhz - lo) / (hi - lo) * (right - left)
    body = [
        '<text x="40" y="38" class="title">Low-band Starlink channel / edge-pilot IF map</text>',
        '<text x="40" y="61" class="muted small">9.75 GHz LNB LO · Qin 234.375 kHz spacing · eight pilots span 1.875 MHz</text>',
    ]
    for tick in range(900, 2001, 100):
        body += [
            f'<line x1="{x(tick):.2f}" y1="80" x2="{x(tick):.2f}" y2="445" stroke="#1e293b"/>',
            f'<text x="{x(tick):.2f}" y="468" text-anchor="middle" class="muted small">{tick}</text>',
        ]
    body.append(
        '<text x="630" y="495" text-anchor="middle" class="label">L-band IF (MHz)</text>'
    )
    colors = ("#22d3ee", "#818cf8", "#f472b6", "#f59e0b")
    for channel, color in zip(range(1, 5), colors, strict=True):
        y = 115 + (channel - 1) * 80
        center = (channel_center_hz(channel) - LNB_LO_HZ) / 1e6
        lower = edge_if_hz(channel, "lower") / 1e6
        upper = edge_if_hz(channel, "upper") / 1e6
        body += [
            f'<text x="40" y="{y + 5}" class="label">CH{channel}</text>',
            f'<line x1="{x(center - 120):.2f}" y1="{y}" x2="{x(center + 120):.2f}" y2="{y}" stroke="{color}" stroke-width="18" opacity=".20"/>',
            f'<line x1="{x(lower):.2f}" y1="{y - 19}" x2="{x(lower):.2f}" y2="{y + 19}" stroke="{color}" stroke-width="5"/>',
            f'<line x1="{x(upper):.2f}" y1="{y - 19}" x2="{x(upper):.2f}" y2="{y + 19}" stroke="{color}" stroke-width="5"/>',
            f'<circle cx="{x(center):.2f}" cy="{y}" r="5" fill="#e2e8f0"/>',
            f'<text x="{x(lower):.2f}" y="{y + 37}" text-anchor="middle" class="small">L {lower:.4f}</text>',
            f'<text x="{x(upper):.2f}" y="{y + 37}" text-anchor="middle" class="small">U {upper:.4f}</text>',
        ]
    body += [
        '<rect x="42" y="420" width="18" height="10" fill="#22d3ee" opacity=".5"/><text x="68" y="430" class="muted small">240 MHz channel</text>',
        '<line x1="230" y1="425" x2="250" y2="425" stroke="#e2e8f0" stroke-width="5"/><text x="260" y="430" class="muted small">edge-pilot center</text>',
        '<text x="455" y="430" class="muted small">1.25 MS/s: clipped · 2.5 / 5 MS/s: full pilot band fits</text>',
    ]
    _svg(path, width, height, body)


def render_cadence(path: Path, evidence: dict[str, Any]) -> None:
    width, height = 1200, 560
    left, right, top, bottom = 110, 1140, 85, 475
    units = evidence["units"]
    start = units[0]["requested_start_utc_ns"]
    period = (units[-1]["requested_start_utc_ns"] - start) / 1e9
    x = lambda seconds: left + seconds / period * (right - left)
    y = lambda skew_ms: bottom - skew_ms / 100.0 * (bottom - top)
    batches = {
        int(re.search(r"_u(\d{3})_", batch["batch_id"])[1]): batch
        for batch in evidence.get("public_batches", [])
    }
    body = [
        '<text x="40" y="36" class="title">v6 immutable cadence and measured first-sample skew</text>',
        '<text x="40" y="59" class="muted small">Markers are software-coordinated first-sample evidence; they do not imply a shared RF/sample clock.</text>',
        f'<line x1="{left}" y1="{y(100):.2f}" x2="{right}" y2="{y(100):.2f}" stroke="#ef4444" stroke-width="2"/>',
        f'<text x="{left - 10}" y="{y(100) + 4:.2f}" text-anchor="end" class="small">100 ms limit</text>',
        f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" class="axis"/>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" class="axis"/>',
    ]
    for tick in (0, 20, 40, 60, 80, 100):
        body += [
            f'<line x1="{left}" y1="{y(tick):.2f}" x2="{right}" y2="{y(tick):.2f}" stroke="#1e293b"/>',
            f'<text x="{left - 10}" y="{y(tick) + 4:.2f}" text-anchor="end" class="muted small">{tick}</text>',
        ]
    colors = {1_250_000: "#22d3ee", 2_500_000: "#a78bfa", 5_000_000: "#f59e0b"}
    points = []
    for unit in units:
        seconds = (unit["requested_start_utc_ns"] - start) / 1e9
        batch = batches.get(unit["unit_index"])
        if batch:
            skew_ms = batch["observed_start_skew_ns"] / 1e6
            points.append(skew_ms)
            fill = colors[unit["sample_rate_hz"]]
            body.append(
                f'<circle cx="{x(seconds):.2f}" cy="{y(skew_ms):.2f}" r="7" fill="{fill}"/>'
            )
            body.append(
                f'<text x="{x(seconds):.2f}" y="{y(skew_ms) - 12:.2f}" text-anchor="middle" class="small">{skew_ms:.2f}</text>'
            )
        else:
            body.append(
                f'<circle cx="{x(seconds):.2f}" cy="{bottom}" r="6" fill="none" stroke="#64748b"/>'
            )
        body.append(
            f'<text x="{x(seconds):.2f}" y="{bottom + 24}" text-anchor="middle" class="small">u{unit["unit_index"]} {unit["duration_ms"]}ms</text>'
        )
    body += [
        '<text x="25" y="280" transform="rotate(-90 25 280)" text-anchor="middle" class="label">Observed first-sample skew (ms)</text>',
        '<text x="625" y="535" text-anchor="middle" class="label">Scheduled offset from first release (seconds)</text>',
    ]
    for offset, (rate, color) in enumerate(colors.items()):
        body.append(
            f'<circle cx="{840 + offset * 110}" cy="55" r="6" fill="{color}"/><text x="{852 + offset * 110}" y="60" class="small">{rate / 1e6:g} MS/s</text>'
        )
    if points:
        body.append(
            f'<text x="40" y="520" class="muted small">Observed {len(points)}/9 · maximum {max(points):.3f} ms</text>'
        )
    _svg(path, width, height, body)


def _color(value: float) -> tuple[int, int, int]:
    anchors = (
        (11, 18, 32),
        (30, 64, 175),
        (12, 170, 183),
        (250, 204, 21),
        (239, 68, 68),
    )
    value = max(0.0, min(1.0, value)) * (len(anchors) - 1)
    index = min(int(value), len(anchors) - 2)
    fraction = value - index
    return tuple(
        round(anchors[index][i] * (1 - fraction) + anchors[index + 1][i] * fraction)
        for i in range(3)
    )


def _png(path: Path, width: int, height: int, rows: list[bytes]) -> None:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + row for row in rows)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def render_waterfall_png(compact: dict[str, Any], path: Path) -> None:
    tile = compact["tile"]
    power = tile["power_db"]
    source_h, source_w = len(power), len(power[0])
    assert source_h and source_w and all(len(row) == source_w for row in power)
    scale_x, scale_y, margin, bar = 5, 3, 24, 40
    width = margin * 2 + source_w * scale_x + bar
    height = margin * 2 + source_h * scale_y
    floor, ceiling = tile["floor_db"], tile["ceiling_db"]
    pixels = [[(11, 18, 32)] * width for _ in range(height)]
    for sy, row in enumerate(power):
        for sx, value in enumerate(row):
            color = _color((value - floor) / (ceiling - floor))
            for dy in range(scale_y):
                for dx in range(scale_x):
                    pixels[margin + sy * scale_y + dy][margin + sx * scale_x + dx] = (
                        color
                    )
    for py in range(margin, height - margin):
        color = _color(1 - (py - margin) / max(1, height - 2 * margin - 1))
        for px in range(width - margin - bar + 8, width - margin):
            pixels[py][px] = color
    rows = [bytes(component for pixel in row for component in pixel) for row in pixels]
    _png(path, width, height, rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    state = Path("/home/mouse9911/.local/state/leo-flow/campaigns") / CAMPAIGN_ID
    parser.add_argument(
        "--definition", type=Path, default=state / "qualification.definition.json"
    )
    parser.add_argument(
        "--materialization",
        type=Path,
        default=state / "materialization/qualification.materialization.json",
    )
    parser.add_argument(
        "--terminal-audit",
        type=Path,
        default=state / "evidence/live-qualification-terminal-audit.json",
    )
    parser.add_argument("--dashboard-url", default="http://127.0.0.1:8090")
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "assets")
    parser.add_argument("--refresh-api", action="store_true")
    parser.add_argument("--require-terminal", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    evidence = audit_materialization(args.definition, args.materialization)
    evidence["qualification_outcome"] = audit_terminal_evidence(args.terminal_audit)
    if args.refresh_api:
        evidence = refresh_public_evidence(args.dashboard_url.rstrip("/"), evidence)
        refresh_waterfall_sources(args.dashboard_url.rstrip("/"), evidence, args.output)
    elif (args.output / "v6-public-evidence.json").exists():
        prior = _json(args.output / "v6-public-evidence.json")
        evidence["public_batches"] = prior.get("public_batches", [])
        evidence["terminal_batch_count"] = prior.get("terminal_batch_count", 0)
        evidence["successful_recording_count"] = prior.get(
            "successful_recording_count", 0
        )
        evidence["representative_waterfalls"] = prior.get(
            "representative_waterfalls", {}
        )
    if args.require_terminal:
        assert evidence.get("terminal_batch_count") == 9
        assert evidence.get("successful_recording_count") == 18
        validate_terminal_public_agreement(evidence)
    render_frequency_map(args.output / "channel-frequency-map.svg")
    render_cadence(args.output / "v6-cadence-skew.svg", evidence)
    (args.output / "v6-public-evidence.json").write_bytes(canonical_bytes(evidence))
    print(
        json.dumps(
            {
                "campaign_id": CAMPAIGN_ID,
                "terminal_batches": evidence.get("terminal_batch_count", 0),
                "successful_recordings": evidence.get("successful_recording_count", 0),
                "output": str(args.output),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
