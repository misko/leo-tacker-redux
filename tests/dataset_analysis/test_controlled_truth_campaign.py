from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from leo_flow.analysis.dataset import (
    CAMPAIGN_SCHEMA,
    CampaignEphemerisInput,
    CampaignGroup,
    CampaignManifest,
    CampaignMatrixPoint,
    CampaignMember,
    CampaignTruthKind,
    CampaignTruthProvenance,
    CampaignTruthSpec,
    CampaignValidationError,
    DatasetSplit,
    LockedTestOpening,
    MetricBound,
    TruthProducerKind,
    decode_campaign,
    encode_campaign,
    materialize_campaign,
)
from leo_flow.contracts.core import Digest

METHODS = ("edge-energy@1", "paired-delay@2")
ROOT = Path(__file__).resolve().parents[2]


def digest(value: str) -> Digest:
    return Digest.sha256(value.encode())


def matrix(
    *, null_class: str | None = None, confounders: tuple[str, ...] = ()
) -> CampaignMatrixPoint:
    return CampaignMatrixPoint(
        snr_db="-8.5",
        frequency_offset_hz=125_000,
        drift_hz_s="-2750.25",
        receiver_delay_samples=(0, 3),
        receiver_gain_db=("0", "-1.25"),
        clipping=True,
        clip_min=-32768,
        clip_max=32767,
        null_class=null_class,
        confounders=confounders,
    )


def truth(
    truth_id: str,
    group_id: str,
    kind: CampaignTruthKind,
    *,
    target: bool | None,
    base: str | None = None,
) -> CampaignTruthSpec:
    producer = {
        CampaignTruthKind.BASE_NOISE: TruthProducerKind.SCORE_BLIND_SELECTION,
        CampaignTruthKind.DIGITAL_INJECTION: TruthProducerKind.INDEPENDENT_GENERATOR,
        CampaignTruthKind.CONTROLLED_RF: TruthProducerKind.CONTROLLED_INSTRUMENT,
        CampaignTruthKind.HARD_NULL: TruthProducerKind.SCORE_BLIND_SELECTION,
        CampaignTruthKind.CONFOUNDER: TruthProducerKind.INDEPENDENT_CHARACTERIZATION,
    }[kind]
    point = matrix(
        null_class="quiet-load" if kind is CampaignTruthKind.HARD_NULL else None,
        confounders=("known-lte-interferer",)
        if kind is CampaignTruthKind.CONFOUNDER
        else (),
    )
    return CampaignTruthSpec(
        truth_spec_id=truth_id,
        group_id=group_id,
        kind=kind,
        target_present=target,
        base_truth_spec_id=base,
        provenance=CampaignTruthProvenance(
            producer_id=f"truth-steward-{truth_id}",
            producer_kind=producer,
            producer_artifact_digest=digest(f"producer-{truth_id}"),
            evidence_digest=digest(f"evidence-{truth_id}"),
            selection_policy_digest=digest(f"selection-{truth_id}"),
            produced_utc_ns=10,
            independent_of_method_ids=METHODS,
            detector_derived=False,
        ),
        matrix=point,
    )


def member(
    index: int,
    spec: CampaignTruthSpec,
    captured: int,
    *,
    base_recording_id: str | None = None,
    scored: bool,
) -> CampaignMember:
    return CampaignMember(
        truth_spec_id=spec.truth_spec_id,
        group_id=spec.group_id,
        recording_id=f"recording-{index}",
        recording_identity_digest=digest(f"recording-{index}"),
        feature_set_id=f"feature-{index}",
        feature_set_digest=digest(f"feature-{index}"),
        captured_utc_ns=captured,
        radio_id="radio-v5-a",
        lnb_ids=("lnb-a", "lnb-b"),
        observation_mode="controlled-campaign",
        sample_rate_hz=2_000_000,
        gain_mode="manual",
        gain_db="50",
        scored_truth=scored,
        derived_from_recording_id=base_recording_id,
        ephemeris_input=CampaignEphemerisInput(
            snapshot_digest=digest(f"tle-{index}"),
            provenance_digest=digest(f"tle-provenance-{index}"),
            selection_policy_digest=digest("available-then-v1"),
            selection_policy="available_then",
            available_utc_ns=captured - 1,
        ),
    )


