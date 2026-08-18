"""Additive contract for the unversioned recording-analysis facade.

The facade composes already-published dashboard products.  Product catalogs keep
owning their payload contracts; this module only standardizes selection and the
durable availability state used when an optional catalog has no payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from .core import RecordingId, SchemaRef


class RecordingAnalysisSection(str, Enum):
    PRIMARY = "primary"
    EXTENDED = "extended"


class RecordingAnalysisProduct(str, Enum):
    RECORDING_FACTS = "recording_facts"
    EVIDENCE_CONTEXT = "evidence_context"
    QAM = "qam"
    ADAPTIVE_DETECTOR_RESPONSE = "adaptive_detector_response"
    DOPPLER_SUMMARY = "doppler_summary"
    APPROACHES = "approaches"
    FULL_DWELL_TIMELINE = "full_dwell_timeline"
    PILOT_PRESCREEN = "pilot_prescreen"
    PILOT_REFINEMENT = "pilot_refinement"
    LEGACY_FULL_DWELL = "legacy_full_dwell"
    BASIC_DOPPLER = "basic_doppler"
    ADVANCED_DOPPLER = "advanced_doppler"
    PILOT_DOPPLER_ASSOCIATION = "pilot_doppler_association"
    SYMBOLWISE_REPLAY = "symbolwise_replay"
    RECEIVER_AGNOSTIC_CFO_QAM = "receiver_agnostic_cfo_qam"
    LEGACY_SUITE = "legacy_suite"
    WATERFALL = "waterfall"
    DOPPLER_VISUALIZATION = "doppler_visualization"
    SURROGATE_NULL = "surrogate_null"
    TEMPORAL_PILOT = "temporal_pilot"
    PILOT_CONSTELLATION = "pilot_constellation"


PRIMARY_RECORDING_ANALYSIS_PRODUCTS = (
    RecordingAnalysisProduct.RECORDING_FACTS,
    RecordingAnalysisProduct.EVIDENCE_CONTEXT,
    RecordingAnalysisProduct.QAM,
    RecordingAnalysisProduct.ADAPTIVE_DETECTOR_RESPONSE,
    RecordingAnalysisProduct.DOPPLER_SUMMARY,
)

EXTENDED_RECORDING_ANALYSIS_PRODUCTS = (
    RecordingAnalysisProduct.APPROACHES,
    RecordingAnalysisProduct.FULL_DWELL_TIMELINE,
    RecordingAnalysisProduct.PILOT_PRESCREEN,
    RecordingAnalysisProduct.PILOT_REFINEMENT,
    RecordingAnalysisProduct.LEGACY_FULL_DWELL,
    RecordingAnalysisProduct.BASIC_DOPPLER,
    RecordingAnalysisProduct.ADVANCED_DOPPLER,
    RecordingAnalysisProduct.PILOT_DOPPLER_ASSOCIATION,
    RecordingAnalysisProduct.SYMBOLWISE_REPLAY,
    RecordingAnalysisProduct.RECEIVER_AGNOSTIC_CFO_QAM,
    RecordingAnalysisProduct.LEGACY_SUITE,
    RecordingAnalysisProduct.WATERFALL,
    RecordingAnalysisProduct.DOPPLER_VISUALIZATION,
    RecordingAnalysisProduct.SURROGATE_NULL,
    RecordingAnalysisProduct.TEMPORAL_PILOT,
    RecordingAnalysisProduct.PILOT_CONSTELLATION,
)


class RecordingAnalysisProductState(str, Enum):
    COMPLETE = "complete"
    NO_CANDIDATE = "no_candidate"
    PENDING = "pending"
    FAILED = "failed"
    NOT_ANALYZED = "not_analyzed"


class RecordingAnalysisProductAvailabilityQueryPortV0_1(Protocol):
    """Report durable non-catalog state; adapters must not infer work state."""

    def recording_analysis_product_state(
        self, recording_id: RecordingId, product: RecordingAnalysisProduct
    ) -> RecordingAnalysisProductState: ...


@dataclass(frozen=True)
class RecordingAnalysisProductEnvelopeV0_1:
    product: RecordingAnalysisProduct
    state: RecordingAnalysisProductState
    source: str | None
    payload: object | None

    def __post_init__(self) -> None:
        if (self.state is RecordingAnalysisProductState.COMPLETE) != (
            self.payload is not None
        ):
            raise ValueError("complete analysis products require a catalog payload")
        if self.payload is None and self.source is not None:
            raise ValueError("unavailable analysis products cannot select a source")
        if self.payload is not None and not self.source:
            raise ValueError("complete analysis products require a source")


@dataclass(frozen=True)
class RecordingAnalysisSectionEnvelopeV0_1:
    section: RecordingAnalysisSection
    products: tuple[RecordingAnalysisProductEnvelopeV0_1, ...]

    def __post_init__(self) -> None:
        expected = (
            PRIMARY_RECORDING_ANALYSIS_PRODUCTS
            if self.section is RecordingAnalysisSection.PRIMARY
            else EXTENDED_RECORDING_ANALYSIS_PRODUCTS
        )
        if tuple(item.product for item in self.products) != expected:
            raise ValueError("recording analysis section inventory is not canonical")


@dataclass(frozen=True)
class RecordingAnalysisFacadeViewV0_1:
    schema: SchemaRef
    recording_id: RecordingId
    requested_sections: tuple[RecordingAnalysisSection, ...]
    sections: tuple[RecordingAnalysisSectionEnvelopeV0_1, ...]

    SCHEMA_ID = "org.leo-flow.dashboard.recording-analysis-facade"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID):
            raise ValueError("unsupported recording analysis facade schema")
        if (
            not self.requested_sections
            or tuple(item.section for item in self.sections) != self.requested_sections
        ):
            raise ValueError("recording analysis sections differ from the request")
        if len(set(self.requested_sections)) != len(self.requested_sections):
            raise ValueError("recording analysis sections must be unique")
