#!/usr/bin/env python3
"""Validate a LEO Flow benchmark manifest and its frozen legacy oracle.

The module uses only the Python standard library so that corpus integrity can
be checked before the application, numerical stack, or testkit is installed.
It never writes to referenced storage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import defaultdict
from fractions import Fraction
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


MANIFEST_SCHEMA = "leo-flow.benchmark-corpus/v1"
ORACLE_SCHEMA = "leo-flow.legacy-oracle-summary/v1"
SYNTHETIC_SCHEMA = "leo-flow.synthetic-iq-fixtures/v1"
CANONICALIZATION = "leo-flow-benchmark-canonical-json-v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

TRUTH_TIERS = {
    "exact_synthetic": 1,
    "exact_digital_injection": 2,
    "hardware_truth": 3,
    "independent_external_evidence": 4,
    "consensus_proxy": 5,
    "unlabeled_sky": 6,
}
SPLIT_GROUP_KEYS = ("station_id", "capture_session_id", "utc_day", "pass_group_id")


class ValidationError(ValueError):
    """Raised when a benchmark artifact violates a normative invariant."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def canonical_json_bytes(value: Any) -> bytes:
    """Return the corpus-v1 canonical JSON encoding.

    Corpus v1 permits JSON strings, integers, booleans, nulls, lists and maps.
    Floats are rejected so hashes cannot change with numeric formatting or
    NaN/Infinity behavior.  Scientific decimals are represented as strings.
    """

    def reject_floats(item: Any, location: str = "$") -> None:
        if isinstance(item, float):
            raise ValidationError(f"{location}: floats are not canonical corpus values")
        if isinstance(item, Mapping):
            for key, child in item.items():
                _require(isinstance(key, str), f"{location}: map key is not a string")
                reject_floats(child, f"{location}.{key}")
        elif isinstance(item, list):
            for index, child in enumerate(item):
                reject_floats(child, f"{location}[{index}]")

    reject_floats(value)
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def membership_digest(members: Sequence[Mapping[str, Any]]) -> str:
    """Hash the complete, ordered member records.

    Hashing the full records freezes labels, groups and artifact references as
    well as recording identity.  Members must be sorted before this is called.
    """

    return canonical_sha256(list(members))


def split_group_id(grouping: Mapping[str, Any]) -> str:
    # Radio and receiver epoch remain grouping metadata, but are deliberately
    # not part of the default split unit: two radios observing the same station
    # session/day can share sky, temperature and satellite-pass conditions.
    split_basis = {key: grouping[key] for key in SPLIT_GROUP_KEYS}
    return "group-sha256:" + canonical_sha256(split_basis)


def payload_index_digest(entries: Iterable[Mapping[str, Any]]) -> str:
    normalized = sorted(
        [[entry["path"], entry["bytes"], entry["sha256"]] for entry in entries],
        key=lambda row: row[0],
    )
    return canonical_sha256(normalized)


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read JSON {path}: {exc}") from exc


def _validate_sha256(value: Any, location: str) -> None:
    _require(isinstance(value, str) and SHA256_RE.fullmatch(value) is not None,
             f"{location}: expected lowercase SHA-256")


def _validate_relative_path(value: Any, location: str) -> None:
    _require(isinstance(value, str) and value != "", f"{location}: missing path")
    path = PurePosixPath(value)
    _require(not path.is_absolute(), f"{location}: path must be relative to a root ID")
    _require(".." not in path.parts, f"{location}: parent traversal is forbidden")


def _validate_object_ref(ref: Mapping[str, Any], location: str, roots: set[str]) -> None:
    _require(set(ref) == {"root_id", "relative_path", "sha256", "bytes"},
             f"{location}: object ref fields differ from v1")
    _require(ref["root_id"] in roots, f"{location}: unknown root_id")
    _validate_relative_path(ref["relative_path"], f"{location}.relative_path")
    _validate_sha256(ref["sha256"], f"{location}.sha256")
    _require(isinstance(ref["bytes"], int) and ref["bytes"] >= 0,
             f"{location}.bytes: expected non-negative integer")


def _coverage_values(member: Mapping[str, Any], dimension: str) -> set[Any]:
    coverage = member["coverage"]
    if dimension == "receiver_chain_id":
        return set(coverage["receiver_chain_ids"])
    if dimension == "confound":
        return set(coverage["confounds"])
    return {coverage[dimension]}


