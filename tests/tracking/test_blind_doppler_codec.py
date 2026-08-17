from __future__ import annotations

import json

import pytest

from leo_flow.analysis.tracking.blind_doppler import (
    BasicBlindDopplerAnalyzer,
    BlindDopplerConfig,
    blind_doppler_config_digest,
)
from leo_flow.analysis.tracking.blind_doppler_codec import (
    MAX_BLIND_DOPPLER_BUNDLE_BYTES,
    MalformedBlindDopplerError,
    decode_blind_doppler_bundle,
    encode_blind_doppler_bundle,
)
from leo_flow.contracts.blind_doppler import (
    BlindDopplerAnalysisRequestV0_1,
    SpectrogramRowV0_1,
    SpectrogramSliceV0_1,
)
from leo_flow.contracts.core import Digest, ReceiverChainId, SchemaRef, SegmentId, UtcNs


def _bundle():
    identity = Digest.sha256(b"input")
    spectrogram = SpectrogramSliceV0_1(
        schema=SchemaRef(SpectrogramSliceV0_1.SCHEMA_ID),
        input_identity_digest=identity,
        segment_id=SegmentId("seg_codec"),
        receiver_chain_id=ReceiverChainId("rx_codec"),
        center_frequency_hz=1_000_000.0,
        frequency_bin_offsets_hz=(-1_000.0, 0.0, 1_000.0),
        power_reference="test-db",
        rows=tuple(
            SpectrogramRowV0_1(
                midpoint_utc_ns=UtcNs(1_800_000_000_000_000_000 + row * 1_000_000),
                power_db=(0.0, 20.0, 0.0),
            )
            for row in range(5)
        ),
    )
    config = BlindDopplerConfig()
    request = BlindDopplerAnalysisRequestV0_1(
        schema=SchemaRef(BlindDopplerAnalysisRequestV0_1.SCHEMA_ID),
        input_identity_digest=identity,
        config_digest=blind_doppler_config_digest(config),
        max_candidates=2,
    )
    return BasicBlindDopplerAnalyzer(config).analyze_blind_doppler(spectrogram, request)


def test_codec_round_trip_is_canonical() -> None:
    bundle = _bundle()
    encoded = encode_blind_doppler_bundle(bundle)

    assert decode_blind_doppler_bundle(encoded) == bundle
    assert encode_blind_doppler_bundle(decode_blind_doppler_bundle(encoded)) == encoded


def test_codec_rejects_noncanonical_and_unknown_fields() -> None:
    encoded = encode_blind_doppler_bundle(_bundle())
    with pytest.raises(MalformedBlindDopplerError, match="canonical"):
        decode_blind_doppler_bundle(b" " + encoded)

    document = json.loads(encoded)
    document["surprise"] = True
    with pytest.raises(MalformedBlindDopplerError, match="fields differ"):
        decode_blind_doppler_bundle(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        )


def test_codec_rejects_duplicate_keys_and_oversize() -> None:
    with pytest.raises(MalformedBlindDopplerError, match="duplicate JSON key"):
        decode_blind_doppler_bundle(b'{"schema":1,"schema":2}')
    with pytest.raises(MalformedBlindDopplerError, match="size limit"):
        decode_blind_doppler_bundle(b"x" * (MAX_BLIND_DOPPLER_BUNDLE_BYTES + 1))