def campaign() -> CampaignManifest:
    train_base = truth(
        "truth-train-base", "group-train", CampaignTruthKind.BASE_NOISE, target=None
    )
    train_injection = truth(
        "truth-train-injection",
        "group-train",
        CampaignTruthKind.DIGITAL_INJECTION,
        target=True,
        base=train_base.truth_spec_id,
    )
    validation_base = truth(
        "truth-validation-base",
        "group-validation",
        CampaignTruthKind.BASE_NOISE,
        target=None,
    )
    validation_null = truth(
        "truth-validation-null",
        "group-validation",
        CampaignTruthKind.HARD_NULL,
        target=False,
    )
    test_base = truth(
        "truth-test-base", "group-test", CampaignTruthKind.BASE_NOISE, target=None
    )
    test_confounder = truth(
        "truth-test-confounder",
        "group-test",
        CampaignTruthKind.CONFOUNDER,
        target=False,
    )
    truths = (
        train_base,
        train_injection,
        validation_base,
        validation_null,
        test_base,
        test_confounder,
    )
    groups = (
        CampaignGroup(
            "group-train",
            DatasetSplit.TRAIN,
            100,
            200,
            train_base.truth_spec_id,
            (train_injection.truth_spec_id,),
            (train_base.truth_spec_id, train_injection.truth_spec_id),
        ),
        CampaignGroup(
            "group-validation",
            DatasetSplit.VALIDATION,
            300,
            400,
            validation_base.truth_spec_id,
            (),
            (validation_base.truth_spec_id, validation_null.truth_spec_id),
        ),
        CampaignGroup(
            "group-test",
            DatasetSplit.LOCKED_TEST,
            500,
            600,
            test_base.truth_spec_id,
            (),
            (test_base.truth_spec_id, test_confounder.truth_spec_id),
        ),
    )
    train_base_member = member(1, train_base, 110, scored=False)
    validation_base_member = member(3, validation_base, 310, scored=False)
    test_base_member = member(5, test_base, 510, scored=False)
    members = (
        train_base_member,
        member(
            2,
            train_injection,
            111,
            base_recording_id=train_base_member.recording_id,
            scored=True,
        ),
        validation_base_member,
        member(4, validation_null, 311, scored=True),
        test_base_member,
        member(6, test_confounder, 511, scored=True),
    )
    return CampaignManifest(
        campaign_id="controlled-truth-campaign-v1-example",
        frozen_utc_ns=700,
        candidate_method_ids=METHODS,
        truth_specs=truths,
        groups=groups,
        members=members,
    )


def opening(value: CampaignManifest) -> LockedTestOpening:
    return LockedTestOpening(
        campaign_digest=value.digest,
        method_id=METHODS[0],
        method_artifact_digest=digest("method-code"),
        config_digest=digest("method-config"),
        metrics_spec_digest=digest("metric-definitions"),
        metric_bounds=(
            MetricBound("false-positive-rate", None, "0.01"),
            MetricBound("recall", "0.90", None),
        ),
        frozen_utc_ns=710,
        opened_utc_ns=720,
        steward_id="release-steward",
    )


def test_campaign_round_trip_freezes_truth_and_recording_feature_membership() -> None:
    expected = campaign()
    encoded = encode_campaign(expected)
    restored = decode_campaign(encoded)
    materialized = materialize_campaign(restored, evaluated_method_id=METHODS[0])

    assert restored == expected
    assert json.loads(encoded)["schema"] == CAMPAIGN_SCHEMA
    assert tuple(member.feature_set_id for member in materialized.ordered_members) == (
        "feature-1",
        "feature-2",
        "feature-3",
        "feature-4",
        "feature-5",
        "feature-6",
    )
    assert tuple(member.recording_id for member in materialized.ordered_members) == (
        "recording-1",
        "recording-2",
        "recording-3",
        "recording-4",
        "recording-5",
        "recording-6",
    )
    assert len(materialized.members_in(DatasetSplit.TRAIN)) == 2