def coverage_gaps(manifest: Mapping[str, Any]) -> dict[str, list[Any]]:
    gaps: dict[str, list[Any]] = {}
    for dimension, required in manifest["promotion_coverage"].items():
        observed: set[Any] = set()
        for member in manifest["members"]:
            observed.update(_coverage_values(member, dimension))
        missing = [value for value in required if value not in observed]
        if missing:
            gaps[dimension] = missing
    return gaps


def validate_manifest(manifest: Mapping[str, Any], *, promotion_gate: bool = False) -> None:
    required_top = {
        "schema", "corpus_id", "purpose", "status", "canonicalization",
        "membership_digest_sha256", "storage_roots", "split_policy",
        "truth_policy", "promotion_coverage", "members",
    }
    _require(set(manifest) == required_top, "manifest top-level fields differ from v1")
    _require(manifest["schema"] == MANIFEST_SCHEMA, "unsupported benchmark schema")
    _require(manifest["canonicalization"] == CANONICALIZATION,
             "unsupported canonicalization")
    _require(manifest["purpose"] in {"development", "locked_test"},
             "purpose must be development or locked_test")
    _require(manifest["status"] in {"frozen", "sealed"}, "invalid corpus status")
    if manifest["purpose"] == "development":
        _require(manifest["status"] == "frozen", "development corpus must be frozen")
    else:
        _require(manifest["status"] == "sealed", "locked test must be sealed")

    root_items = manifest["storage_roots"]
    _require(isinstance(root_items, list) and root_items, "storage_roots must be non-empty")
    root_ids: set[str] = set()
    for index, root in enumerate(root_items):
        _require(set(root) == {"root_id", "description"},
                 f"storage_roots[{index}]: fields differ from v1")
        _require(root["root_id"] not in root_ids, "duplicate storage root ID")
        root_ids.add(root["root_id"])

    policy = manifest["split_policy"]
    _require(policy.get("unit") == "split_group_id", "split unit must be split_group_id")
    _require(policy.get("assignment") == "explicit_frozen",
             "split assignment must be explicit_frozen")
    _require(policy.get("time_order") == ["train", "validation", "locked_test"],
             "time_order must be train, validation, locked_test")
    _require(policy.get("half_open_intervals") is True,
             "split intervals must be half-open")
    truth_policy = manifest["truth_policy"]
    _require(truth_policy.get("unlabeled_is_negative") is False,
             "unlabeled sky must never be treated as negative")
    _require(truth_policy.get("tle_only_is_ground_truth") is False,
             "TLE-only agreement must not be ground truth")

    members = manifest["members"]
    _require(isinstance(members, list) and members, "members must be non-empty")
    member_ids = [member.get("member_id") for member in members]
    _require(member_ids == sorted(member_ids), "members must be sorted by member_id")
    _require(len(member_ids) == len(set(member_ids)), "member_id values must be unique")
    recordings: set[str] = set()
    group_partitions: dict[str, set[str]] = defaultdict(set)
    oracle_ids: set[str] = set()

    required_member = {
        "member_id", "recording_id", "partition", "legacy_manifest_created_utc_ns",
        "grouping", "split_group_id", "coverage", "truth", "availability",
        "source_manifest_ref", "payload_set", "legacy_analysis_report_ref",
        "legacy_followup_report_ref", "legacy_oracle_entry_id",
    }
    required_grouping = {
        "station_id", "radio_id", "receiver_chain_ids", "hardware_epoch_id",
        "capture_session_id", "utc_day", "pass_group_id",
    }
    required_coverage = {
        "radio_id", "receiver_chain_ids", "observation_mode", "activity_shape",
        "gain_mode", "hardware_epoch_id", "channel_number", "region",
        "evidence_class", "confounds",
    }
    for index, member in enumerate(members):
        location = f"members[{index}]"
        _require(set(member) == required_member, f"{location}: fields differ from v1")
        _require(member["recording_id"] not in recordings,
                 f"{location}: duplicate recording_id")
        recordings.add(member["recording_id"])
        _require(member["partition"] in {"development", "train", "validation", "locked_test"},
                 f"{location}: invalid partition")
        if manifest["purpose"] == "development":
            _require(member["partition"] != "locked_test",
                     f"{location}: locked test member exposed in development corpus")
        else:
            _require(member["partition"] == "locked_test",
                     f"{location}: sealed corpus may contain only locked_test")
        _require(isinstance(member["legacy_manifest_created_utc_ns"], int),
                 f"{location}: UTC nanoseconds must be an integer")
        grouping = member["grouping"]
        _require(set(grouping) == required_grouping,
                 f"{location}.grouping: fields differ from v1")
        _require(grouping["radio_id"] == member["coverage"]["radio_id"],
                 f"{location}: radio differs between grouping and coverage")
        _require(grouping["receiver_chain_ids"] == member["coverage"]["receiver_chain_ids"],
                 f"{location}: receiver chains differ between grouping and coverage")
        _require(member["split_group_id"] == split_group_id(grouping),
                 f"{location}: split_group_id does not match grouping")
        group_partitions[member["split_group_id"]].add(member["partition"])

        coverage = member["coverage"]
        _require(set(coverage) == required_coverage,
                 f"{location}.coverage: fields differ from v1")
        _require(isinstance(coverage["receiver_chain_ids"], list)
                 and len(coverage["receiver_chain_ids"]) == 2,
                 f"{location}: exactly two receiver chains are required")
        _require(coverage["observation_mode"] in {"narrow", "wide", "oversample", "channel-hop"},
                 f"{location}: invalid observation mode")
        _require(coverage["gain_mode"] in {"manual", "slow_attack"},
                 f"{location}: invalid gain mode")
        _require(isinstance(coverage["confounds"], list),
                 f"{location}: confounds must be a list")

        truth = member["truth"]
        _require(set(truth) == {
            "tier", "tier_ordinal", "label", "target_present",
            "usable_for_locked_truth", "provenance",
        }, f"{location}.truth: fields differ from v1")
        _require(isinstance(truth["provenance"], list) and truth["provenance"],
                 f"{location}: label provenance must be non-empty")
        tier = truth.get("tier")
        _require(tier in TRUTH_TIERS, f"{location}: invalid truth tier")
        _require(truth.get("tier_ordinal") == TRUTH_TIERS[tier],
                 f"{location}: truth tier ordinal mismatch")
        if tier == "unlabeled_sky":
            _require(truth.get("target_present") is None,
                     f"{location}: unlabeled sky cannot be positive or negative")
            _require(truth.get("label") == "unlabeled",
                     f"{location}: unlabeled sky must use label=unlabeled")
        if tier == "consensus_proxy":
            _require(truth.get("target_present") is True,
                     f"{location}: current consensus proxies must be positive")
            _require(truth.get("usable_for_locked_truth") is False,
                     f"{location}: proxy cannot be locked truth")

        _require(member["availability"] in {"full_legacy_raw", "retained_evidence_clip"},
                 f"{location}: invalid availability")
        for name in ("source_manifest_ref", "legacy_analysis_report_ref",
                     "legacy_followup_report_ref"):
            _validate_object_ref(member[name], f"{location}.{name}", root_ids)
        payload = member["payload_set"]
        _require(set(payload) == {
            "root_id", "base_relative_path", "index_ref", "index_kind", "index_sha256",
            "object_count", "total_bytes",
        }, f"{location}.payload_set: fields differ from v1")
        _require(payload["root_id"] in root_ids, f"{location}: unknown payload root")
        _validate_relative_path(payload["base_relative_path"],
                                f"{location}.payload_set.base_relative_path")
        _validate_object_ref(payload["index_ref"], f"{location}.payload_set.index_ref",
                             root_ids)
        _require(payload["index_kind"] in {"legacy_chunks_and_survey", "evidence_clips"},
                 f"{location}: invalid payload index kind")
        _validate_sha256(payload["index_sha256"], f"{location}.payload_set.index_sha256")
        _require(isinstance(payload["object_count"], int) and payload["object_count"] > 0,
                 f"{location}: payload object_count must be positive")
        _require(isinstance(payload["total_bytes"], int) and payload["total_bytes"] > 0,
                 f"{location}: payload total_bytes must be positive")
        oracle_id = member["legacy_oracle_entry_id"]
        _require(oracle_id not in oracle_ids, f"{location}: duplicate oracle entry ID")
        oracle_ids.add(oracle_id)

    leaked = {group: sorted(parts) for group, parts in group_partitions.items()
              if len(parts) != 1}
    _require(not leaked, f"split leakage: groups occur in multiple partitions: {leaked}")
    _validate_sha256(manifest["membership_digest_sha256"],
                     "membership_digest_sha256")
    _require(manifest["membership_digest_sha256"] == membership_digest(members),
             "membership_digest_sha256 does not match complete ordered members")
    if promotion_gate:
        gaps = coverage_gaps(manifest)
        _require(not gaps, f"scientific promotion coverage gaps: {gaps}")


