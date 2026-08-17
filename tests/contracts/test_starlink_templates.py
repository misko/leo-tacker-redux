from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.analysis.recording.starlink_templates import (
    qin_edge_pilot_artifacts_v0_1,
    qin_edge_pilot_frame_v1,
    qin_edge_pilot_states_v1,
)
from leo_flow.contracts.core import ArtifactRef, Digest
from leo_flow.contracts.starlink import StarlinkEdge


def test_artifact_rejects_byte_order_transform_and_digest_ambiguity() -> None:
    artifact, _ = qin_edge_pilot_artifacts_v0_1(
        2_500_000.0,
        StarlinkEdge.LOWER,
    )

    with pytest.raises(ValueError, match="byte order"):
        replace(artifact, byte_order="big-endian")
    with pytest.raises(ValueError, match="sampling transform"):
        replace(artifact, sampling_transform="implicit-resampler")
    with pytest.raises(ValueError, match="does not apply a filter"):
        replace(artifact, filter_transform="unknown-radio-filter")
    with pytest.raises(ValueError, match="exact sample bytes"):
        replace(
            artifact,
            template_ref=ArtifactRef(
                artifact.template_ref.artifact_id,
                Digest.sha256(b"different-payload"),
                artifact.template_ref.schema,
            ),
        )


def test_generator_rejects_malformed_rate_and_roll() -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        qin_edge_pilot_frame_v1(float("nan"), StarlinkEdge.UPPER)
    with pytest.raises(TypeError, match="symbol_roll"):
        qin_edge_pilot_states_v1(StarlinkEdge.UPPER, symbol_roll=True)