def test_benchmark_schema_is_versioned_strict_and_covers_the_campaign_matrix() -> None:
    schema = json.loads(
        (
            ROOT / "benchmark/specs/controlled-truth-dataset-campaign-v1.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert schema["properties"]["schema"]["const"] == CAMPAIGN_SCHEMA
    assert schema["additionalProperties"] is False
    assert set(schema["$defs"]["matrix"]["required"]) == {
        "snr_db",
        "frequency_offset_hz",
        "drift_hz_s",
        "receiver_delay_samples",
        "receiver_gain_db",
        "clipping",
        "clip_min",
        "clip_max",
        "null_class",
        "confounders",
    }
    example = decode_campaign(
        (
            ROOT / "benchmark/manifests/controlled-truth-campaign-v1.example.json"
        ).read_bytes()
    )
    assert example.campaign_id == "controlled-truth-campaign-v1-example"
    assert len(example.members) == 6


def test_locked_test_requires_campaign_bound_frozen_method_config_and_bounds() -> None:
    value = campaign()
    materialized = materialize_campaign(value, evaluated_method_id=METHODS[0])
    with pytest.raises(CampaignValidationError, match="remains sealed"):
        materialized.members_in(DatasetSplit.LOCKED_TEST)

    receipt = opening(value)
    assert len(materialized.members_in(DatasetSplit.LOCKED_TEST, opening=receipt)) == 2
    with pytest.raises(CampaignValidationError, match="another campaign"):
        materialized.members_in(
            DatasetSplit.LOCKED_TEST,
            opening=replace(receipt, campaign_digest=digest("other-campaign")),
        )
    with pytest.raises(CampaignValidationError, match="materialized method"):
        materialized.members_in(
            DatasetSplit.LOCKED_TEST,
            opening=replace(receipt, method_id=METHODS[1]),
        )
    with pytest.raises(CampaignValidationError, match="before campaign membership"):
        materialized.members_in(
            DatasetSplit.LOCKED_TEST,
            opening=replace(receipt, frozen_utc_ns=600),
        )


def test_detector_derived_or_incompletely_independent_truth_is_forbidden() -> None:
    with pytest.raises(CampaignValidationError, match="detector-derived"):
        replace(
            campaign().truth_specs[1].provenance,
            detector_derived=True,
        )
    changed = campaign()
    bad_truth = replace(
        changed.truth_specs[1],
        provenance=replace(
            changed.truth_specs[1].provenance,
            independent_of_method_ids=(METHODS[0],),
        ),
    )
    with pytest.raises(CampaignValidationError, match="every candidate method"):
        replace(
            changed,
            truth_specs=(changed.truth_specs[0], bad_truth, *changed.truth_specs[2:]),
        )


def test_duplicate_result_identity_and_group_leakage_are_forbidden() -> None:
    value = campaign()
    duplicate = replace(
        value.members[1], feature_set_digest=value.members[0].feature_set_digest
    )
    with pytest.raises(CampaignValidationError, match="FeatureSet digest"):
        replace(value, members=(value.members[0], duplicate, *value.members[2:]))

    leaked = replace(value.members[1], derived_from_recording_id="recording-3")
    with pytest.raises(CampaignValidationError, match="group's base recording"):
        replace(value, members=(value.members[0], leaked, *value.members[2:]))


def test_every_injection_and_base_must_materialize_in_the_same_group() -> None:
    value = campaign()
    group = replace(value.groups[0], injection_truth_spec_ids=())
    with pytest.raises(CampaignValidationError, match="every and only"):
        replace(value, groups=(group, *value.groups[1:]))
    with pytest.raises(CampaignValidationError, match="every and only frozen truth"):
        replace(value, members=value.members[1:])


def test_future_tle_and_non_time_ordered_partitions_are_forbidden() -> None:
    value = campaign()
    ephemeris = value.members[0].ephemeris_input
    assert ephemeris is not None
    with pytest.raises(CampaignValidationError, match="future TLE"):
        replace(
            value.members[0],
            ephemeris_input=replace(
                ephemeris,
                available_utc_ns=value.members[0].captured_utc_ns + 1,
            ),
        )

    late_train = replace(value.groups[0], stop_utc_ns=350)
    with pytest.raises(CampaignValidationError, match="time ordered"):
        replace(value, groups=(late_train, *value.groups[1:]))


def test_mutable_query_unknown_fields_and_digest_tampering_are_rejected() -> None:
    document = json.loads(encode_campaign(campaign()))
    document["members"][0]["mutable_query"] = "latest FeatureSet"
    with pytest.raises(CampaignValidationError, match="fields differ"):
        decode_campaign(json.dumps(document).encode())

    document = json.loads(encode_campaign(campaign()))
    document["truth_specs"][0]["matrix"]["confounders"] = ["changed"]
    with pytest.raises(CampaignValidationError, match="truth_spec_digest"):
        decode_campaign(json.dumps(document).encode())

    document = json.loads(encode_campaign(campaign()))
    document["membership_digest"] = str(digest("substituted-membership"))
    with pytest.raises(CampaignValidationError, match="membership_digest"):
        decode_campaign(json.dumps(document).encode())


def test_injection_matrix_and_locked_metric_bounds_are_complete() -> None:
    injection = campaign().truth_specs[1]
    with pytest.raises(CampaignValidationError, match="freeze SNR"):
        replace(injection, matrix=replace(injection.matrix, snr_db=None))
    with pytest.raises(CampaignValidationError, match="minimum exceeds"):
        MetricBound("recall", "0.9", "0.8")
    with pytest.raises(CampaignValidationError, match="requires metric bounds"):
        replace(opening(campaign()), metric_bounds=())
