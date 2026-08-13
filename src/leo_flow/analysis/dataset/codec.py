"""Deterministic JSON codec for durable dataset snapshot bundles."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, NoReturn, cast

from leo_flow.contracts.core import (
    AnalysisRunId,
    DatasetSnapshotId,
    Digest,
    DigestAlgorithm,
    FeatureSetId,
    SchemaRef,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.features import FeatureSetRef
from leo_flow.contracts.model import FeatureDatasetSnapshot
from leo_flow.contracts.storage import ObjectRef

from .api import DatasetSplit, LabelEvidence, LabelSource, TruthLabel
from .snapshot import DatasetMember, DatasetRole, DatasetSnapshotBundle

MAX_DATASET_SNAPSHOT_BYTES = 16 * 1024 * 1024


class MalformedDatasetSnapshotError(ValueError):
    """Serialized dataset data is invalid, ambiguous, or fails its digest."""


def encode_dataset_snapshot(snapshot: DatasetSnapshotBundle) -> bytes:
    """Encode a canonical document; equal bundles always produce equal bytes."""

    feature = snapshot.feature_dataset
    return canonical_json_bytes(
        {
            "schema": snapshot.SCHEMA_ID,
            "version": "0.1",
            "feature_dataset": {
                "schema": feature.SCHEMA_ID,
                "version": "0.1",
                "snapshot_id": str(feature.snapshot_id),
                "selection_spec": feature.selection_spec,
                "selection_cutoff_utc_ns": feature.selection_cutoff_utc_ns,
                "membership_digest": str(feature.membership_digest),
            },
            "evaluated_method_id": snapshot.evaluated_method_id,
            "members": [_member_document(member) for member in snapshot.members],
            "snapshot_digest": str(snapshot.snapshot_digest),
            "promoted": snapshot.promoted,
            "promotion_warnings": list(snapshot.promotion_warnings),
        }
    )


def decode_dataset_snapshot(data: bytes) -> DatasetSnapshotBundle:
    """Decode with strict keys, duplicate-key rejection, and digest verification."""

    if len(data) > MAX_DATASET_SNAPSHOT_BYTES:
        raise MalformedDatasetSnapshotError("dataset snapshot exceeds size limit")
    try:
        document = json.loads(data, object_pairs_hook=_unique_object)
        if canonical_json_bytes(document) != data:
            _bad("dataset snapshot bytes are not canonical JSON")
        root = _object(document, "root")
        _keys(
            root,
            {
                "schema",
                "version",
                "feature_dataset",
                "evaluated_method_id",
                "members",
                "snapshot_digest",
                "promoted",
                "promotion_warnings",
            },
            "root",
        )
        if (
            root["schema"] != DatasetSnapshotBundle.SCHEMA_ID
            or root["version"] != "0.1"
        ):
            _bad("unsupported durable dataset snapshot schema")
        feature_doc = _object(root["feature_dataset"], "feature_dataset")
        _keys(
            feature_doc,
            {
                "schema",
                "version",
                "snapshot_id",
                "selection_spec",
                "selection_cutoff_utc_ns",
                "membership_digest",
            },
            "feature_dataset",
        )
        if (
            feature_doc["schema"] != FeatureDatasetSnapshot.SCHEMA_ID
            or feature_doc["version"] != "0.1"
        ):
            _bad("unsupported feature dataset snapshot schema")
        raw_members = _array(root["members"], "members")
        members = tuple(
            _parse_member(value, index) for index, value in enumerate(raw_members)
        )
        feature_dataset = FeatureDatasetSnapshot(
            schema=SchemaRef(FeatureDatasetSnapshot.SCHEMA_ID),
            snapshot_id=DatasetSnapshotId(
                _string(feature_doc["snapshot_id"], "snapshot_id")
            ),
            ordered_feature_set_refs=tuple(item.feature_set_ref for item in members),
            selection_spec=_string(feature_doc["selection_spec"], "selection_spec"),
            selection_cutoff_utc_ns=UtcNs(
                _integer(
                    feature_doc["selection_cutoff_utc_ns"], "selection_cutoff_utc_ns"
                )
            ),
            membership_digest=_digest(feature_doc["membership_digest"]),
        )
        promoted = root["promoted"]
        if not isinstance(promoted, bool):
            _bad("promoted must be boolean")
        warnings = tuple(
            _string(value, "promotion warning")
            for value in _array(root["promotion_warnings"], "promotion_warnings")
        )
        return DatasetSnapshotBundle(
            schema=SchemaRef(DatasetSnapshotBundle.SCHEMA_ID),
            feature_dataset=feature_dataset,
            evaluated_method_id=_string(
                root["evaluated_method_id"], "evaluated_method_id"
            ),
            members=members,
            snapshot_digest=_digest(root["snapshot_digest"]),
            promoted=promoted,
            promotion_warnings=warnings,
        )
    except MalformedDatasetSnapshotError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise MalformedDatasetSnapshotError(str(exc)) from exc


def _member_document(member: DatasetMember) -> object:
    ref = member.feature_set_ref
    bundle = ref.bundle_ref
    return {
        "feature_set_ref": {
            "feature_set_id": str(ref.feature_set_id),
            "analysis_run_id": str(ref.analysis_run_id),
            "bundle_ref": {
                "digest": str(bundle.digest),
                "byte_count": bundle.byte_count,
                "media_type": bundle.media_type,
                "format_id": bundle.format_id,
                "locator": bundle.locator,
            },
        },
        "split_group_id": member.split_group_id,
        "split": member.split.value,
        "role": member.role.value,
        "truth": {
            "target_present": member.truth.target_present,
            "source": member.truth.source.value,
            "confidence": member.truth.confidence,
            "evidence": [
                {
                    "source": evidence.source.value,
                    "evidence_digest": str(evidence.evidence_digest),
                    "producer_id": evidence.producer_id,
                    "produced_utc_ns": evidence.produced_utc_ns,
                    "independent_of_method_ids": list(
                        evidence.independent_of_method_ids
                    ),
                    "uncertainty": [list(item) for item in evidence.uncertainty],
                    "base_recording_digest": (
                        str(evidence.base_recording_digest)
                        if evidence.base_recording_digest is not None
                        else None
                    ),
                    "injection_spec_digest": (
                        str(evidence.injection_spec_digest)
                        if evidence.injection_spec_digest is not None
                        else None
                    ),
                }
                for evidence in member.truth.evidence
            ],
        },
    }


def _parse_member(value: object, index: int) -> DatasetMember:
    name = f"members[{index}]"
    member = _object(value, name)
    _keys(member, {"feature_set_ref", "split_group_id", "split", "role", "truth"}, name)
    ref_doc = _object(member["feature_set_ref"], f"{name}.feature_set_ref")
    _keys(
        ref_doc,
        {"feature_set_id", "analysis_run_id", "bundle_ref"},
        f"{name}.feature_set_ref",
    )
    blob = _object(ref_doc["bundle_ref"], f"{name}.bundle_ref")
    _keys(
        blob,
        {"digest", "byte_count", "media_type", "format_id", "locator"},
        f"{name}.bundle_ref",
    )
    ref = FeatureSetRef(
        feature_set_id=FeatureSetId(
            _string(ref_doc["feature_set_id"], "feature_set_id")
        ),
        analysis_run_id=AnalysisRunId(
            _string(ref_doc["analysis_run_id"], "analysis_run_id")
        ),
        bundle_ref=ObjectRef(
            digest=_digest(blob["digest"]),
            byte_count=_integer(blob["byte_count"], "byte_count"),
            media_type=_string(blob["media_type"], "media_type"),
            format_id=_string(blob["format_id"], "format_id"),
            locator=_string(blob["locator"], "locator"),
        ),
    )
    truth_doc = _object(member["truth"], f"{name}.truth")
    _keys(
        truth_doc,
        {"target_present", "source", "confidence", "evidence"},
        f"{name}.truth",
    )
    source = LabelSource(_string(truth_doc["source"], "truth source"))
    present = truth_doc["target_present"]
    if present is not None and not isinstance(present, bool):
        _bad("target_present must be boolean or null")
    confidence = truth_doc["confidence"]
    if confidence is not None and (
        isinstance(confidence, bool) or not isinstance(confidence, (int, float))
    ):
        _bad("confidence must be a number or null")
    evidence = tuple(
        _parse_evidence(item, source, index)
        for index, item in enumerate(_array(truth_doc["evidence"], "evidence"))
    )
    return DatasetMember(
        feature_set_ref=ref,
        split_group_id=_string(member["split_group_id"], "split_group_id"),
        split=DatasetSplit(_string(member["split"], "split")),
        role=DatasetRole(_string(member["role"], "role")),
        truth=TruthLabel(
            target_present=present,
            source=source,
            evidence=evidence,
            confidence=float(confidence) if confidence is not None else None,
        ),
    )


def _parse_evidence(
    value: object, truth_source: LabelSource, index: int
) -> LabelEvidence:
    name = f"evidence[{index}]"
    evidence = _object(value, name)
    _keys(
        evidence,
        {
            "source",
            "evidence_digest",
            "producer_id",
            "produced_utc_ns",
            "independent_of_method_ids",
            "uncertainty",
            "base_recording_digest",
            "injection_spec_digest",
        },
        name,
    )
    source = LabelSource(_string(evidence["source"], "evidence source"))
    if source is not truth_source:
        _bad("label and evidence sources differ")
    uncertainty = tuple(
        (
            _string(_pair(item, "uncertainty")[0], "uncertainty key"),
            _string(_pair(item, "uncertainty")[1], "uncertainty value"),
        )
        for item in _array(evidence["uncertainty"], "uncertainty")
    )
    return LabelEvidence(
        source=source,
        evidence_digest=_digest(evidence["evidence_digest"]),
        producer_id=_string(evidence["producer_id"], "producer_id"),
        produced_utc_ns=_integer(evidence["produced_utc_ns"], "produced_utc_ns"),
        independent_of_method_ids=tuple(
            _string(item, "method ID")
            for item in _array(
                evidence["independent_of_method_ids"], "independent_of_method_ids"
            )
        ),
        uncertainty=uncertainty,
        base_recording_digest=_optional_digest(evidence["base_recording_digest"]),
        injection_spec_digest=_optional_digest(evidence["injection_spec_digest"]),
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MalformedDatasetSnapshotError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _object(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        _bad(f"{name} must be an object")
    return cast(Mapping[str, Any], value)


def _array(value: object, name: str) -> list[Any]:
    if not isinstance(value, list):
        _bad(f"{name} must be an array")
    return value


def _pair(value: object, name: str) -> list[Any]:
    pair = _array(value, name)
    if len(pair) != 2:
        _bad(f"{name} entries must contain two strings")
    return pair


def _keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        _bad(f"{name} fields differ: expected {sorted(expected)}")


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        _bad(f"{name} must be a non-empty string")
    return value


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _bad(f"{name} must be an integer")
    return value


def _digest(value: object) -> Digest:
    text = _string(value, "digest")
    prefix = f"{DigestAlgorithm.SHA256.value}:"
    if not text.startswith(prefix):
        _bad("only sha256 digests are supported")
    return Digest(DigestAlgorithm.SHA256, text[len(prefix) :])


def _optional_digest(value: object) -> Digest | None:
    return None if value is None else _digest(value)


def _bad(message: str) -> NoReturn:
    raise MalformedDatasetSnapshotError(message)
