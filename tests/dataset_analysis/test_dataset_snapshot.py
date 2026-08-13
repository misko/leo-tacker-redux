from __future__ import annotations

import json
from dataclasses import replace

import pytest

from leo_flow.analysis.dataset import (
    DatasetCandidate,
    DatasetRole,
    DatasetSnapshotBundle,
    DatasetSplit,
    LabelEvidence,
    LabelSource,
    MalformedDatasetSnapshotError,
    TruthLabel,
    carve_dataset,
    decode_dataset_snapshot,
    encode_dataset_snapshot,
    freeze_dataset_snapshot,
    verify_snapshot_ref,
)
from leo_flow.contracts.core import (
    AnalysisRunId,
    Digest,
    FeatureSetId,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.features import FeatureSetRef
from leo_flow.contracts.storage import ObjectRef


def _digest(value: str) -> Digest:
    return Digest.sha256(value.encode())


def _truth(source: LabelSource, present: bool | None) -> TruthLabel:
    injection = (
        {
            "base_recording_digest": _digest("base-noise"),
            "injection_spec_digest": _digest("injection-spec"),
        }
        if source is LabelSource.INJECTED
        else {}
    )
    return TruthLabel(
        target_present=present,
        source=source,
        evidence=(
            LabelEvidence(
                source=source,
                evidence_digest=_digest(f"evidence-{source.value}-{present}"),
                producer_id="independent-fixture-v1",
                produced_utc_ns=100,
                independent_of_method_ids=("method-a",),
                uncertainty=(("snr_db", "+/-0.5"),),
                **injection,
            ),
        ),
        confidence=None if present is None else 1.0,
    )


def _candidate(
    index: int,
    group: str,
    truth: TruthLabel,
    *,
    scored: bool = True,
) -> DatasetCandidate:
    return DatasetCandidate(
        feature_set_id=f"fset_{index}",
        feature_set_digest=_digest(f"feature-{index}"),
        recording_id=f"rec_{index}",
        split_group_id=group,
        captured_utc_ns=index * 1_000,
        radio_id="radio_v5a",
        lnb_ids=("lnb-a",),
        observation_mode="wide",
        sample_rate_hz=30_720_000,
        gain_mode="manual",
        gain_db="50",
        satellite_id="25544",
        truth=truth,
        scored_truth=scored,
    )


def _ref(candidate: DatasetCandidate, *, locator: str | None = None) -> FeatureSetRef:
    return FeatureSetRef(
        feature_set_id=FeatureSetId(candidate.feature_set_id),
        analysis_run_id=AnalysisRunId(
            f"arun_{candidate.feature_set_id.removeprefix('fset_')}"
        ),
        bundle_ref=ObjectRef(
            digest=candidate.feature_set_digest,
            byte_count=512,
            media_type="application/json",
            format_id="feature-set-json-v0.1",
            locator=locator or f"opaque://features/{candidate.feature_set_id}",
        ),
    )


def _snapshot(*, context: bool = True) -> DatasetSnapshotBundle:
    candidates = (
        _candidate(1, "pass-a", _truth(LabelSource.INJECTED, True)),
        _candidate(2, "pass-b", _truth(LabelSource.OBSERVED, False)),
        _candidate(3, "pass-c", _truth(LabelSource.MANUAL, False)),
    )
    if context:
        candidates += (
            replace(
                _candidate(
                    4,
                    "pass-a",
                    _truth(LabelSource.UNLABELED, None),
                    scored=False,
                ),
                captured_utc_ns=1_500,
            ),
        )
    carved = carve_dataset(
        reversed(candidates),
        group_partitions={
            "pass-a": DatasetSplit.TRAIN,
            "pass-b": DatasetSplit.VALIDATION,
            "pass-c": DatasetSplit.LOCKED_TEST,
        },
        evaluated_method_id="method-a",
        require_promotion=True,
    )
    return freeze_dataset_snapshot(
        carved,
        candidates,
        tuple(_ref(item) for item in candidates),
        selection_spec="reviewed-groups:v1",
        selection_cutoff_utc_ns=UtcNs(10_000),
    )


def test_freeze_reconciles_rich_carve_with_unchanged_model_snapshot() -> None:
    snapshot = _snapshot()

    assert snapshot.promoted is True
    assert snapshot.feature_dataset.selection_spec == "reviewed-groups:v1"
    assert snapshot.feature_dataset.ordered_feature_set_refs == tuple(
        member.feature_set_ref for member in snapshot.members
    )
    assert [member.split for member in snapshot.members] == [
        DatasetSplit.TRAIN,
        DatasetSplit.TRAIN,
        DatasetSplit.VALIDATION,
        DatasetSplit.LOCKED_TEST,
    ]
    assert snapshot.members_in(DatasetSplit.TRAIN, role=DatasetRole.CONTEXT_ONLY) == (
        snapshot.members[1],
    )
    assert snapshot.members[0].truth.evidence[0].injection_spec_digest == _digest(
        "injection-spec"
    )


def test_round_trip_is_deterministic_and_verifies_both_digests() -> None:
    snapshot = _snapshot()
    encoded = encode_dataset_snapshot(snapshot)
    decoded = decode_dataset_snapshot(encoded)

    assert decoded == snapshot
    assert encode_dataset_snapshot(decoded) == encoded
    verify_snapshot_ref(decoded, snapshot.ref)


def test_truth_or_split_tampering_fails_rich_snapshot_digest() -> None:
    document = json.loads(encode_dataset_snapshot(_snapshot()))
    document["members"][0]["split"] = "validation"

    with pytest.raises(
        MalformedDatasetSnapshotError,
        match="(split group cannot cross|snapshot digest)",
    ):
        decode_dataset_snapshot(canonical_json_bytes(document))

    document = json.loads(encode_dataset_snapshot(_snapshot()))
    document["feature_dataset"]["selection_spec"] = "unreviewed:tampered"
    with pytest.raises(MalformedDatasetSnapshotError, match="snapshot digest"):
        decode_dataset_snapshot(canonical_json_bytes(document))


def test_feature_membership_tampering_fails_legacy_model_digest() -> None:
    document = json.loads(encode_dataset_snapshot(_snapshot()))
    document["members"][0]["feature_set_ref"]["analysis_run_id"] = "arun_tampered"

    with pytest.raises(
        MalformedDatasetSnapshotError, match="membership_digest does not match"
    ):
        decode_dataset_snapshot(canonical_json_bytes(document))


def test_codec_rejects_unknown_and_duplicate_fields() -> None:
    encoded = encode_dataset_snapshot(_snapshot())
    document = json.loads(encoded)
    document["random_seed"] = 42
    with pytest.raises(MalformedDatasetSnapshotError, match="root fields differ"):
        decode_dataset_snapshot(canonical_json_bytes(document))

    duplicate = encoded.replace(
        b'{"evaluated_method_id"', b'{"promoted":true,"evaluated_method_id"', 1
    )
    with pytest.raises(MalformedDatasetSnapshotError, match="duplicate JSON key"):
        decode_dataset_snapshot(duplicate)


def test_codec_rejects_semantically_equal_noncanonical_json() -> None:
    canonical = encode_dataset_snapshot(_snapshot())
    document = json.loads(canonical)
    pretty = json.dumps(document, indent=2).encode()

    assert json.loads(pretty) == json.loads(canonical)
    with pytest.raises(MalformedDatasetSnapshotError, match="not canonical JSON"):
        decode_dataset_snapshot(pretty)

    reordered = json.dumps(
        {key: document[key] for key in reversed(document)}, separators=(",", ":")
    ).encode()
    assert json.loads(reordered) == json.loads(canonical)
    with pytest.raises(MalformedDatasetSnapshotError, match="not canonical JSON"):
        decode_dataset_snapshot(reordered)


def test_freeze_rejects_feature_digest_or_membership_substitution() -> None:
    candidates = (
        _candidate(1, "pass-a", _truth(LabelSource.INJECTED, True)),
        _candidate(2, "pass-b", _truth(LabelSource.OBSERVED, False)),
        _candidate(3, "pass-c", _truth(LabelSource.MANUAL, False)),
    )
    carved = carve_dataset(
        candidates,
        group_partitions={
            "pass-a": DatasetSplit.TRAIN,
            "pass-b": DatasetSplit.VALIDATION,
            "pass-c": DatasetSplit.LOCKED_TEST,
        },
        evaluated_method_id="method-a",
    )
    bad_ref = replace(
        _ref(candidates[0]),
        bundle_ref=replace(
            _ref(candidates[0]).bundle_ref, digest=_digest("substitution")
        ),
    )
    with pytest.raises(ValueError, match="feature digest mismatch"):
        freeze_dataset_snapshot(
            carved,
            candidates,
            (bad_ref, _ref(candidates[1]), _ref(candidates[2])),
            selection_spec="reviewed:v1",
            selection_cutoff_utc_ns=UtcNs(10_000),
        )

    changed_truth = replace(
        candidates[2], truth=_truth(LabelSource.PSEUDO_LABEL, False)
    )
    with pytest.raises(ValueError, match="no longer matches carved snapshot"):
        freeze_dataset_snapshot(
            carved,
            (candidates[0], candidates[1], changed_truth),
            tuple(_ref(item) for item in candidates),
            selection_spec="reviewed:v1",
            selection_cutoff_utc_ns=UtcNs(10_000),
        )


def test_locator_replacement_does_not_change_scientific_identity() -> None:
    original = _snapshot(context=False)
    relocated_members = tuple(
        replace(
            member,
            feature_set_ref=replace(
                member.feature_set_ref,
                bundle_ref=replace(
                    member.feature_set_ref.bundle_ref,
                    locator=f"replacement://{member.feature_set_ref.feature_set_id}",
                ),
            ),
        )
        for member in original.members
    )
    relocated_feature_dataset = replace(
        original.feature_dataset,
        ordered_feature_set_refs=tuple(
            member.feature_set_ref for member in relocated_members
        ),
    )
    relocated = replace(
        original,
        feature_dataset=relocated_feature_dataset,
        members=relocated_members,
    )

    assert (
        relocated.feature_dataset.membership_digest
        == original.feature_dataset.membership_digest
    )
    assert relocated.snapshot_digest == original.snapshot_digest
    assert encode_dataset_snapshot(relocated) != encode_dataset_snapshot(original)


def test_reader_ref_detects_snapshot_substitution() -> None:
    expected = _snapshot(context=False)
    other = _snapshot(context=True)
    with pytest.raises(ValueError, match="does not match its ref"):
        verify_snapshot_ref(other, expected.ref)


def test_bundle_rejects_group_leakage_and_false_promotion_state() -> None:
    snapshot = _snapshot()
    leaked_member = replace(
        snapshot.members[-1],
        split_group_id=snapshot.members[0].split_group_id,
    )
    with pytest.raises(ValueError, match="split group cannot cross"):
        replace(snapshot, members=snapshot.members[:-1] + (leaked_member,))
    with pytest.raises(ValueError, match="promotion state"):
        replace(snapshot, promoted=False)
