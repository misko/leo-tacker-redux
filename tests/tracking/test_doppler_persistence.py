from __future__ import annotations

import io

import pytest

from leo_flow.analysis.tracking.blind_doppler_codec import (
    BLIND_DOPPLER_FORMAT_ID,
    BLIND_DOPPLER_MEDIA_TYPE,
    encode_blind_doppler_bundle,
)
from leo_flow.analysis.tracking.doppler_persistence import (
    ADVANCED_DOPPLER_FORMAT_ID,
    ADVANCED_DOPPLER_MEDIA_TYPE,
    DopplerPersistenceError,
    DurableDopplerReaderV0_1,
    decode_advanced_doppler_bundle,
    encode_advanced_doppler_bundle,
)
from leo_flow.contracts.blind_doppler import BlindDopplerBundleV0_1
from leo_flow.contracts.core import (
    Digest,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
)
from leo_flow.contracts.doppler_evidence import (
    AdvancedDopplerEvidenceBundleV0_1,
    AdvancedTrackEvidenceV0_1,
    CandidatePathAssociationV0_1,
    DopplerAnalysisId,
    DopplerAnalysisRefV0_1,
    SlopeBankEvidenceV0_1,
)
from leo_flow.contracts.waterfall import WaterfallProductId
from leo_flow.storage.filesystem import FileSystemBlobStore


def _bundles():
    input_digest = Digest.sha256(b"spectrogram")
    basic = BlindDopplerBundleV0_1(
        SchemaRef(BlindDopplerBundleV0_1.SCHEMA_ID),
        input_digest,
        Digest.sha256(b"basic-config"),
        "blind-doppler-v0.1",
        True,
        6,
        0,
        (),
        (),
        ("no_candidate_met_track_bounds",),
    )
    path_digest = Digest.sha256(b"candidate-path")
    track = AdvancedTrackEvidenceV0_1(
        Digest.sha256(b"slope-track"),
        (2, 3, 4, 5, 6, 7),
        1.0,
        2_000.0,
        8.0,
        7.0,
    )
    advanced = AdvancedDopplerEvidenceBundleV0_1(
        SchemaRef(AdvancedDopplerEvidenceBundleV0_1.SCHEMA_ID),
        input_digest,
        Digest.sha256(encode_blind_doppler_bundle(basic)),
        Digest.sha256(b"advanced-config"),
        (),
        "advanced-blind-doppler-v0.1",
        True,
        "temporal-median-residual-db-minus-per-row-median-db",
        CandidatePathAssociationV0_1(
            "advanced-path-only", path_digest, None, 0, 0.0, None, None
        ),
        SlopeBankEvidenceV0_1(
            path_digest,
            Digest.sha256(b"spectrogram"),
            track,
            None,
            8.0,
            1.0,
            0.0,
            (0.5, 0.25),
            (0, 3),
            (1, 4),
            (2, 5),
        ),
        (track,),
        reason_codes=("no-basic-blind-candidate",),
    )
    return basic, advanced


def test_advanced_codec_is_canonical_and_round_trips() -> None:
    _, advanced = _bundles()
    payload = encode_advanced_doppler_bundle(advanced)

    assert decode_advanced_doppler_bundle(payload) == advanced
    with pytest.raises(ValueError, match="canonical"):
        decode_advanced_doppler_bundle(payload + b"\n")


def test_reader_verifies_exact_catalog_and_blob_closure(tmp_path) -> None:
    basic, advanced = _bundles()
    store = FileSystemBlobStore(tmp_path / "cas")
    basic_payload = encode_blind_doppler_bundle(basic)
    advanced_payload = encode_advanced_doppler_bundle(advanced)
    basic_ref = store.put(
        io.BytesIO(basic_payload),
        expected_digest=Digest.sha256(basic_payload),
        expected_bytes=len(basic_payload),
        media_type=BLIND_DOPPLER_MEDIA_TYPE,
        format_id=BLIND_DOPPLER_FORMAT_ID,
        idempotency_key="basic",
    )
    advanced_ref = store.put(
        io.BytesIO(advanced_payload),
        expected_digest=Digest.sha256(advanced_payload),
        expected_bytes=len(advanced_payload),
        media_type=ADVANCED_DOPPLER_MEDIA_TYPE,
        format_id=ADVANCED_DOPPLER_FORMAT_ID,
        idempotency_key="advanced",
    )
    ref = DopplerAnalysisRefV0_1(
        DopplerAnalysisId("doppler_" + "1" * 32),
        RecordingId("rec_doppler"),
        WaterfallProductId("waterfall_" + "2" * 32),
        Digest.sha256(b"waterfall"),
        SegmentId("seg_ch4_lower"),
        ReceiverChainId("rx_first"),
        basic.input_identity_digest,
        basic.config_digest,
        advanced.config_digest,
        basic_ref,
        advanced_ref,
        0,
        0,
        None,
    )

    class _Query:
        def list_recording_doppler(self, recording_id):
            return (ref,) if recording_id == ref.recording_id else ()

    reader = DurableDopplerReaderV0_1(store, _Query())
    with reader.open(ref.recording_id, ref.doppler_id) as view:
        assert view.ref == ref
        assert view.basic == basic
        assert view.advanced == advanced

    wrong = DopplerAnalysisRefV0_1(
        ref.doppler_id,
        ref.recording_id,
        ref.waterfall_product_id,
        ref.waterfall_bundle_digest,
        ref.segment_id,
        ref.receiver_chain_id,
        Digest.sha256(b"wrong-spectrogram"),
        ref.basic_config_digest,
        ref.advanced_config_digest,
        ref.basic_bundle_ref,
        ref.advanced_bundle_ref,
        ref.candidate_count,
        ref.moving_candidate_count,
        ref.strongest_candidate_score,
    )

    class _WrongQuery:
        def list_recording_doppler(self, recording_id):
            return (wrong,)

    with (
        pytest.raises(DopplerPersistenceError, match="catalog projection"),
        DurableDopplerReaderV0_1(store, _WrongQuery()).open(
            wrong.recording_id, wrong.doppler_id
        ),
    ):
        pass
