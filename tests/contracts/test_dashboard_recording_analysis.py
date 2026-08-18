from __future__ import annotations

import pytest

from leo_flow.contracts.core import RecordingId, SchemaRef
from leo_flow.contracts.dashboard_recording_analysis import (
    EXTENDED_RECORDING_ANALYSIS_PRODUCTS,
    PRIMARY_RECORDING_ANALYSIS_PRODUCTS,
    RecordingAnalysisFacadeViewV0_1,
    RecordingAnalysisProduct,
    RecordingAnalysisProductEnvelopeV0_1,
    RecordingAnalysisProductState,
    RecordingAnalysisSection,
    RecordingAnalysisSectionEnvelopeV0_1,
)


def _products(section: RecordingAnalysisSection):
    inventory = (
        PRIMARY_RECORDING_ANALYSIS_PRODUCTS
        if section is RecordingAnalysisSection.PRIMARY
        else EXTENDED_RECORDING_ANALYSIS_PRODUCTS
    )
    return tuple(
        RecordingAnalysisProductEnvelopeV0_1(
            product, RecordingAnalysisProductState.NOT_ANALYZED, None, None
        )
        for product in inventory
    )


def test_facade_contract_fixes_the_full_primary_and_extended_inventory() -> None:
    primary = RecordingAnalysisSectionEnvelopeV0_1(
        RecordingAnalysisSection.PRIMARY,
        _products(RecordingAnalysisSection.PRIMARY),
    )
    extended = RecordingAnalysisSectionEnvelopeV0_1(
        RecordingAnalysisSection.EXTENDED,
        _products(RecordingAnalysisSection.EXTENDED),
    )
    view = RecordingAnalysisFacadeViewV0_1(
        SchemaRef(RecordingAnalysisFacadeViewV0_1.SCHEMA_ID),
        RecordingId("rec_facade"),
        (RecordingAnalysisSection.PRIMARY, RecordingAnalysisSection.EXTENDED),
        (primary, extended),
    )

    assert len(view.sections[0].products) == 5
    assert {item.product for item in view.sections[1].products} == {
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
    }


def test_product_envelopes_do_not_claim_complete_without_catalog_data() -> None:
    with pytest.raises(ValueError, match="catalog payload"):
        RecordingAnalysisProductEnvelopeV0_1(
            RecordingAnalysisProduct.QAM,
            RecordingAnalysisProductState.COMPLETE,
            None,
            None,
        )
    with pytest.raises(ValueError, match="cannot select a source"):
        RecordingAnalysisProductEnvelopeV0_1(
            RecordingAnalysisProduct.QAM,
            RecordingAnalysisProductState.PENDING,
            "adaptive-qam-v0.4",
            None,
        )


def test_section_envelopes_reject_partial_or_reordered_inventory() -> None:
    with pytest.raises(ValueError, match="inventory is not canonical"):
        RecordingAnalysisSectionEnvelopeV0_1(
            RecordingAnalysisSection.PRIMARY,
            _products(RecordingAnalysisSection.PRIMARY)[1:],
        )
