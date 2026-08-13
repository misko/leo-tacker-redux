from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from benchmark.synthetic_iq import generate_case, verify_spec
from benchmark.validate import (
    ValidationError,
    canonical_json_bytes,
    coverage_gaps,
    load_json,
    membership_digest,
    payload_index_digest,
    validate_manifest,
    validate_oracle,
    validate_synthetic_spec,
)

MANIFEST_PATH = ROOT / "benchmark/manifests/development-2026-08-13.json"
ORACLE_PATH = ROOT / "benchmark/oracles/development-2026-08-13.legacy-summary.json"
SYNTHETIC_PATH = ROOT / "benchmark/specs/synthetic-iq-v1.json"


class BenchmarkManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_json(MANIFEST_PATH)

    def test_frozen_development_manifest_is_valid(self) -> None:
        validate_manifest(self.manifest)
        self.assertEqual(len(self.manifest["members"]), 9)
        self.assertEqual(
            membership_digest(self.manifest["members"]),
            "b7d8c18ac3a7933c13bf38de3b8056eec208c4cac4198cab962ab724aeeaf0f9",
        )

    def test_canonical_json_is_independent_of_mapping_order(self) -> None:
        left = {"z": [1, {"b": True, "a": None}], "a": "value"}
        right = {"a": "value", "z": [1, {"a": None, "b": True}]}
        self.assertEqual(canonical_json_bytes(left), canonical_json_bytes(right))

    def test_canonical_json_rejects_floats(self) -> None:
        with self.assertRaisesRegex(ValidationError, "floats are not canonical"):
            canonical_json_bytes({"unstable": 0.1})

    def test_any_member_change_invalidates_frozen_membership(self) -> None:
        changed = copy.deepcopy(self.manifest)
        changed["members"][0]["truth"]["label"] = "quietly_changed"
        with self.assertRaisesRegex(ValidationError, "membership_digest"):
            validate_manifest(changed)

    def test_one_split_group_cannot_cross_partitions(self) -> None:
        changed = copy.deepcopy(self.manifest)
        changed["members"][0]["partition"] = "train"
        changed["membership_digest_sha256"] = membership_digest(changed["members"])
        with self.assertRaisesRegex(ValidationError, "split leakage"):
            validate_manifest(changed)

    def test_unlabeled_sky_cannot_become_a_negative(self) -> None:
        changed = copy.deepcopy(self.manifest)
        member = changed["members"][1]
        member["truth"]["target_present"] = False
        changed["membership_digest_sha256"] = membership_digest(changed["members"])
        with self.assertRaisesRegex(ValidationError, "unlabeled sky"):
            validate_manifest(changed)

    def test_development_corpus_deliberately_fails_promotion_gate(self) -> None:
        self.assertEqual(
            coverage_gaps(self.manifest),
            {
                "evidence_class": [
                    "exact_injection_positive",
                    "hardware_positive",
                    "independent_positive",
                    "hard_null",
                ],
                "confound": [
                    "real_interference",
                    "interrupted_source",
                    "corrupt_source",
                ],
            },
        )
        with self.assertRaisesRegex(ValidationError, "promotion coverage gaps"):
            validate_manifest(self.manifest, promotion_gate=True)

    def test_path_traversal_is_rejected(self) -> None:
        changed = copy.deepcopy(self.manifest)
        changed["members"][0]["source_manifest_ref"]["relative_path"] = "../secret"
        changed["membership_digest_sha256"] = membership_digest(changed["members"])
        with self.assertRaisesRegex(ValidationError, "parent traversal"):
            validate_manifest(changed)

    def test_payload_index_hash_is_order_independent_but_content_sensitive(
        self,
    ) -> None:
        entries = [
            {"path": "b.ci16", "bytes": 8, "sha256": "b" * 64},
            {"path": "a.ci16", "bytes": 4, "sha256": "a" * 64},
        ]
        self.assertEqual(
            payload_index_digest(entries), payload_index_digest(reversed(entries))
        )
        changed = copy.deepcopy(entries)
        changed[0]["bytes"] += 1
        self.assertNotEqual(
            payload_index_digest(entries), payload_index_digest(changed)
        )

    def test_frozen_legacy_oracle_matches_every_member_and_source_hash(self) -> None:
        oracle = load_json(ORACLE_PATH)
        validate_oracle(oracle, self.manifest)
        changed = copy.deepcopy(oracle)
        changed["entries"][0]["analysis_report_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValidationError, "analysis report digest"):
            validate_oracle(changed, self.manifest)


class SyntheticFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = load_json(SYNTHETIC_PATH)

    def test_synthetic_contract_and_all_normative_hashes_validate(self) -> None:
        validate_synthetic_spec(self.spec)
        verify_spec(self.spec)

    def test_ci16_layout_size_and_truth_are_exact(self) -> None:
        for case in self.spec["cases"]:
            data, truth = generate_case(case)
            self.assertEqual(len(data), case["sample_count"] * 2 * 2 * 2)
            self.assertEqual(truth, case["expected_truth"])

    def test_truth_covers_frequency_drift_snr_delay_gain_and_clipping(self) -> None:
        for case in self.spec["cases"]:
            signal = case["signal_truth"]
            self.assertIn("frequency_start_hz", signal)
            self.assertIn("drift_hz_s", signal)
            self.assertIn("snr_db", signal)
            for receiver in case["receiver_truth"]:
                self.assertIn("delay_samples", receiver)
                self.assertIn("gain_db", receiver)
            self.assertIn("clip_min", case["quantization_truth"])
            self.assertIn("clipped_component_count", case["expected_truth"])

    def test_seed_tampering_is_detected_by_normative_ci16_hash(self) -> None:
        changed = copy.deepcopy(self.spec)
        changed["cases"][0]["seed_u64"] += 1
        with self.assertRaisesRegex(ValidationError, "ci16_sha256"):
            verify_spec(changed)

    def test_spec_is_plain_json_without_nonstandard_constants(self) -> None:
        serialized = SYNTHETIC_PATH.read_text(encoding="utf-8")
        parsed = json.loads(serialized, parse_constant=lambda value: self.fail(value))
        self.assertEqual(parsed["schema"], "leo-flow.synthetic-iq-fixtures/v1")


if __name__ == "__main__":
    unittest.main()