def _entries_from_manifest(document: Mapping[str, Any], kind: str) -> list[Mapping[str, Any]]:
    if kind == "legacy_chunks_and_survey":
        entries = list(document.get("chunks", []))
        survey = document.get("survey_iq")
        if survey:
            entries.append(survey)
        return entries
    if kind == "evidence_clips":
        return list(document.get("clips", []))
    raise ValidationError(f"unsupported payload index kind: {kind}")


def _resolve_ref(ref: Mapping[str, Any], roots: Mapping[str, Path]) -> Path:
    root_id = ref["root_id"]
    _require(root_id in roots, f"no local mapping supplied for root {root_id}")
    root = roots[root_id].resolve()
    path = (root / ref["relative_path"]).resolve()
    _require(path == root or root in path.parents, "reference escapes mapped root")
    return path


def _verify_file(path: Path, *, expected_bytes: int, expected_sha256: str) -> None:
    try:
        stat = path.stat()
    except OSError as exc:
        raise ValidationError(f"missing referenced file {path}: {exc}") from exc
    _require(stat.st_size == expected_bytes,
             f"{path}: byte count {stat.st_size} != {expected_bytes}")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    _require(digest.hexdigest() == expected_sha256, f"{path}: SHA-256 mismatch")


def verify_references(
    manifest: Mapping[str, Any],
    roots: Mapping[str, Path],
    *,
    verify_payloads: bool = False,
) -> None:
    """Verify referenced metadata and, optionally, every large payload object."""

    for member in manifest["members"]:
        refs = [member[name] for name in (
            "source_manifest_ref", "legacy_analysis_report_ref",
            "legacy_followup_report_ref",
        )]
        refs.append(member["payload_set"]["index_ref"])
        seen_refs: set[tuple[str, str, str]] = set()
        for ref in refs:
            ref_identity = (ref["root_id"], ref["relative_path"], ref["sha256"])
            if ref_identity in seen_refs:
                continue
            seen_refs.add(ref_identity)
            path = _resolve_ref(ref, roots)
            _verify_file(path, expected_bytes=ref["bytes"], expected_sha256=ref["sha256"])

        source_path = _resolve_ref(member["payload_set"]["index_ref"], roots)
        source = load_json(source_path)
        payload = member["payload_set"]
        entries = _entries_from_manifest(source, payload["index_kind"])
        _require(payload_index_digest(entries) == payload["index_sha256"],
                 f"{member['member_id']}: payload index digest mismatch")
        _require(len(entries) == payload["object_count"],
                 f"{member['member_id']}: payload object count mismatch")
        _require(sum(entry["bytes"] for entry in entries) == payload["total_bytes"],
                 f"{member['member_id']}: payload byte total mismatch")
        if verify_payloads:
            base_ref = {
                "root_id": payload["root_id"],
                "relative_path": payload["base_relative_path"],
            }
            base = _resolve_ref(base_ref, roots)
            for entry in entries:
                candidate = (base / entry["path"]).resolve()
                _require(base == candidate or base in candidate.parents,
                         f"{member['member_id']}: payload path escapes base")
                _verify_file(candidate, expected_bytes=entry["bytes"],
                             expected_sha256=entry["sha256"])


