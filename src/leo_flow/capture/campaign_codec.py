"""Strict canonical JSON codec for private Gauss campaign documents."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from leo_flow.contracts.core import (
    Digest,
    DigestAlgorithm,
    RadioId,
    UtcNs,
    canonical_json_bytes,
)

from .campaign import (
    CAMPAIGN_CELLS,
    CAMPAIGN_ROUNDS,
    CAMPAIGN_SCHEMA,
    CAMPAIGN_WINDOW_NS,
    MAIN_GEOMETRY_SCHEDULE,
    MAIN_SCHEDULE_POLICY,
    QUALIFICATION_CAMPAIGN_SCHEMA,
    QUALIFICATION_SCHEDULE_POLICY,
    QUALIFICATION_SLOT_PERIOD_DENOMINATOR,
    QUALIFICATION_SLOT_PERIOD_NUMERATOR_NS,
    SLOT_PERIOD_DENOMINATOR,
    SLOT_PERIOD_NUMERATOR_NS,
    SLOTS_PER_ROUND,
    CampaignDefinition,
    CampaignQualificationReceipt,
)

MAX_CAMPAIGN_DEFINITION_BYTES = 65_536
MAX_QUALIFICATION_RECEIPT_BYTES = 65_536


def encode_campaign_definition(definition: CampaignDefinition) -> bytes:
    return canonical_json_bytes(definition.document())


def decode_campaign_definition(payload: bytes) -> CampaignDefinition:
    if len(payload) > MAX_CAMPAIGN_DEFINITION_BYTES:
        raise ValueError("campaign definition exceeds the size limit")
    root = _root(payload)
    qualification = root.get("campaign_kind") == "qualification"
    expected = {
        "schema",
        "campaign_id",
        "start_utc_ns",
        "campaign_kind",
        "radios",
        "station_digests",
        "cells",
        "maximum_capture_invocations",
        "maximum_fresh_attempts_per_cell",
        "maximum_analysis_invocations",
        "maximum_observed_start_skew_ns",
        "maximum_start_lateness_ns",
        "preflight_lead_ns",
        "capture_mode",
        "analysis_after_each_capture",
        "unit_schedule",
        "qualification_receipt_digest",
        "rounds",
        "slots_per_round",
        "slot_period_numerator_ns",
        "slot_period_denominator",
        "no_catch_up",
    }
    if not qualification:
        expected |= {
            "window_duration_ns",
            "geometry_schedule",
            "capture_run_transition_limit",
            "analysis_drain_transition_limit",
        }
    if set(root) != expected:
        raise ValueError("campaign definition fields are not exact")
    radios = root["radios"]
    station_digests = root["station_digests"]
    if not isinstance(radios, list) or len(radios) != 2:
        raise ValueError("campaign radios must have two entries")
    if not isinstance(station_digests, list) or len(station_digests) != 2:
        raise ValueError("campaign station digests must have two entries")
    if root["cells"] != [item.document() for item in CAMPAIGN_CELLS]:
        raise ValueError("campaign cell matrix differs")
    if root["schema"] != (
        QUALIFICATION_CAMPAIGN_SCHEMA if qualification else CAMPAIGN_SCHEMA
    ):
        raise ValueError("campaign schema differs")
    if root["capture_mode"] != "coordinated" or not isinstance(
        root["analysis_after_each_capture"], bool
    ):
        raise ValueError("campaign execution policy differs")
    if root["rounds"] != (1 if qualification else CAMPAIGN_ROUNDS):
        raise ValueError("campaign rounds differ")
    if root["slots_per_round"] != SLOTS_PER_ROUND:
        raise ValueError("campaign round width differs")
    if root["unit_schedule"] != (
        QUALIFICATION_SCHEDULE_POLICY if qualification else MAIN_SCHEDULE_POLICY
    ):
        raise ValueError("campaign unit schedule differs")
    expected_period = (
        (
            QUALIFICATION_SLOT_PERIOD_NUMERATOR_NS,
            QUALIFICATION_SLOT_PERIOD_DENOMINATOR,
        )
        if qualification
        else (SLOT_PERIOD_NUMERATOR_NS, SLOT_PERIOD_DENOMINATOR)
    )
    if (
        root["slot_period_numerator_ns"] != expected_period[0]
        or root["slot_period_denominator"] != expected_period[1]
        or root["no_catch_up"] is not True
    ):
        raise ValueError("campaign timing policy differs")
    if not qualification and root["window_duration_ns"] != CAMPAIGN_WINDOW_NS:
        raise ValueError("campaign window differs")
    receipt = root["qualification_receipt_digest"]
    definition = CampaignDefinition(
        campaign_id=_string(root["campaign_id"]),
        start_utc_ns=UtcNs(_integer(root["start_utc_ns"])),
        radio_a_id=RadioId(_string(radios[0])),
        radio_b_id=RadioId(_string(radios[1])),
        station_a_digest=_digest(station_digests[0]),
        station_b_digest=_digest(station_digests[1]),
        maximum_start_lateness_ns=_integer(root["maximum_start_lateness_ns"]),
        preflight_lead_ns=_integer(root["preflight_lead_ns"]),
        qualification_receipt_digest=(None if receipt is None else _digest(receipt)),
        qualification=qualification,
        maximum_capture_invocations=_integer(root["maximum_capture_invocations"]),
        maximum_fresh_attempts_per_cell=_integer(
            root["maximum_fresh_attempts_per_cell"]
        ),
        maximum_analysis_invocations=_integer(root["maximum_analysis_invocations"]),
        maximum_observed_start_skew_ns=_integer(root["maximum_observed_start_skew_ns"]),
        analysis_after_each_capture=root["analysis_after_each_capture"],
    )
    if not qualification and (
        root["geometry_schedule"] != [list(item) for item in MAIN_GEOMETRY_SCHEDULE]
        or root["capture_run_transition_limit"]
        != definition.capture_run_transition_limit
        or root["analysis_drain_transition_limit"]
        != definition.analysis_drain_transition_limit
    ):
        raise ValueError("main campaign geometry or transition policy differs")
    if encode_campaign_definition(definition) != payload:
        raise ValueError("campaign definition is not canonical")
    return definition


def encode_qualification_receipt(receipt: CampaignQualificationReceipt) -> bytes:
    return canonical_json_bytes(receipt.document())


def decode_qualification_receipt(payload: bytes) -> CampaignQualificationReceipt:
    if len(payload) > MAX_QUALIFICATION_RECEIPT_BYTES:
        raise ValueError("qualification receipt exceeds the size limit")
    root = _root(payload)
    if (
        set(root)
        != {
            "schema",
            "qualification_definition_digest",
            "issued_utc_ns",
            "unit_digests",
            "snapshot_digests",
            "analysis_receipt_digests",
            "successful_counts",
        }
        or root["schema"] != "org.leo-flow.gauss-v5-campaign-qualification/v1"
    ):
        raise ValueError("qualification receipt fields differ")
    units = root["unit_digests"]
    snapshots = root["snapshot_digests"]
    analysis_receipts = root["analysis_receipt_digests"]
    counts = root["successful_counts"]
    if (
        not isinstance(units, list)
        or not isinstance(snapshots, list)
        or not isinstance(analysis_receipts, list)
        or not isinstance(counts, list)
    ):
        raise TypeError("qualification receipt arrays are invalid")
    receipt = CampaignQualificationReceipt(
        _digest(root["qualification_definition_digest"]),
        UtcNs(_integer(root["issued_utc_ns"])),
        tuple(_digest(item) for item in units),
        tuple(_digest(item) for item in snapshots),
        tuple(_digest(item) for item in analysis_receipts),
        tuple(_integer(item) for item in counts),
    )
    if encode_qualification_receipt(receipt) != payload:
        raise ValueError("qualification receipt is not canonical")
    return receipt


def _root(payload: bytes) -> Mapping[str, Any]:
    value = json.loads(payload)
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("campaign document must be an object")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("campaign string is invalid")
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("campaign integer is invalid")
    return value


def _digest(value: object) -> Digest:
    text = _string(value)
    algorithm, separator, encoded = text.partition(":")
    if not separator:
        raise ValueError("campaign digest has no algorithm")
    return Digest(DigestAlgorithm(algorithm), encoded)
