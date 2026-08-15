"""Versioned, leakage-resistant controlled-truth dataset campaigns.

The campaign boundary handles metadata and immutable identities only.  It does
not open recordings or FeatureSets, execute a detector, resolve a query, or
retrieve ephemeris.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, NoReturn

from leo_flow.contracts.core import (
    Digest,
    DigestAlgorithm,
    canonical_digest,
    canonical_json_bytes,
)

from .api import (
    DatasetCandidate,
    DatasetSnapshot,
    DatasetSplit,
    LabelEvidence,
    LabelSource,
    TruthLabel,
    carve_dataset,
)

CAMPAIGN_SCHEMA = "leo-flow.dataset-campaign/v1"
MAX_CAMPAIGN_BYTES = 16 * 1024 * 1024


class CampaignValidationError(ValueError):
    """A campaign is malformed, mutable, circular, or leakage-prone."""


class CampaignTruthKind(str, Enum):
    BASE_NOISE = "base_noise"
    DIGITAL_INJECTION = "digital_injection"
    CONTROLLED_RF = "controlled_rf"
    HARD_NULL = "hard_null"
    CONFOUNDER = "confounder"


class TruthProducerKind(str, Enum):
    INDEPENDENT_GENERATOR = "independent_generator"
    CONTROLLED_INSTRUMENT = "controlled_instrument"
    SCORE_BLIND_SELECTION = "score_blind_selection"
    INDEPENDENT_CHARACTERIZATION = "independent_characterization"


@dataclass(frozen=True)
class CampaignMatrixPoint:
    """One explicit scientific point; decimals use stable strings."""

    snr_db: str | None
    frequency_offset_hz: int | None
    drift_hz_s: str | None
    receiver_delay_samples: tuple[int, ...] | None
    receiver_gain_db: tuple[str, ...] | None
    clipping: bool
    clip_min: int | None
    clip_max: int | None
    null_class: str | None
    confounders: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.snr_db is not None:
            _decimal(self.snr_db, "snr_db")
        if self.drift_hz_s is not None:
            _decimal(self.drift_hz_s, "drift_hz_s")
        if self.frequency_offset_hz is not None and (
            isinstance(self.frequency_offset_hz, bool)
            or not isinstance(self.frequency_offset_hz, int)
        ):
            raise CampaignValidationError("frequency_offset_hz must be an integer")
        if self.receiver_delay_samples is not None and (
            not self.receiver_delay_samples
            or any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in self.receiver_delay_samples
            )
        ):
            raise CampaignValidationError(
                "receiver_delay_samples must be non-empty integers"
            )
        if self.receiver_gain_db is not None:
            if not self.receiver_gain_db:
                raise CampaignValidationError("receiver_gain_db must be non-empty")
            for value in self.receiver_gain_db:
                _decimal(value, "receiver_gain_db")
        if (
            self.receiver_delay_samples is not None
            and self.receiver_gain_db is not None
            and len(self.receiver_delay_samples) != len(self.receiver_gain_db)
        ):
            raise CampaignValidationError(
                "receiver delay and gain vectors must have equal length"
            )
        if (self.clip_min is None) is not (self.clip_max is None):
            raise CampaignValidationError("clipping limits must be a pair")
        if any(
            value is not None
            and (isinstance(value, bool) or not isinstance(value, int))
            for value in (self.clip_min, self.clip_max)
        ):
            raise CampaignValidationError("clipping limits must be integers")
        if self.clipping and (self.clip_min is None or self.clip_max is None):
            raise CampaignValidationError("clipped points require exact limits")
        if not self.clipping and (
            self.clip_min is not None or self.clip_max is not None
        ):
            raise CampaignValidationError("unclipped points cannot carry limits")
        if (
            self.clip_min is not None
            and self.clip_max is not None
            and self.clip_min >= self.clip_max
        ):
            raise CampaignValidationError("clip_min must be below clip_max")
        _optional_token(self.null_class, "null_class")
        _unique_tokens(self.confounders, "confounders", allow_empty=True)


@dataclass(frozen=True)
class CampaignTruthProvenance:
    producer_id: str
    producer_kind: TruthProducerKind
    producer_artifact_digest: Digest
    evidence_digest: Digest
    selection_policy_digest: Digest
    produced_utc_ns: int
    independent_of_method_ids: tuple[str, ...]
    detector_derived: bool

    def __post_init__(self) -> None:
        _token(self.producer_id, "producer_id")
        _unique_tokens(self.independent_of_method_ids, "independent_of_method_ids")
        _non_negative_int(self.produced_utc_ns, "produced_utc_ns")
        if not isinstance(self.detector_derived, bool) or self.detector_derived:
            raise CampaignValidationError("detector-derived labels are forbidden")


@dataclass(frozen=True)
class CampaignTruthSpec:
    truth_spec_id: str
    group_id: str
    kind: CampaignTruthKind
    target_present: bool | None
    base_truth_spec_id: str | None
    provenance: CampaignTruthProvenance
    matrix: CampaignMatrixPoint

    def __post_init__(self) -> None:
        _token(self.truth_spec_id, "truth_spec_id")
        _token(self.group_id, "group_id")
        _optional_token(self.base_truth_spec_id, "base_truth_spec_id")
        if self.target_present is not None and not isinstance(
            self.target_present, bool
        ):
            raise CampaignValidationError("target_present must be boolean or null")
        if self.kind is CampaignTruthKind.BASE_NOISE:
            if self.target_present is not None or self.base_truth_spec_id is not None:
                raise CampaignValidationError(
                    "base noise must remain unlabeled and cannot derive from a spec"
                )
            if (
                self.provenance.producer_kind
                is not TruthProducerKind.SCORE_BLIND_SELECTION
            ):
                raise CampaignValidationError(
                    "base noise selection must be score blind"
                )
        elif self.target_present is None:
            raise CampaignValidationError("controlled truth must state target presence")
        if self.kind is CampaignTruthKind.DIGITAL_INJECTION:
            if self.target_present is not True or self.base_truth_spec_id is None:
                raise CampaignValidationError(
                    "digital injection must be positive and pin its base truth spec"
                )
            if (
                self.provenance.producer_kind
                is not TruthProducerKind.INDEPENDENT_GENERATOR
            ):
                raise CampaignValidationError(
                    "digital injection requires an independent generator"
                )
            if any(
                value is None
                for value in (
                    self.matrix.snr_db,
                    self.matrix.frequency_offset_hz,
                    self.matrix.drift_hz_s,
                    self.matrix.receiver_delay_samples,
                    self.matrix.receiver_gain_db,
                )
            ):
                raise CampaignValidationError(
                    "digital injection matrix must freeze SNR, offset, drift, delay, and gain"
                )
            if self.matrix.null_class is not None:
                raise CampaignValidationError("digital injection cannot be a null")
        elif self.base_truth_spec_id is not None:
            raise CampaignValidationError(
                "only digital injections may name a base truth spec"
            )
        if self.kind is CampaignTruthKind.CONTROLLED_RF and (
            self.target_present is not True
            or self.provenance.producer_kind
            is not TruthProducerKind.CONTROLLED_INSTRUMENT
        ):
            raise CampaignValidationError(
                "controlled RF truth must be positive instrument truth"
            )
        if self.kind is CampaignTruthKind.HARD_NULL and (
            self.target_present is not False
            or self.matrix.null_class is None
            or self.provenance.producer_kind
            is not TruthProducerKind.SCORE_BLIND_SELECTION
        ):
            raise CampaignValidationError(
                "hard nulls require score-blind negative truth and a null class"
            )
        if self.kind is CampaignTruthKind.CONFOUNDER and (
            not self.matrix.confounders
            or self.provenance.producer_kind
            is not TruthProducerKind.INDEPENDENT_CHARACTERIZATION
        ):
            raise CampaignValidationError(
                "confounders require independent characterization and named confounders"
            )

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class CampaignGroup:
    group_id: str
    partition: DatasetSplit
    start_utc_ns: int
    stop_utc_ns: int
    base_truth_spec_id: str
    injection_truth_spec_ids: tuple[str, ...]
    truth_spec_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _token(self.group_id, "group_id")
        _token(self.base_truth_spec_id, "base_truth_spec_id")
        _unique_tokens(
            self.injection_truth_spec_ids,
            "injection_truth_spec_ids",
            allow_empty=True,
        )
        _unique_tokens(self.truth_spec_ids, "truth_spec_ids")
        _non_negative_int(self.start_utc_ns, "start_utc_ns")
        _non_negative_int(self.stop_utc_ns, "stop_utc_ns")
        if self.stop_utc_ns <= self.start_utc_ns:
            raise CampaignValidationError("campaign group interval must be non-empty")
        if self.base_truth_spec_id not in self.truth_spec_ids:
            raise CampaignValidationError("group omits its base truth spec")
        if not set(self.injection_truth_spec_ids).issubset(self.truth_spec_ids):
            raise CampaignValidationError("group omits an injection truth spec")


@dataclass(frozen=True)
class CampaignEphemerisInput:
    snapshot_digest: Digest
    provenance_digest: Digest
    selection_policy_digest: Digest
    selection_policy: str
    available_utc_ns: int

    def __post_init__(self) -> None:
        if self.selection_policy != "available_then":
            raise CampaignValidationError(
                "campaign ephemeris must use immutable available_then selection"
            )
        _non_negative_int(self.available_utc_ns, "available_utc_ns")


@dataclass(frozen=True)
class CampaignMember:
    truth_spec_id: str
    group_id: str
    recording_id: str
    recording_identity_digest: Digest
    feature_set_id: str
    feature_set_digest: Digest
    captured_utc_ns: int
    radio_id: str
    lnb_ids: tuple[str, ...]
    observation_mode: str
    sample_rate_hz: int
    gain_mode: str
    gain_db: str | None
    scored_truth: bool
    derived_from_recording_id: str | None
    ephemeris_input: CampaignEphemerisInput | None

    def __post_init__(self) -> None:
        for name in (
            "truth_spec_id",
            "group_id",
            "recording_id",
            "feature_set_id",
            "radio_id",
            "observation_mode",
            "gain_mode",
        ):
            _token(str(getattr(self, name)), name)
        _optional_token(self.gain_db, "gain_db")
        _optional_token(self.derived_from_recording_id, "derived_from_recording_id")
        _unique_tokens(self.lnb_ids, "lnb_ids")
        _non_negative_int(self.captured_utc_ns, "captured_utc_ns")
        if (
            isinstance(self.sample_rate_hz, bool)
            or not isinstance(self.sample_rate_hz, int)
            or self.sample_rate_hz <= 0
        ):
            raise CampaignValidationError("sample_rate_hz must be positive")
        if not isinstance(self.scored_truth, bool):
            raise CampaignValidationError("scored_truth must be boolean")
        if (
            self.ephemeris_input is not None
            and self.ephemeris_input.available_utc_ns > self.captured_utc_ns
        ):
            raise CampaignValidationError(
                "future TLE availability cannot enter a campaign member"
            )

    @property
    def identity(self) -> object:
        return {
            "truth_spec_id": self.truth_spec_id,
            "group_id": self.group_id,
            "recording_id": self.recording_id,
            "recording_identity_digest": str(self.recording_identity_digest),
            "feature_set_id": self.feature_set_id,
            "feature_set_digest": str(self.feature_set_digest),
            "captured_utc_ns": self.captured_utc_ns,
            "radio_id": self.radio_id,
            "lnb_ids": self.lnb_ids,
            "observation_mode": self.observation_mode,
            "sample_rate_hz": self.sample_rate_hz,
            "gain_mode": self.gain_mode,
            "gain_db": self.gain_db,
            "scored_truth": self.scored_truth,
            "derived_from_recording_id": self.derived_from_recording_id,
            "ephemeris_input": self.ephemeris_input,
        }


@dataclass(frozen=True)
class CampaignManifest:
    campaign_id: str
    frozen_utc_ns: int
    candidate_method_ids: tuple[str, ...]
    truth_specs: tuple[CampaignTruthSpec, ...]
    groups: tuple[CampaignGroup, ...]
    members: tuple[CampaignMember, ...]

    def __post_init__(self) -> None:
        _token(self.campaign_id, "campaign_id")
        _non_negative_int(self.frozen_utc_ns, "frozen_utc_ns")
        _unique_tokens(self.candidate_method_ids, "candidate_method_ids")
        validate_campaign(self)

    @property
    def truth_specs_digest(self) -> Digest:
        return canonical_digest(
            tuple((spec.truth_spec_id, str(spec.digest)) for spec in self.truth_specs)
        )

    @property
    def membership_digest(self) -> Digest:
        return canonical_digest(tuple(member.identity for member in self.members))

    @property
    def digest(self) -> Digest:
        return canonical_digest(
            {
                "schema": CAMPAIGN_SCHEMA,
                "campaign_id": self.campaign_id,
                "frozen_utc_ns": self.frozen_utc_ns,
                "candidate_method_ids": self.candidate_method_ids,
                "truth_specs_digest": str(self.truth_specs_digest),
                "membership_digest": str(self.membership_digest),
                "groups": self.groups,
            }
        )


@dataclass(frozen=True)
class MetricBound:
    metric_id: str
    minimum: str | None
    maximum: str | None

    def __post_init__(self) -> None:
        _token(self.metric_id, "metric_id")
        if self.minimum is None and self.maximum is None:
            raise CampaignValidationError("metric bound must have a minimum or maximum")
        minimum = (
            _decimal(self.minimum, "minimum") if self.minimum is not None else None
        )
        maximum = (
            _decimal(self.maximum, "maximum") if self.maximum is not None else None
        )
        if minimum is not None and maximum is not None and minimum > maximum:
            raise CampaignValidationError("metric minimum exceeds maximum")


@dataclass(frozen=True)
class LockedTestOpening:
    campaign_digest: Digest
    method_id: str
    method_artifact_digest: Digest
    config_digest: Digest
    metrics_spec_digest: Digest
    metric_bounds: tuple[MetricBound, ...]
    frozen_utc_ns: int
    opened_utc_ns: int
    steward_id: str

    def __post_init__(self) -> None:
        _token(self.method_id, "method_id")
        _token(self.steward_id, "steward_id")
        _non_negative_int(self.frozen_utc_ns, "frozen_utc_ns")
        _non_negative_int(self.opened_utc_ns, "opened_utc_ns")
        if self.frozen_utc_ns > self.opened_utc_ns:
            raise CampaignValidationError("locked-test opening precedes its freeze")
        if not self.metric_bounds:
            raise CampaignValidationError("locked-test opening requires metric bounds")
        metric_ids = tuple(bound.metric_id for bound in self.metric_bounds)
        if len(metric_ids) != len(set(metric_ids)):
            raise CampaignValidationError("locked-test metric bounds must be unique")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class CampaignMaterialization:
    manifest: CampaignManifest
    carved: DatasetSnapshot
    ordered_members: tuple[CampaignMember, ...]

    def __post_init__(self) -> None:
        expected_ids = tuple(row[0] for row in self.carved.ordered_members)
        if (
            tuple(member.feature_set_id for member in self.ordered_members)
            != expected_ids
        ):
            raise CampaignValidationError(
                "campaign materialization and carved dataset order differ"
            )

    def members_in(
        self,
        split: DatasetSplit,
        *,
        opening: LockedTestOpening | None = None,
    ) -> tuple[CampaignMember, ...]:
        if split is DatasetSplit.LOCKED_TEST:
            _validate_opening(self.manifest, self.carved.evaluated_method_id, opening)
        group_by_id = {group.group_id: group for group in self.manifest.groups}
        return tuple(
            member
            for member in self.ordered_members
            if group_by_id[member.group_id].partition is split
        )


def validate_campaign(campaign: CampaignManifest) -> None:
    """Validate exact truth, grouping, identity, and temporal closure."""

    _unique((spec.truth_spec_id for spec in campaign.truth_specs), "truth spec ID")
    _unique((str(spec.digest) for spec in campaign.truth_specs), "truth spec digest")
    _unique((group.group_id for group in campaign.groups), "campaign group ID")
    for name, values in (
        ("recording ID", (member.recording_id for member in campaign.members)),
        (
            "recording identity digest",
            (str(member.recording_identity_digest) for member in campaign.members),
        ),
        ("FeatureSet ID", (member.feature_set_id for member in campaign.members)),
        (
            "FeatureSet digest",
            (str(member.feature_set_digest) for member in campaign.members),
        ),
    ):
        _unique(values, name)

    methods = set(campaign.candidate_method_ids)
    specs = {spec.truth_spec_id: spec for spec in campaign.truth_specs}
    groups = {group.group_id: group for group in campaign.groups}
    members_by_spec = {member.truth_spec_id: member for member in campaign.members}
    if len(members_by_spec) != len(campaign.members):
        raise CampaignValidationError("truth specs must materialize exactly once")
    if set(members_by_spec) != set(specs):
        raise CampaignValidationError(
            "campaign must materialize every and only frozen truth spec"
        )
    split_order = {
        DatasetSplit.TRAIN: 0,
        DatasetSplit.VALIDATION: 1,
        DatasetSplit.LOCKED_TEST: 2,
    }
    ordered_members = tuple(
        sorted(
            campaign.members,
            key=lambda member: (
                split_order[groups[member.group_id].partition]
                if member.group_id in groups
                else 3,
                member.captured_utc_ns,
                member.feature_set_id,
            ),
        )
    )
    if campaign.members != ordered_members:
        raise CampaignValidationError(
            "campaign members must be partition- and time-ordered"
        )
    for spec in campaign.truth_specs:
        if set(spec.provenance.independent_of_method_ids) != methods:
            raise CampaignValidationError(
                "truth provenance must declare independence from every candidate method"
            )
        group = groups.get(spec.group_id)
        member = members_by_spec[spec.truth_spec_id]
        if group is None or member.group_id != spec.group_id:
            raise CampaignValidationError("truth, member, and group identity differ")
        if not group.start_utc_ns <= member.captured_utc_ns < group.stop_utc_ns:
            raise CampaignValidationError(
                "member capture lies outside its frozen group"
            )
        if spec.kind is CampaignTruthKind.BASE_NOISE and member.scored_truth:
            raise CampaignValidationError(
                "base noise cannot silently become scored truth"
            )
        if member.scored_truth and spec.target_present is None:
            raise CampaignValidationError("scored member lacks controlled truth")
    for group in campaign.groups:
        group_specs = {
            spec.truth_spec_id
            for spec in campaign.truth_specs
            if spec.group_id == group.group_id
        }
        if set(group.truth_spec_ids) != group_specs:
            raise CampaignValidationError("group truth-spec closure is not exact")
        base = specs.get(group.base_truth_spec_id)
        if base is None or base.kind is not CampaignTruthKind.BASE_NOISE:
            raise CampaignValidationError("group base must be an exact base-noise spec")
        injections = {
            spec.truth_spec_id
            for spec in campaign.truth_specs
            if spec.group_id == group.group_id
            and spec.kind is CampaignTruthKind.DIGITAL_INJECTION
        }
        if set(group.injection_truth_spec_ids) != injections:
            raise CampaignValidationError(
                "group must enumerate every and only digital injection"
            )
        base_recording = members_by_spec[group.base_truth_spec_id].recording_id
        for injection_id in injections:
            spec = specs[injection_id]
            member = members_by_spec[injection_id]
            if spec.base_truth_spec_id != group.base_truth_spec_id:
                raise CampaignValidationError(
                    "injection pins the wrong base truth spec"
                )
            if member.derived_from_recording_id != base_recording:
                raise CampaignValidationError(
                    "injection result must derive from its group's base recording"
                )
        for spec_id in group_specs - injections:
            if members_by_spec[spec_id].derived_from_recording_id is not None:
                raise CampaignValidationError(
                    "only digital injection results may derive from a recording"
                )

    partitions = {
        split: [group for group in campaign.groups if group.partition is split]
        for split in DatasetSplit
    }
    if any(not values for values in partitions.values()):
        raise CampaignValidationError(
            "campaign requires train, validation, and locked-test groups"
        )
    if not (
        max(group.stop_utc_ns for group in partitions[DatasetSplit.TRAIN])
        <= min(group.start_utc_ns for group in partitions[DatasetSplit.VALIDATION])
        and max(group.stop_utc_ns for group in partitions[DatasetSplit.VALIDATION])
        <= min(group.start_utc_ns for group in partitions[DatasetSplit.LOCKED_TEST])
    ):
        raise CampaignValidationError(
            "campaign partitions must be whole-group and time ordered"
        )
    if campaign.frozen_utc_ns < max(
        member.captured_utc_ns for member in campaign.members
    ):
        raise CampaignValidationError(
            "campaign freeze must follow all materialized result identities"
        )


def materialize_campaign(
    campaign: CampaignManifest, *, evaluated_method_id: str
) -> CampaignMaterialization:
    """Freeze exact Recording/FeatureSet identities through the dataset carve."""

    specs = {spec.truth_spec_id: spec for spec in campaign.truth_specs}
    member_by_spec = {member.truth_spec_id: member for member in campaign.members}
    if evaluated_method_id not in campaign.candidate_method_ids:
        raise CampaignValidationError(
            "materialized method is outside the frozen campaign"
        )
    base_recording_digests = {
        spec.truth_spec_id: member_by_spec[
            spec.base_truth_spec_id
        ].recording_identity_digest
        for spec in campaign.truth_specs
        if spec.base_truth_spec_id is not None
    }
    candidates = tuple(
        _candidate(
            member,
            specs[member.truth_spec_id],
            base_recording_digests.get(member.truth_spec_id),
        )
        for member in campaign.members
    )
    carved = carve_dataset(
        candidates,
        group_partitions={group.group_id: group.partition for group in campaign.groups},
        evaluated_method_id=evaluated_method_id,
    )
    member_by_feature = {member.feature_set_id: member for member in campaign.members}
    ordered = tuple(member_by_feature[row[0]] for row in carved.ordered_members)
    # The richer digest freezes recording identities and truth-spec references;
    # the carve independently freezes the ordered FeatureSet partition membership.
    if campaign.membership_digest != canonical_digest(
        tuple(member.identity for member in campaign.members)
    ):
        raise CampaignValidationError("campaign membership digest changed")
    return CampaignMaterialization(campaign, carved, ordered)


def encode_campaign(campaign: CampaignManifest) -> bytes:
    return canonical_json_bytes(_campaign_document(campaign))


def decode_campaign(data: bytes) -> CampaignManifest:
    if len(data) > MAX_CAMPAIGN_BYTES:
        raise CampaignValidationError("campaign document exceeds size limit")
    try:
        value = json.loads(data, object_pairs_hook=_unique_object)
        root = _object(value, "root")
        campaign = _parse_campaign(root)
        expected_truth = _digest(root["truth_specs_digest"])
        expected_membership = _digest(root["membership_digest"])
        if campaign.truth_specs_digest != expected_truth:
            _bad("truth_specs_digest does not match frozen truth specs")
        if campaign.membership_digest != expected_membership:
            _bad("membership_digest does not match frozen members")
        return campaign
    except CampaignValidationError:
        raise
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise CampaignValidationError(
            f"malformed campaign document: {error}"
        ) from error


def _candidate(
    member: CampaignMember,
    spec: CampaignTruthSpec,
    base_recording_digest: Digest | None,
) -> DatasetCandidate:
    source = {
        CampaignTruthKind.BASE_NOISE: LabelSource.UNLABELED,
        CampaignTruthKind.DIGITAL_INJECTION: LabelSource.INJECTED,
        CampaignTruthKind.CONTROLLED_RF: LabelSource.OBSERVED,
        CampaignTruthKind.HARD_NULL: LabelSource.OBSERVED,
        CampaignTruthKind.CONFOUNDER: LabelSource.OBSERVED,
    }[spec.kind]
    evidence = LabelEvidence(
        source=source,
        evidence_digest=spec.provenance.evidence_digest,
        producer_id=spec.provenance.producer_id,
        produced_utc_ns=spec.provenance.produced_utc_ns,
        independent_of_method_ids=spec.provenance.independent_of_method_ids,
        uncertainty=(),
        base_recording_digest=(
            base_recording_digest
            if spec.kind is CampaignTruthKind.DIGITAL_INJECTION
            else None
        ),
        injection_spec_digest=(
            spec.digest if spec.kind is CampaignTruthKind.DIGITAL_INJECTION else None
        ),
    )
    return DatasetCandidate(
        feature_set_id=member.feature_set_id,
        feature_set_digest=member.feature_set_digest,
        recording_id=member.recording_id,
        split_group_id=member.group_id,
        captured_utc_ns=member.captured_utc_ns,
        radio_id=member.radio_id,
        lnb_ids=member.lnb_ids,
        observation_mode=member.observation_mode,
        sample_rate_hz=member.sample_rate_hz,
        gain_mode=member.gain_mode,
        gain_db=member.gain_db,
        satellite_id=None,
        truth=TruthLabel(
            target_present=spec.target_present,
            source=source,
            evidence=(evidence,),
            confidence=None if spec.target_present is None else 1.0,
        ),
        scored_truth=member.scored_truth,
        derived_from_recording_id=member.derived_from_recording_id,
    )


def _validate_opening(
    campaign: CampaignManifest,
    evaluated_method_id: str,
    opening: LockedTestOpening | None,
) -> None:
    if opening is None:
        raise CampaignValidationError("locked test remains sealed")
    if opening.campaign_digest != campaign.digest:
        raise CampaignValidationError("opening receipt names another campaign")
    if opening.method_id != evaluated_method_id:
        raise CampaignValidationError(
            "opening method differs from the materialized method"
        )
    if opening.frozen_utc_ns < campaign.frozen_utc_ns:
        raise CampaignValidationError(
            "method/config/metric bounds were frozen before campaign membership"
        )


def _campaign_document(campaign: CampaignManifest) -> object:
    return {
        "schema": CAMPAIGN_SCHEMA,
        "campaign_id": campaign.campaign_id,
        "frozen_utc_ns": campaign.frozen_utc_ns,
        "candidate_method_ids": list(campaign.candidate_method_ids),
        "truth_specs_digest": str(campaign.truth_specs_digest),
        "membership_digest": str(campaign.membership_digest),
        "truth_specs": [_truth_document(spec) for spec in campaign.truth_specs],
        "groups": [_group_document(group) for group in campaign.groups],
        "members": [_member_document(member) for member in campaign.members],
    }


def _truth_document(spec: CampaignTruthSpec) -> object:
    provenance = spec.provenance
    matrix = spec.matrix
    return {
        "truth_spec_id": spec.truth_spec_id,
        "truth_spec_digest": str(spec.digest),
        "group_id": spec.group_id,
        "kind": spec.kind.value,
        "target_present": spec.target_present,
        "base_truth_spec_id": spec.base_truth_spec_id,
        "provenance": {
            "producer_id": provenance.producer_id,
            "producer_kind": provenance.producer_kind.value,
            "producer_artifact_digest": str(provenance.producer_artifact_digest),
            "evidence_digest": str(provenance.evidence_digest),
            "selection_policy_digest": str(provenance.selection_policy_digest),
            "produced_utc_ns": provenance.produced_utc_ns,
            "independent_of_method_ids": list(provenance.independent_of_method_ids),
            "detector_derived": provenance.detector_derived,
        },
        "matrix": {
            "snr_db": matrix.snr_db,
            "frequency_offset_hz": matrix.frequency_offset_hz,
            "drift_hz_s": matrix.drift_hz_s,
            "receiver_delay_samples": (
                list(matrix.receiver_delay_samples)
                if matrix.receiver_delay_samples is not None
                else None
            ),
            "receiver_gain_db": (
                list(matrix.receiver_gain_db)
                if matrix.receiver_gain_db is not None
                else None
            ),
            "clipping": matrix.clipping,
            "clip_min": matrix.clip_min,
            "clip_max": matrix.clip_max,
            "null_class": matrix.null_class,
            "confounders": list(matrix.confounders),
        },
    }


def _group_document(group: CampaignGroup) -> object:
    return {
        "group_id": group.group_id,
        "partition": group.partition.value,
        "start_utc_ns": group.start_utc_ns,
        "stop_utc_ns": group.stop_utc_ns,
        "base_truth_spec_id": group.base_truth_spec_id,
        "injection_truth_spec_ids": list(group.injection_truth_spec_ids),
        "truth_spec_ids": list(group.truth_spec_ids),
    }


def _member_document(member: CampaignMember) -> object:
    ephemeris = member.ephemeris_input
    return {
        "truth_spec_id": member.truth_spec_id,
        "group_id": member.group_id,
        "recording_id": member.recording_id,
        "recording_identity_digest": str(member.recording_identity_digest),
        "feature_set_id": member.feature_set_id,
        "feature_set_digest": str(member.feature_set_digest),
        "captured_utc_ns": member.captured_utc_ns,
        "radio_id": member.radio_id,
        "lnb_ids": list(member.lnb_ids),
        "observation_mode": member.observation_mode,
        "sample_rate_hz": member.sample_rate_hz,
        "gain_mode": member.gain_mode,
        "gain_db": member.gain_db,
        "scored_truth": member.scored_truth,
        "derived_from_recording_id": member.derived_from_recording_id,
        "ephemeris_input": (
            {
                "snapshot_digest": str(ephemeris.snapshot_digest),
                "provenance_digest": str(ephemeris.provenance_digest),
                "selection_policy_digest": str(ephemeris.selection_policy_digest),
                "selection_policy": ephemeris.selection_policy,
                "available_utc_ns": ephemeris.available_utc_ns,
            }
            if ephemeris is not None
            else None
        ),
    }


def _parse_campaign(root: Mapping[str, Any]) -> CampaignManifest:
    _keys(
        root,
        {
            "schema",
            "campaign_id",
            "frozen_utc_ns",
            "candidate_method_ids",
            "truth_specs_digest",
            "membership_digest",
            "truth_specs",
            "groups",
            "members",
        },
        "root",
    )
    if root["schema"] != CAMPAIGN_SCHEMA:
        _bad("unsupported campaign schema")
    truths = tuple(
        _parse_truth(_object(value, f"truth_specs[{index}]"), index)
        for index, value in enumerate(_array(root["truth_specs"], "truth_specs"))
    )
    groups = tuple(
        _parse_group(_object(value, f"groups[{index}]"), index)
        for index, value in enumerate(_array(root["groups"], "groups"))
    )
    members = tuple(
        _parse_member(_object(value, f"members[{index}]"), index)
        for index, value in enumerate(_array(root["members"], "members"))
    )
    campaign = CampaignManifest(
        campaign_id=_string(root["campaign_id"], "campaign_id"),
        frozen_utc_ns=_integer(root["frozen_utc_ns"], "frozen_utc_ns"),
        candidate_method_ids=_strings(
            root["candidate_method_ids"], "candidate_method_ids"
        ),
        truth_specs=truths,
        groups=groups,
        members=members,
    )
    for index, (raw, spec) in enumerate(
        zip(_array(root["truth_specs"], "truth_specs"), truths, strict=True)
    ):
        raw_spec = _object(raw, f"truth_specs[{index}]")
        if _digest(raw_spec["truth_spec_digest"]) != spec.digest:
            _bad(f"truth_specs[{index}].truth_spec_digest differs")
    return campaign


def _parse_truth(value: Mapping[str, Any], index: int) -> CampaignTruthSpec:
    name = f"truth_specs[{index}]"
    _keys(
        value,
        {
            "truth_spec_id",
            "truth_spec_digest",
            "group_id",
            "kind",
            "target_present",
            "base_truth_spec_id",
            "provenance",
            "matrix",
        },
        name,
    )
    provenance = _object(value["provenance"], f"{name}.provenance")
    _keys(
        provenance,
        {
            "producer_id",
            "producer_kind",
            "producer_artifact_digest",
            "evidence_digest",
            "selection_policy_digest",
            "produced_utc_ns",
            "independent_of_method_ids",
            "detector_derived",
        },
        f"{name}.provenance",
    )
    matrix = _object(value["matrix"], f"{name}.matrix")
    _keys(
        matrix,
        {
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
        },
        f"{name}.matrix",
    )
    target = value["target_present"]
    if target is not None and not isinstance(target, bool):
        _bad(f"{name}.target_present must be boolean or null")
    detector_derived = provenance["detector_derived"]
    clipping = matrix["clipping"]
    if not isinstance(detector_derived, bool) or not isinstance(clipping, bool):
        _bad(f"{name}: boolean field is malformed")
    return CampaignTruthSpec(
        truth_spec_id=_string(value["truth_spec_id"], "truth_spec_id"),
        group_id=_string(value["group_id"], "group_id"),
        kind=CampaignTruthKind(_string(value["kind"], "kind")),
        target_present=target,
        base_truth_spec_id=_optional_string(
            value["base_truth_spec_id"], "base_truth_spec_id"
        ),
        provenance=CampaignTruthProvenance(
            producer_id=_string(provenance["producer_id"], "producer_id"),
            producer_kind=TruthProducerKind(
                _string(provenance["producer_kind"], "producer_kind")
            ),
            producer_artifact_digest=_digest(provenance["producer_artifact_digest"]),
            evidence_digest=_digest(provenance["evidence_digest"]),
            selection_policy_digest=_digest(provenance["selection_policy_digest"]),
            produced_utc_ns=_integer(provenance["produced_utc_ns"], "produced_utc_ns"),
            independent_of_method_ids=_strings(
                provenance["independent_of_method_ids"], "independent_of_method_ids"
            ),
            detector_derived=detector_derived,
        ),
        matrix=CampaignMatrixPoint(
            snr_db=_optional_string(matrix["snr_db"], "snr_db"),
            frequency_offset_hz=_optional_integer(
                matrix["frequency_offset_hz"], "frequency_offset_hz"
            ),
            drift_hz_s=_optional_string(matrix["drift_hz_s"], "drift_hz_s"),
            receiver_delay_samples=_optional_integers(
                matrix["receiver_delay_samples"], "receiver_delay_samples"
            ),
            receiver_gain_db=_optional_strings(
                matrix["receiver_gain_db"], "receiver_gain_db"
            ),
            clipping=clipping,
            clip_min=_optional_integer(matrix["clip_min"], "clip_min"),
            clip_max=_optional_integer(matrix["clip_max"], "clip_max"),
            null_class=_optional_string(matrix["null_class"], "null_class"),
            confounders=_strings(
                matrix["confounders"], "confounders", allow_empty=True
            ),
        ),
    )


def _parse_group(value: Mapping[str, Any], index: int) -> CampaignGroup:
    name = f"groups[{index}]"
    _keys(
        value,
        {
            "group_id",
            "partition",
            "start_utc_ns",
            "stop_utc_ns",
            "base_truth_spec_id",
            "injection_truth_spec_ids",
            "truth_spec_ids",
        },
        name,
    )
    return CampaignGroup(
        group_id=_string(value["group_id"], "group_id"),
        partition=DatasetSplit(_string(value["partition"], "partition")),
        start_utc_ns=_integer(value["start_utc_ns"], "start_utc_ns"),
        stop_utc_ns=_integer(value["stop_utc_ns"], "stop_utc_ns"),
        base_truth_spec_id=_string(value["base_truth_spec_id"], "base_truth_spec_id"),
        injection_truth_spec_ids=_strings(
            value["injection_truth_spec_ids"],
            "injection_truth_spec_ids",
            allow_empty=True,
        ),
        truth_spec_ids=_strings(value["truth_spec_ids"], "truth_spec_ids"),
    )


def _parse_member(value: Mapping[str, Any], index: int) -> CampaignMember:
    name = f"members[{index}]"
    _keys(
        value,
        {
            "truth_spec_id",
            "group_id",
            "recording_id",
            "recording_identity_digest",
            "feature_set_id",
            "feature_set_digest",
            "captured_utc_ns",
            "radio_id",
            "lnb_ids",
            "observation_mode",
            "sample_rate_hz",
            "gain_mode",
            "gain_db",
            "scored_truth",
            "derived_from_recording_id",
            "ephemeris_input",
        },
        name,
    )
    scored = value["scored_truth"]
    if not isinstance(scored, bool):
        _bad(f"{name}.scored_truth must be boolean")
    raw_ephemeris = value["ephemeris_input"]
    ephemeris: CampaignEphemerisInput | None = None
    if raw_ephemeris is not None:
        item = _object(raw_ephemeris, f"{name}.ephemeris_input")
        _keys(
            item,
            {
                "snapshot_digest",
                "provenance_digest",
                "selection_policy_digest",
                "selection_policy",
                "available_utc_ns",
            },
            f"{name}.ephemeris_input",
        )
        ephemeris = CampaignEphemerisInput(
            snapshot_digest=_digest(item["snapshot_digest"]),
            provenance_digest=_digest(item["provenance_digest"]),
            selection_policy_digest=_digest(item["selection_policy_digest"]),
            selection_policy=_string(item["selection_policy"], "selection_policy"),
            available_utc_ns=_integer(item["available_utc_ns"], "available_utc_ns"),
        )
    return CampaignMember(
        truth_spec_id=_string(value["truth_spec_id"], "truth_spec_id"),
        group_id=_string(value["group_id"], "group_id"),
        recording_id=_string(value["recording_id"], "recording_id"),
        recording_identity_digest=_digest(value["recording_identity_digest"]),
        feature_set_id=_string(value["feature_set_id"], "feature_set_id"),
        feature_set_digest=_digest(value["feature_set_digest"]),
        captured_utc_ns=_integer(value["captured_utc_ns"], "captured_utc_ns"),
        radio_id=_string(value["radio_id"], "radio_id"),
        lnb_ids=_strings(value["lnb_ids"], "lnb_ids"),
        observation_mode=_string(value["observation_mode"], "observation_mode"),
        sample_rate_hz=_integer(value["sample_rate_hz"], "sample_rate_hz"),
        gain_mode=_string(value["gain_mode"], "gain_mode"),
        gain_db=_optional_string(value["gain_db"], "gain_db"),
        scored_truth=scored,
        derived_from_recording_id=_optional_string(
            value["derived_from_recording_id"], "derived_from_recording_id"
        ),
        ephemeris_input=ephemeris,
    )


def _decimal(value: str, name: str) -> Decimal:
    if not isinstance(value, str):
        raise CampaignValidationError(f"{name} must be a finite decimal string")
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError) as error:
        raise CampaignValidationError(
            f"{name} must be a finite decimal string"
        ) from error
    if not number.is_finite():
        raise CampaignValidationError(f"{name} must be a finite decimal string")
    return number


def _token(value: str, name: str) -> None:
    if not value or any(character.isspace() for character in value):
        raise CampaignValidationError(f"{name} must be a token")


def _optional_token(value: str | None, name: str) -> None:
    if value is not None:
        _token(value, name)


def _unique_tokens(
    values: Sequence[str], name: str, *, allow_empty: bool = False
) -> None:
    if not values and not allow_empty:
        raise CampaignValidationError(f"{name} must be non-empty")
    for value in values:
        _token(value, name)
    if len(values) != len(set(values)):
        raise CampaignValidationError(f"{name} must be unique")


def _unique(values: Any, name: str) -> None:
    materialized = tuple(values)
    if not materialized or len(materialized) != len(set(materialized)):
        raise CampaignValidationError(f"{name} values must be non-empty and unique")


def _non_negative_int(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CampaignValidationError(f"{name} must be a non-negative integer")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _bad(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        _bad(f"{name} must be an object")
    return value


def _array(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        _bad(f"{name} must be an array")
    return value


def _keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        _bad(f"{name} fields differ from campaign v1")


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str):
        _bad(f"{name} must be a string")
    return value


def _optional_string(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _string(value, name)


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _bad(f"{name} must be an integer")
    return value


def _optional_integer(value: Any, name: str) -> int | None:
    if value is None:
        return None
    return _integer(value, name)


def _strings(value: Any, name: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    result = tuple(_string(item, name) for item in _array(value, name))
    if not result and not allow_empty:
        _bad(f"{name} must be non-empty")
    return result


def _optional_strings(value: Any, name: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    return _strings(value, name)


def _optional_integers(value: Any, name: str) -> tuple[int, ...] | None:
    if value is None:
        return None
    return tuple(_integer(item, name) for item in _array(value, name))


def _digest(value: Any) -> Digest:
    text = _string(value, "digest")
    algorithm, separator, digest_value = text.partition(":")
    if separator != ":" or algorithm != DigestAlgorithm.SHA256.value:
        _bad("digest must be sha256:<lowercase-hex>")
    return Digest(DigestAlgorithm.SHA256, digest_value)


def _bad(message: str) -> NoReturn:
    raise CampaignValidationError(message)