def validate_oracle(oracle: Mapping[str, Any], manifest: Mapping[str, Any]) -> None:
    _require(set(oracle) == {
        "schema", "oracle_id", "corpus_id", "membership_digest_sha256",
        "field_semantics", "entries",
    }, "oracle top-level fields differ from v1")
    _require(oracle["schema"] == ORACLE_SCHEMA, "unsupported oracle schema")
    _require(oracle["corpus_id"] == manifest["corpus_id"], "oracle corpus mismatch")
    _require(oracle["membership_digest_sha256"] == manifest["membership_digest_sha256"],
             "oracle membership digest mismatch")
    _require(oracle["field_semantics"].get("scientific_truth") is False,
             "legacy oracle must explicitly deny scientific truth")
    expected = {member["legacy_oracle_entry_id"]: member for member in manifest["members"]}
    entries = oracle["entries"]
    ids = [entry.get("oracle_entry_id") for entry in entries]
    _require(ids == sorted(ids), "oracle entries must be sorted")
    _require(set(ids) == set(expected), "oracle entries do not match corpus members")
    summary_fields = {
        "window_count", "exact_check_count", "exact_candidate_count",
        "exact_qualified_count", "single_receiver_candidate_count",
        "single_receiver_qualified_count", "followup_trigger_count",
        "doppler_track_qualified",
    }
    confirmation_fields = {
        "confirmed", "same_receiver_confirmed", "dual_receiver_confirmed",
        "cross_receiver_confirmed",
    }
    for index, entry in enumerate(entries):
        location = f"oracle.entries[{index}]"
        member = expected[entry["oracle_entry_id"]]
        _require(entry.get("recording_id") == member["recording_id"],
                 f"{location}: recording mismatch")
        _require(entry.get("analysis_report_sha256")
                 == member["legacy_analysis_report_ref"]["sha256"],
                 f"{location}: analysis report digest mismatch")
        _require(entry.get("followup_report_sha256")
                 == member["legacy_followup_report_ref"]["sha256"],
                 f"{location}: followup report digest mismatch")
        _require(set(entry.get("analysis_summary", {})) == summary_fields,
                 f"{location}: analysis summary fields differ from v1")
        _require(set(entry.get("confirmation", {})) == confirmation_fields,
                 f"{location}: confirmation fields differ from v1")
        for key, value in entry["analysis_summary"].items():
            if key == "doppler_track_qualified":
                _require(isinstance(value, bool), f"{location}.{key}: expected bool")
            else:
                _require(isinstance(value, int) and value >= 0,
                         f"{location}.{key}: expected non-negative integer")
        for key, value in entry["confirmation"].items():
            _require(isinstance(value, bool), f"{location}.{key}: expected bool")


