"""Self-describing Qin Appendix-A edge-pilot template artifact contract."""

from __future__ import annotations

from dataclasses import dataclass

from ._validation import require_positive, require_token
from .core import V0_1, ArtifactRef, Digest, SchemaRef, canonical_digest
from .starlink import StarlinkEdge


@dataclass(frozen=True)
class QinEdgePilotTemplateArtifactV0_1:
    schema: SchemaRef
    artifact_id: str
    source_citation_ref: ArtifactRef
    edge: StarlinkEdge
    pilot_indices: tuple[int, ...]
    first_ofdm_symbol_index: int
    final_ofdm_symbol_index: int
    pilot_codebook_digest: Digest
    symbol_roll: int
    sample_rate_hz: float
    frame_rate_hz: float
    ofdm_symbol_duration_s: float
    cyclic_prefix_duration_s: float
    subcarrier_spacing_hz: float
    tuning_offset_hz: float
    normalization: str
    sampling_transform: str
    filter_transform: str
    cancellation_zero_threshold_abs: float
    sample_count: int
    sample_encoding: str
    byte_order: str
    sample_bytes_digest: Digest
    template_ref: ArtifactRef

    SCHEMA_ID = "org.leo-flow.qin-edge-pilot-template-artifact"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported Qin edge-pilot template artifact schema")
        require_token(self.artifact_id, "artifact_id")
        expected = (
            tuple(range(528, 536))
            if self.edge is StarlinkEdge.LOWER
            else tuple(range(488, 496))
        )
        if self.pilot_indices != expected:
            raise ValueError("pilot indices do not match the declared Qin edge")
        if (self.first_ofdm_symbol_index, self.final_ofdm_symbol_index) != (2, 301):
            raise ValueError("Qin v1 pilot symbols must span OFDM indices 2..301")
        if self.symbol_roll not in (0, 17):
            raise ValueError("artifact must be exact or the fixed 17-symbol control")
        for name in (
            "sample_rate_hz",
            "frame_rate_hz",
            "ofdm_symbol_duration_s",
            "cyclic_prefix_duration_s",
            "subcarrier_spacing_hz",
        ):
            require_positive(getattr(self, name), name)
        if self.frame_rate_hz != 750.0:
            raise ValueError("Qin v1 template frame rate must be 750 Hz")
        if self.ofdm_symbol_duration_s != 4.4e-6:
            raise ValueError("Qin v1 OFDM symbol duration must be 4.4 microseconds")
        if self.cyclic_prefix_duration_s != 2 / 15 * 1e-6:
            raise ValueError("Qin v1 cyclic-prefix duration is inconsistent")
        if self.subcarrier_spacing_hz != 234_375.0:
            raise ValueError("Qin v1 subcarrier spacing must be 234375 Hz")
        expected_tuning_offset = (
            -115_429_687.5 if self.edge is StarlinkEdge.LOWER else 115_195_312.5
        )
        if self.tuning_offset_hz != expected_tuning_offset:
            raise ValueError("edge-pilot tuning offset is inconsistent")
        if self.normalization != "sum-eight-unit-pilots-divided-by-sqrt-eight":
            raise ValueError("unsupported pilot waveform normalization")
        if self.sampling_transform != "direct-evaluation-at-n-over-sample-rate":
            raise ValueError("unsupported pilot sampling transform")
        if self.filter_transform != "none":
            raise ValueError("v0.1 template synthesis does not apply a filter")
        if self.cancellation_zero_threshold_abs != 1e-10:
            raise ValueError("unsupported cancellation-zero canonicalization")
        if self.sample_count <= 0:
            raise ValueError("template artifact requires samples")
        if self.sample_count != round(self.sample_rate_hz / self.frame_rate_hz):
            raise ValueError("template sample count differs from one frame")
        if self.sample_encoding != "interleaved-complex-float32":
            raise ValueError("unsupported template sample encoding")
        if self.byte_order != "little-endian":
            raise ValueError("unsupported template byte order")
        if self.template_ref.digest != self.sample_bytes_digest:
            raise ValueError("template reference must identify the exact sample bytes")
        if self.template_ref.schema != SchemaRef(
            "org.leo-flow.starlink-edge-pilot-template", V0_1
        ):
            raise ValueError("template reference has the wrong payload schema")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)

    @property
    def ref(self) -> ArtifactRef:
        return ArtifactRef(
            self.artifact_id,
            self.digest,
            SchemaRef(self.SCHEMA_ID, V0_1),
        )
