"""Immutable basic/advanced Doppler blobs and their narrow catalog projection."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Any, NoReturn

from leo_flow.analysis.recording.waterfall_doppler_pipeline import (
    PreparedTileDopplerV0_1,
)
from leo_flow.contracts.blind_doppler import BlindDopplerBundleV0_1
from leo_flow.contracts.core import (
    Digest,
    DigestAlgorithm,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SchemaVersion,
    SegmentId,
    canonical_digest,
    canonical_json_bytes,
)
from leo_flow.contracts.doppler_evidence import (
    AdvancedDopplerEvidenceBundleV0_1,
    AdvancedTrackEvidenceV0_1,
    BroadbandEvidenceV0_1,
    CandidatePathAssociationV0_1,
    CombEvidenceV0_1,
    DopplerAnalysisId,
    DopplerAnalysisRefV0_1,
    DualReceiverEvidenceV0_1,
    PostBlindTleAssociationV0_1,
    RecordingDopplerAnalysisQueryPortV0_1,
    SlopeBankEvidenceV0_1,
)
from leo_flow.contracts.storage import ObjectRef
from leo_flow.contracts.waterfall_v0_2 import WaterfallBundleV0_2
from leo_flow.storage.ports import BlobReader

from .blind_doppler_codec import (
    BLIND_DOPPLER_FORMAT_ID,
    BLIND_DOPPLER_MEDIA_TYPE,
    MAX_BLIND_DOPPLER_BUNDLE_BYTES,
    decode_blind_doppler_bundle,
    encode_blind_doppler_bundle,
)

ADVANCED_DOPPLER_MEDIA_TYPE = "application/json"
ADVANCED_DOPPLER_FORMAT_ID = "advanced-doppler-evidence-bundle-v0.1"
MAX_ADVANCED_DOPPLER_BUNDLE_BYTES = 8 * 1024 * 1024


class DopplerPersistenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class DopplerCatalogProjectionV0_1:
    doppler_id: DopplerAnalysisId
    recording_id: RecordingId
    waterfall_product_id: str
    waterfall_bundle_digest: Digest
    segment_id: SegmentId
    receiver_chain_id: ReceiverChainId
    spectrogram_digest: Digest
    basic_config_digest: Digest
    advanced_config_digest: Digest
    candidate_count: int
    moving_candidate_count: int
    strongest_candidate_score: float | None


@dataclass(frozen=True)
class DurableDopplerViewV0_1:
    ref: DopplerAnalysisRefV0_1
    basic: BlindDopplerBundleV0_1
    advanced: AdvancedDopplerEvidenceBundleV0_1


class DurableDopplerReaderV0_1:
    def __init__(
        self, blobs: BlobReader, query: RecordingDopplerAnalysisQueryPortV0_1
    ) -> None:
        self._blobs = blobs
        self._query = query

    def open(
        self, recording_id: RecordingId, doppler_id: DopplerAnalysisId
    ) -> AbstractContextManager[DurableDopplerViewV0_1]:
        return self._open(recording_id, doppler_id)

    @contextmanager
    def _open(
        self, recording_id: RecordingId, doppler_id: DopplerAnalysisId
    ) -> Iterator[DurableDopplerViewV0_1]:
        ref = next(
            (
                item
                for item in self._query.list_recording_doppler(recording_id)
                if item.doppler_id == doppler_id
            ),
            None,
        )
        if ref is None:
            raise DopplerPersistenceError("Doppler analysis was not found")
        basic_payload = self._read(
            ref.basic_bundle_ref,
            media_type=BLIND_DOPPLER_MEDIA_TYPE,
            format_id=BLIND_DOPPLER_FORMAT_ID,
            maximum_bytes=MAX_BLIND_DOPPLER_BUNDLE_BYTES,
        )
        advanced_payload = self._read(
            ref.advanced_bundle_ref,
            media_type=ADVANCED_DOPPLER_MEDIA_TYPE,
            format_id=ADVANCED_DOPPLER_FORMAT_ID,
            maximum_bytes=MAX_ADVANCED_DOPPLER_BUNDLE_BYTES,
        )
        try:
            basic = decode_blind_doppler_bundle(basic_payload)
            advanced = decode_advanced_doppler_bundle(advanced_payload)
        except ValueError as error:
            raise DopplerPersistenceError("Doppler bundle bytes are invalid") from error
        if (
            basic.input_identity_digest != ref.spectrogram_digest
            or basic.config_digest != ref.basic_config_digest
            or advanced.input_identity_digest != ref.spectrogram_digest
            or advanced.blind_bundle_digest != ref.basic_bundle_ref.digest
            or advanced.config_digest != ref.advanced_config_digest
            or len(basic.candidates) != ref.candidate_count
        ):
            raise DopplerPersistenceError(
                "Doppler bundles disagree with catalog projection"
            )
        yield DurableDopplerViewV0_1(ref, basic, advanced)

    def _read(
        self,
        ref: ObjectRef,
        *,
        media_type: str,
        format_id: str,
        maximum_bytes: int,
    ) -> bytes:
        if (
            ref.media_type != media_type
            or ref.format_id != format_id
            or ref.byte_count > maximum_bytes
        ):
            raise DopplerPersistenceError("Doppler blob metadata is invalid")
        metadata = self._blobs.head(ref)
        if metadata.ref != ref or not metadata.verified:
            raise DopplerPersistenceError("Doppler blob is not verified")
        with self._blobs.open(ref) as stream:
            payload = stream.read(maximum_bytes + 1)
        if len(payload) != ref.byte_count or Digest.sha256(payload) != ref.digest:
            raise DopplerPersistenceError("Doppler bytes differ from their reference")
        return payload


def doppler_projection_v0_1(
    waterfall: WaterfallBundleV0_2,
    waterfall_bundle_digest: Digest,
    prepared: PreparedTileDopplerV0_1,
) -> DopplerCatalogProjectionV0_1:
    spectrogram = prepared.spectrogram
    basic = prepared.basic
    advanced = prepared.advanced
    basic_digest = Digest.sha256(encode_blind_doppler_bundle(basic))
    if (
        basic.input_identity_digest != spectrogram.input_identity_digest
        or advanced.input_identity_digest != spectrogram.input_identity_digest
        or advanced.blind_bundle_digest != basic_digest
    ):
        raise DopplerPersistenceError("Doppler analysis closure is inconsistent")
    identity = canonical_digest(
        {
            "recording_id": str(waterfall.recording_id),
            "waterfall_product_id": str(waterfall.product_id),
            "waterfall_bundle_digest": waterfall_bundle_digest,
            "segment_id": str(spectrogram.segment_id),
            "receiver_chain_id": str(spectrogram.receiver_chain_id),
            "spectrogram_digest": spectrogram.input_identity_digest,
            "basic_config_digest": basic.config_digest,
            "advanced_config_digest": advanced.config_digest,
            "auxiliary_input_digests": advanced.auxiliary_input_digests,
        }
    )
    moving = sum(
        candidate.stationary_control.moving_model_preferred
        for candidate in basic.candidates
    )
    return DopplerCatalogProjectionV0_1(
        doppler_id=DopplerAnalysisId(f"doppler_{identity.value[:32]}"),
        recording_id=waterfall.recording_id,
        waterfall_product_id=str(waterfall.product_id),
        waterfall_bundle_digest=waterfall_bundle_digest,
        segment_id=spectrogram.segment_id,
        receiver_chain_id=spectrogram.receiver_chain_id,
        spectrogram_digest=spectrogram.input_identity_digest,
        basic_config_digest=basic.config_digest,
        advanced_config_digest=advanced.config_digest,
        candidate_count=len(basic.candidates),
        moving_candidate_count=moving,
        strongest_candidate_score=(
            None if not basic.candidates else basic.candidates[0].ranking_score
        ),
    )


def encode_advanced_doppler_bundle(
    bundle: AdvancedDopplerEvidenceBundleV0_1,
) -> bytes:
    payload = canonical_json_bytes(bundle)
    if len(payload) > MAX_ADVANCED_DOPPLER_BUNDLE_BYTES:
        raise ValueError("advanced Doppler bundle exceeds its byte bound")
    return payload


def decode_advanced_doppler_bundle(data: bytes) -> AdvancedDopplerEvidenceBundleV0_1:
    if len(data) > MAX_ADVANCED_DOPPLER_BUNDLE_BYTES:
        raise ValueError("advanced Doppler bundle exceeds its byte bound")
    try:
        root = _object(json.loads(data, object_pairs_hook=_unique_object), "root")
        if canonical_json_bytes(root) != data:
            _bad("advanced Doppler bytes are not canonical JSON")
        _keys(
            root,
            {
                "schema",
                "input_identity_digest",
                "blind_bundle_digest",
                "config_digest",
                "auxiliary_input_digests",
                "algorithm_version",
                "candidate_only",
                "spectral_peak_excess_reference",
                "association",
                "slope_bank",
                "peeled_tracks",
                "comb",
                "broadband",
                "dual_receiver",
                "tle_association",
                "warnings",
                "reason_codes",
            },
            "root",
        )
        return AdvancedDopplerEvidenceBundleV0_1(
            schema=_schema(root["schema"], "schema"),
            input_identity_digest=_digest(root["input_identity_digest"], "input"),
            blind_bundle_digest=_digest(root["blind_bundle_digest"], "blind"),
            config_digest=_digest(root["config_digest"], "config"),
            auxiliary_input_digests=tuple(
                _digest(value, "auxiliary_input")
                for value in _array(
                    root["auxiliary_input_digests"], "auxiliary_input_digests"
                )
            ),
            algorithm_version=_string(root["algorithm_version"], "algorithm_version"),
            candidate_only=_boolean(root["candidate_only"], "candidate_only"),
            spectral_peak_excess_reference=_string(
                root["spectral_peak_excess_reference"],
                "spectral_peak_excess_reference",
            ),
            association=(
                None
                if root["association"] is None
                else _association(root["association"])
            ),
            slope_bank=None
            if root["slope_bank"] is None
            else _slope_bank(root["slope_bank"]),
            peeled_tracks=tuple(
                _track(value, "peeled_track")
                for value in _array(root["peeled_tracks"], "peeled_tracks")
            ),
            comb=None if root["comb"] is None else _comb(root["comb"]),
            broadband=None
            if root["broadband"] is None
            else _broadband(root["broadband"]),
            dual_receiver=(
                None if root["dual_receiver"] is None else _dual(root["dual_receiver"])
            ),
            tle_association=(
                None
                if root["tle_association"] is None
                else _tle(root["tle_association"])
            ),
            warnings=tuple(
                _string(value, "warning")
                for value in _array(root["warnings"], "warnings")
            ),
            reason_codes=tuple(
                _string(value, "reason_code")
                for value in _array(root["reason_codes"], "reason_codes")
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"invalid advanced Doppler bundle: {error}") from error


def _track(value: object, name: str) -> AdvancedTrackEvidenceV0_1:
    item = _object(value, name)
    _keys(
        item,
        {
            "bins",
            "path_digest",
            "slope_bins_per_row",
            "drift_rate_hz_s",
            "score",
            "stationary_improvement",
        },
        name,
    )
    return AdvancedTrackEvidenceV0_1(
        _digest(item["path_digest"], f"{name}.path_digest"),
        tuple(
            _integer(entry, f"{name}.bin")
            for entry in _array(item["bins"], f"{name}.bins")
        ),
        _number(item["slope_bins_per_row"], f"{name}.slope"),
        _number(item["drift_rate_hz_s"], f"{name}.drift_rate_hz_s"),
        _number(item["score"], f"{name}.score"),
        _number(item["stationary_improvement"], f"{name}.stationary_improvement"),
    )


def _slope_bank(value: object) -> SlopeBankEvidenceV0_1:
    item = _object(value, "slope_bank")
    _keys(
        item,
        {
            "track",
            "candidate_path_digest",
            "source_input_digest",
            "basic_candidate_rank",
            "heldout_score",
            "stationary_score",
            "opposite_slope_score",
            "time_shuffle_scores",
            "training_rows",
            "validation_rows",
            "test_rows",
        },
        "slope_bank",
    )
    rank = item["basic_candidate_rank"]
    return SlopeBankEvidenceV0_1(
        _digest(item["candidate_path_digest"], "candidate_path_digest"),
        _digest(item["source_input_digest"], "source_input_digest"),
        _track(item["track"], "slope_bank.track"),
        None if rank is None else _integer(rank, "basic_candidate_rank"),
        _number(item["heldout_score"], "heldout_score"),
        _number(item["stationary_score"], "stationary_score"),
        _number(item["opposite_slope_score"], "opposite_slope_score"),
        tuple(
            _number(entry, "time_shuffle_score")
            for entry in _array(item["time_shuffle_scores"], "time_shuffle_scores")
        ),
        tuple(
            _integer(entry, "training_row")
            for entry in _array(item["training_rows"], "training_rows")
        ),
        tuple(
            _integer(entry, "validation_row")
            for entry in _array(item["validation_rows"], "validation_rows")
        ),
        tuple(
            _integer(entry, "test_row")
            for entry in _array(item["test_rows"], "test_rows")
        ),
    )


def _association(value: object) -> CandidatePathAssociationV0_1:
    item = _object(value, "association")
    _keys(
        item,
        {
            "state",
            "candidate_path_digest",
            "basic_candidate_rank",
            "overlap_point_count",
            "overlap_fraction",
            "mean_frequency_distance_hz",
            "maximum_frequency_distance_hz",
        },
        "association",
    )
    rank = item["basic_candidate_rank"]
    mean = item["mean_frequency_distance_hz"]
    maximum = item["maximum_frequency_distance_hz"]
    return CandidatePathAssociationV0_1(
        _string(item["state"], "association.state"),
        _digest(item["candidate_path_digest"], "association.path_digest"),
        None if rank is None else _integer(rank, "association.basic_candidate_rank"),
        _integer(item["overlap_point_count"], "association.overlap_point_count"),
        _number(item["overlap_fraction"], "association.overlap_fraction"),
        None if mean is None else _number(mean, "association.mean_distance"),
        None if maximum is None else _number(maximum, "association.maximum_distance"),
    )


def _comb(value: object) -> CombEvidenceV0_1:
    item = _object(value, "comb")
    _keys(
        item,
        {
            "candidate_path_digest",
            "source_input_digest",
            "spacing_bins",
            "wrong_spacing_bins",
            "fit_score",
            "heldout_score",
            "wrong_spacing_score",
        },
        "comb",
    )
    return CombEvidenceV0_1(
        _digest(item["candidate_path_digest"], "comb.candidate_path_digest"),
        _digest(item["source_input_digest"], "comb.source_input_digest"),
        _integer(item["spacing_bins"], "spacing_bins"),
        _integer(item["wrong_spacing_bins"], "wrong_spacing_bins"),
        _number(item["fit_score"], "fit_score"),
        _number(item["heldout_score"], "heldout_score"),
        _number(item["wrong_spacing_score"], "wrong_spacing_score"),
    )


def _broadband(value: object) -> BroadbandEvidenceV0_1:
    item = _object(value, "broadband")
    names = {
        "candidate_path_digest",
        "source_input_digest",
        "lower_slope_bins_per_row",
        "upper_slope_bins_per_row",
        "edge_slope_difference",
        "width_mad_fraction",
        "texture_shift_bins",
        "texture_correlation",
    }
    _keys(item, names, "broadband")
    return BroadbandEvidenceV0_1(
        candidate_path_digest=_digest(
            item["candidate_path_digest"], "broadband.candidate_path_digest"
        ),
        source_input_digest=_digest(
            item["source_input_digest"], "broadband.source_input_digest"
        ),
        **{
            name: _number(item[name], name)
            for name in names
            if name not in {"candidate_path_digest", "source_input_digest"}
        },
    )


def _dual(value: object) -> DualReceiverEvidenceV0_1:
    item = _object(value, "dual_receiver")
    _keys(
        item,
        {
            "peer_receiver_chain_id",
            "common_slope_bins_per_row",
            "slope_difference",
            "receiver_offsets_bins",
            "offset_removed_rms_bins",
            "path_correlation",
            "candidate_path_digest",
            "peer_candidate_path_digest",
            "source_input_digest",
        },
        "dual_receiver",
    )
    offsets = tuple(
        _number(entry, "receiver_offset")
        for entry in _array(item["receiver_offsets_bins"], "receiver_offsets_bins")
    )
    if len(offsets) != 2:
        _bad("dual receiver evidence requires two offsets")
    return DualReceiverEvidenceV0_1(
        _digest(item["candidate_path_digest"], "dual_receiver.candidate_path_digest"),
        _digest(
            item["peer_candidate_path_digest"],
            "dual_receiver.peer_candidate_path_digest",
        ),
        _digest(item["source_input_digest"], "dual_receiver.source_input_digest"),
        ReceiverChainId(
            _string(item["peer_receiver_chain_id"], "peer_receiver_chain_id")
        ),
        _number(item["common_slope_bins_per_row"], "common_slope_bins_per_row"),
        _number(item["slope_difference"], "slope_difference"),
        (offsets[0], offsets[1]),
        _number(item["offset_removed_rms_bins"], "offset_removed_rms_bins"),
        _number(item["path_correlation"], "path_correlation"),
    )


def _tle(value: object) -> PostBlindTleAssociationV0_1:
    item = _object(value, "tle_association")
    names = {
        "candidate_path_digest",
        "source_input_digest",
        "name",
        "offset_bins",
        "heldout_rms_bins",
        "runner_up_margin_bins",
        "stationary_control_rms_bins",
        "opposite_slope_control_rms_bins",
        "qualified",
    }
    _keys(item, names, "tle_association")
    return PostBlindTleAssociationV0_1(
        _digest(item["candidate_path_digest"], "tle.candidate_path_digest"),
        _digest(item["source_input_digest"], "tle.source_input_digest"),
        _string(item["name"], "name"),
        _number(item["offset_bins"], "offset_bins"),
        _number(item["heldout_rms_bins"], "heldout_rms_bins"),
        _number(item["runner_up_margin_bins"], "runner_up_margin_bins"),
        _number(item["stationary_control_rms_bins"], "stationary_control_rms_bins"),
        _number(
            item["opposite_slope_control_rms_bins"], "opposite_slope_control_rms_bins"
        ),
        _boolean(item["qualified"], "qualified"),
    )


def _schema(value: object, name: str) -> SchemaRef:
    item = _object(value, name)
    _keys(item, {"schema_id", "version"}, name)
    version = _object(item["version"], f"{name}.version")
    _keys(version, {"major", "minor"}, f"{name}.version")
    return SchemaRef(
        _string(item["schema_id"], f"{name}.schema_id"),
        SchemaVersion(
            _integer(version["major"], "major"), _integer(version["minor"], "minor")
        ),
    )


def _digest(value: object, name: str) -> Digest:
    item = _object(value, name)
    _keys(item, {"algorithm", "value"}, name)
    return Digest(
        DigestAlgorithm(_string(item["algorithm"], "algorithm")),
        _string(item["value"], "value"),
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _bad(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _object(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        _bad(f"{name} must be an object")
    return value


def _array(value: object, name: str) -> list[Any]:
    if not isinstance(value, list):
        _bad(f"{name} must be an array")
    return value


def _keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        _bad(f"{name} fields differ from schema")


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        _bad(f"{name} must be a string")
    return value


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _bad(f"{name} must be an integer")
    return value


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _bad(f"{name} must be a number")
    return float(value)


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        _bad(f"{name} must be a boolean")
    return value


def _bad(message: str) -> NoReturn:
    raise ValueError(message)