def validate_synthetic_spec(spec: Mapping[str, Any]) -> None:
    _require(spec.get("schema") == SYNTHETIC_SCHEMA, "unsupported synthetic fixture schema")
    _require(spec.get("generator_contract", {}).get("detector_independent") is True,
             "synthetic generator must be detector independent")
    layout = spec.get("sample_contract", {})
    _require(layout.get("dtype") == "int16_le", "synthetic dtype must be int16_le")
    _require(layout.get("layout") == "sample,receiver,component",
             "synthetic layout must be sample,receiver,component")
    _require(layout.get("component_order") == ["i", "q"], "component order must be i,q")
    cases = spec.get("cases")
    _require(isinstance(cases, list) and cases, "synthetic cases must be non-empty")
    ids = [case.get("case_id") for case in cases]
    _require(ids == sorted(ids) and len(ids) == len(set(ids)),
             "synthetic cases must have unique sorted IDs")
    for index, case in enumerate(cases):
        location = f"synthetic.cases[{index}]"
        _require(isinstance(case.get("sample_rate_hz"), int) and case["sample_rate_hz"] > 0,
                 f"{location}: sample_rate_hz must be positive integer")
        _require(isinstance(case.get("sample_count"), int) and case["sample_count"] > 0,
                 f"{location}: sample_count must be positive integer")
        _require(isinstance(case.get("seed_u64"), int)
                 and 0 <= case["seed_u64"] < 2**64,
                 f"{location}: seed_u64 out of range")
        signal = case.get("signal_truth", {})
        for key in ("frequency_start_hz", "drift_hz_s", "snr_db"):
            _require(isinstance(signal.get(key), str),
                     f"{location}: {key} must be an exact decimal string")
            value = float(signal[key])
            _require(math.isfinite(value), f"{location}: {key} is not finite")
        frequency = (int(signal["phase_step_start_q64"])
                     * case["sample_rate_hz"] / 2**64)
        drift = (int(signal["phase_step_delta_q64"])
                 * case["sample_rate_hz"] ** 2 / 2**64)
        _require(math.isclose(float(signal["frequency_start_hz"]), frequency,
                              rel_tol=0.0, abs_tol=5e-10),
                 f"{location}: frequency decimal disagrees with Q64 truth")
        _require(math.isclose(float(signal["drift_hz_s"]), drift,
                              rel_tol=0.0, abs_tol=5e-9),
                 f"{location}: drift decimal disagrees with Q64 truth")
        snr_ratio = Fraction(signal["snr_linear_ratio"])
        expected_ratio = Fraction(
            3 * int(signal["amplitude_counts"]) ** 2,
            2 * int(case["noise_truth"]["uniform_component_peak_counts"])
            * (int(case["noise_truth"]["uniform_component_peak_counts"]) + 1),
        )
        _require(snr_ratio == expected_ratio,
                 f"{location}: SNR ratio disagrees with amplitude/noise truth")
        _require(math.isclose(float(signal["snr_db"]),
                              10.0 * math.log10(float(snr_ratio)),
                              rel_tol=0.0, abs_tol=5e-9),
                 f"{location}: SNR dB disagrees with exact ratio")
        receivers = case.get("receiver_truth")
        _require(isinstance(receivers, list) and len(receivers) == 2,
                 f"{location}: exactly two receivers required")
        for rx, receiver in enumerate(receivers):
            _require(isinstance(receiver.get("delay_samples"), int),
                     f"{location}: receiver {rx} delay must be integer")
            for key in ("gain_db", "phase_offset_rad"):
                _require(isinstance(receiver.get(key), str)
                         and math.isfinite(float(receiver[key])),
                         f"{location}: receiver {rx} {key} must be exact decimal string")
        quant = case.get("quantization_truth", {})
        _require(quant.get("rounding") == "nearest_ties_away_from_zero",
                 f"{location}: quantization rounding not explicit")
        _require(isinstance(quant.get("clip_min"), int)
                 and isinstance(quant.get("clip_max"), int)
                 and -32768 <= quant["clip_min"] < quant["clip_max"] <= 32767,
                 f"{location}: invalid CI16 clipping bounds")
        expected = case.get("expected_truth", {})
        _require(expected.get("bytes") == case["sample_count"] * 8,
                 f"{location}: paired CI16 byte count mismatch")
        _require(isinstance(expected.get("clipped_component_count"), int)
                 and expected["clipped_component_count"] >= 0,
                 f"{location}: clipped count must be non-negative")
        _validate_sha256(expected.get("ci16_sha256"), f"{location}.ci16_sha256")


def _parse_roots(values: Sequence[str]) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for value in values:
        root_id, separator, path = value.partition("=")
        if not separator or not root_id or not path:
            raise ValidationError("--root must be ROOT_ID=/absolute/path")
        candidate = Path(path)
        _require(candidate.is_absolute(), f"root {root_id}: path must be absolute")
        _require(root_id not in roots, f"duplicate --root {root_id}")
        roots[root_id] = candidate
    return roots


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--oracle", type=Path)
    parser.add_argument("--synthetic-spec", type=Path)
    parser.add_argument("--promotion-gate", action="store_true")
    parser.add_argument("--verify-files", action="store_true")
    parser.add_argument("--verify-payloads", action="store_true",
                        help="also hash every large IQ payload; potentially very slow")
    parser.add_argument("--root", action="append", default=[], metavar="ID=PATH")
    args = parser.parse_args(argv)
    try:
        manifest = load_json(args.manifest)
        validate_manifest(manifest, promotion_gate=args.promotion_gate)
        if args.oracle:
            validate_oracle(load_json(args.oracle), manifest)
        if args.synthetic_spec:
            validate_synthetic_spec(load_json(args.synthetic_spec))
        if args.verify_files or args.verify_payloads:
            verify_references(manifest, _parse_roots(args.root),
                              verify_payloads=args.verify_payloads)
        gaps = coverage_gaps(manifest)
    except ValidationError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 2
    print(
        f"VALID {manifest['corpus_id']} members={len(manifest['members'])} "
        f"membership_sha256={manifest['membership_digest_sha256']}"
    )
    if gaps:
        print("PROMOTION_GAPS " + json.dumps(gaps, sort_keys=True))
    else:
        print("PROMOTION_COVERAGE complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
