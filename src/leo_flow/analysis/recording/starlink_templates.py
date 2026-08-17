"""Qin et al. Appendix-A Starlink edge-pilot template artifacts.

The constants are the 600-bit pilot integers published for equations (34)--(35)
in arXiv:2602.02627v1. This module is a native implementation and has no
``leo-tracker`` runtime dependency.
"""

from __future__ import annotations

import cmath
import math
import struct
from functools import lru_cache

from leo_flow.contracts.core import V0_1, ArtifactRef, SchemaRef, canonical_digest
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_templates import QinEdgePilotTemplateArtifactV0_1

from .starlink import (
    CONTROL_SYMBOL_ROLL,
    FRAME_RATE_HZ,
    TEMPLATE_SCHEMA_ID,
    KnownCodePilotTemplatePairV0_1,
    template_samples_digest,
)

OFDM_SYMBOL_DURATION_S = 4.4e-6
CYCLIC_PREFIX_DURATION_S = 2 / 15 * 1e-6
SUBCARRIER_SPACING_HZ = 234_375.0
MAXIMUM_TEMPLATE_SAMPLES = 1_000_000
CANCELLATION_ZERO_THRESHOLD_ABS = 1e-10
QIN_ARXIV_ID = "2602.02627v1"

# Each 150-digit hexadecimal integer encodes 300 base-4 coefficients for
# symbols i=2..301. Values are factual signal definitions from Appendix A.
QIN_EDGE_PILOT_HEX_V1 = {
    488: "7634046DA45F89042D0117E163167D4AE832D857515F3CAD90337697FB8F1CD048EFEF559ECD79688BCBBF44D2FA9BDFAE639DB5D7B1DD2DDCE4EC9733C0D4DCCF3172A0EC34CC226C530E",
    489: "CD9AFAA654147A5FE2B407B51FFE15215B3A71624139619628A9C33E8E3A32E5146C09BD3EE9026CA52032D7FD38960FFC52599E9B8A7F6942334BD4C6D99D4331DEF5674570B245FBB25F",
    490: "02481A2B278B88F096C8D174D369D0CF6781B70EBD402D6A6F4C985DA6265866A8374DC0B3E4917146FE3274CA5D61C3F9A31CB8125F291155CBD4F4F84E93C0D854BBC54EE14443EC2DF8",
    491: "D8DC99C2654265B8C32450114C37E2B725A822F1054B46F272877122E47109F113D59E37DFF418FEA3627C7A5CC0A93ABA0F9408E958DF4179C4DE40CEF842D333632B3E77BEB34B2E6045",
    492: "3CC5CA83B0D33089B14C3B6AC3D1946359726B4966B2E966BE61124A5D53E22A73EDBEB383A92F06CA6CAA8A5B1ECE695465145E286EEE1804CD79A00C84FC80C87DE9DF572F9B54AE798B",
    493: "C77BD59D15C2C917EEC97FB479B9F0B2BF5D2ECCD80248D2AC68C84CEA11BAD18D9F6F31B6AFD783347943562E2C6832EA76828FCDAB31EFF6A9A88EA48E3AFA625B2FCDA7B99B0295E926",
    494: "6152EF153B85110FB0B7E24D8334B1C4196DE872B598767BC3CB4A4827A09D924AA7F57EB946F1981D036E3001934B10C9E22ABB6AF1F047B3A874CA95E68CBA67063F605FD05D532AAD3C",
    495: "CD8CACF9DEFACD2CB9811439D8B7E16F9E09BED47370207150A86DFE24EA1298CCB0907F5BAB67D4660462C6B10F74B8D9FA7B6F9EC1399B30B43AF622A894B2220B6B509A84AABB58D023",
    528: "CCBF3A16929836160CEC6EB7417AE6C37DC1E828CEFB60CE0E6C3B546A76B0AE1E7BC0E9577528B0F78F82A4104EA2C316B945D385200C7E5A1C5B48F5F9F9AF5C4BA920ACA3A599DB9974",
    529: "9CF72F5F5B95CE7342C925CF1AAF457F182C32810E2F7486705D5FA2D9C8923B0173FB206B46045C6F162BB9FFD051DB5E5900EFD2DE24D4BB3FE87DD776F00B5613A7D22B2821E139A599",
    530: "296319D723210189953BB730DC6046E4EC5FB48F9718D5B600A01578CAC3159B58EE8A306663921FBE78EE7C1E8E049B4230A14EB4954933AB64F67B396DD6DB12BCBB3CCA60EA79E0614B",
    531: "1017FBBD3D03981EE9F4424D473B8A73E136C777956EAEBD4CA51E9B70D9F5D10657F268595A5C3687D2DD06C98630F817CABEF3EE660822350A70F10A29A8740212A9CF7E7D814D60A69C",
    532: "712EA482B28E96676E65D09994965587314F2B562D0E750FE566E89205A8D4DFED2C4FAFFC5ED1EA6FB63EC13513444006B78ADFB4BDB6CB05470601C9F8F4901423069C9FBD68D292C16F",
    533: "584E9F48ACA08784E696644C78ED9684FC484F32AA1B4DA8E95457358DF89FE8B9D84D47F30D3CA2F2DDF0E76E57F14A44675326EDCF15052CB62B7DF0EBE623057605CF2406E25BD56B3B",
    534: "4AF2ECF32983A9E781852F6E90DC6CCE901863F527E038DA22C0CE02E44FA0563718D93E7454293962B43594CC2EE427FAE6F15C1238D9C85ABC4E303F3AEC3404A52310CAC0378665E19A",
    535: "084AA73DF9F60535829A716EC94D95AA6901B41E81AEF28B03F08CDE7D45425B1164009D56459C4286E269F4B8EBDBA8BF6FC79847B08A69F79AF6E6A7AF05DA504455BA72727DD7BE7744",
}


def qin_edge_pilot_indices_v1(edge: StarlinkEdge) -> tuple[int, ...]:
    return (
        tuple(range(528, 536)) if edge is StarlinkEdge.LOWER else tuple(range(488, 496))
    )


def qin_edge_pilot_states_v1(
    edge: StarlinkEdge,
    *,
    symbol_roll: int = 0,
) -> tuple[tuple[int, ...], ...]:
    """Return the published 300-by-8 base-4 state matrix."""

    if isinstance(symbol_roll, bool) or not isinstance(symbol_roll, int):
        raise TypeError("symbol_roll must be an integer")
    indices = qin_edge_pilot_indices_v1(edge)
    rows = []
    for output_row in range(300):
        source_row = (output_row - symbol_roll) % 300
        shift = 2 * (299 - source_row)
        rows.append(
            tuple(
                (int(QIN_EDGE_PILOT_HEX_V1[index], 16) >> shift) & 3
                for index in indices
            )
        )
    return tuple(rows)


@lru_cache(maxsize=32)
def qin_edge_pilot_frame_v1(
    sample_rate_hz: float,
    edge: StarlinkEdge,
    *,
    symbol_roll: int = 0,
) -> tuple[complex, ...]:
    """Synthesize a pilot-only complex64-equivalent frame at band center."""

    if not math.isfinite(sample_rate_hz) or sample_rate_hz <= 0:
        raise ValueError("sample_rate_hz must be finite and positive")
    count = round(sample_rate_hz / FRAME_RATE_HZ)
    if count <= 0 or count > MAXIMUM_TEMPLATE_SAMPLES:
        raise ValueError("sample rate produces an unsupported template length")
    indices = qin_edge_pilot_indices_v1(edge)
    tuning_offset_hz = math.fsum(_subcarrier_offset_hz(index) for index in indices) / 8
    states = qin_edge_pilot_states_v1(edge, symbol_roll=symbol_roll)
    symbols = tuple(
        tuple(_complex64(cmath.exp(0.5j * math.pi * (state + 0.5))) for state in row)
        for row in states
    )
    output = []
    for sample_index in range(count):
        time_s = sample_index / sample_rate_hz
        symbol_index = math.floor(time_s / OFDM_SYMBOL_DURATION_S)
        if symbol_index < 2 or symbol_index > 301:
            output.append(0j)
            continue
        local_time_s = time_s - symbol_index * OFDM_SYMBOL_DURATION_S
        value = 0j
        for column, subcarrier in enumerate(indices):
            frequency_hz = _subcarrier_offset_hz(subcarrier) - tuning_offset_hz
            value += symbols[symbol_index - 2][column] * cmath.exp(
                2j * math.pi * frequency_hz * (local_time_s - CYCLIC_PREFIX_DURATION_S)
            )
        output.append(_complex64(value / math.sqrt(8)))
    return tuple(output)


def qin_edge_pilot_template_pair_v0_1(
    sample_rate_hz: float,
    edge: StarlinkEdge,
) -> KnownCodePilotTemplatePairV0_1:
    """Build exact and fixed 17-symbol-control artifacts for Redux analysis."""

    exact = qin_edge_pilot_frame_v1(sample_rate_hz, edge)
    control = qin_edge_pilot_frame_v1(
        sample_rate_hz,
        edge,
        symbol_roll=CONTROL_SYMBOL_ROLL,
    )
    exact_artifact, control_artifact = qin_edge_pilot_artifacts_v0_1(
        sample_rate_hz,
        edge,
    )
    return KnownCodePilotTemplatePairV0_1(
        edge,
        qin_edge_pilot_indices_v1(edge),
        sample_rate_hz,
        exact_artifact.template_ref,
        control_artifact.template_ref,
        exact,
        control,
    )


def qin_edge_pilot_artifacts_v0_1(
    sample_rate_hz: float,
    edge: StarlinkEdge,
) -> tuple[QinEdgePilotTemplateArtifactV0_1, QinEdgePilotTemplateArtifactV0_1]:
    """Materialize checked exact/control manifests for a recorded sample rate."""

    return (
        _template_artifact(sample_rate_hz, edge, symbol_roll=0),
        _template_artifact(
            sample_rate_hz,
            edge,
            symbol_roll=CONTROL_SYMBOL_ROLL,
        ),
    )


def qin_source_citation_ref_v1() -> ArtifactRef:
    return ArtifactRef(
        "qin-pilots-other-predictable-elements-arxiv-2602-02627v1",
        canonical_digest(
            {
                "arxiv_id": QIN_ARXIV_ID,
                "title": "Pilots and Other Predictable Elements of the Starlink Ku-Band Downlink",
                "authors": (
                    "Wenkai Qin",
                    "Mark L. Psiaki",
                    "John R. Bowman",
                    "Todd E. Humphreys",
                ),
                "pilot_definition": "equations-34-35-and-appendix-a",
            }
        ),
        SchemaRef("org.leo-flow.external-science-citation", V0_1),
    )


def _template_artifact(
    sample_rate_hz: float,
    edge: StarlinkEdge,
    *,
    symbol_roll: int,
) -> QinEdgePilotTemplateArtifactV0_1:
    samples = qin_edge_pilot_frame_v1(
        sample_rate_hz,
        edge,
        symbol_roll=symbol_roll,
    )
    payload_digest = template_samples_digest(samples)
    role = "exact" if symbol_roll == 0 else "roll17-control"
    template_ref = ArtifactRef(
        f"qin-edge-pilot-{edge.value}-{role}-{payload_digest.value[:16]}",
        payload_digest,
        SchemaRef(TEMPLATE_SCHEMA_ID, V0_1),
    )
    codebook = tuple(
        (index, QIN_EDGE_PILOT_HEX_V1[index])
        for index in qin_edge_pilot_indices_v1(edge)
    )
    artifact_id = (
        f"qin-edge-pilot-{edge.value}-{role}-artifact-{payload_digest.value[:16]}"
    )
    return QinEdgePilotTemplateArtifactV0_1(
        SchemaRef(QinEdgePilotTemplateArtifactV0_1.SCHEMA_ID, V0_1),
        artifact_id,
        qin_source_citation_ref_v1(),
        edge,
        qin_edge_pilot_indices_v1(edge),
        2,
        301,
        canonical_digest(codebook),
        symbol_roll,
        sample_rate_hz,
        FRAME_RATE_HZ,
        OFDM_SYMBOL_DURATION_S,
        CYCLIC_PREFIX_DURATION_S,
        SUBCARRIER_SPACING_HZ,
        _edge_tuning_offset_hz(edge),
        "sum-eight-unit-pilots-divided-by-sqrt-eight",
        "direct-evaluation-at-n-over-sample-rate",
        "none",
        CANCELLATION_ZERO_THRESHOLD_ABS,
        len(samples),
        "interleaved-complex-float32",
        "little-endian",
        payload_digest,
        template_ref,
    )


def _subcarrier_offset_hz(index: int) -> float:
    signed = index if index < 512 else index - 1024
    return signed * SUBCARRIER_SPACING_HZ


def _edge_tuning_offset_hz(edge: StarlinkEdge) -> float:
    indices = qin_edge_pilot_indices_v1(edge)
    return math.fsum(_subcarrier_offset_hz(index) for index in indices) / len(indices)


def _complex64(value: complex) -> complex:
    # The eight-carrier sum has mathematically exact cancellations whose libm
    # residues remain below 2e-12. Canonical zero keeps artifact bytes portable.
    real = 0.0 if abs(value.real) < CANCELLATION_ZERO_THRESHOLD_ABS else value.real
    imag = 0.0 if abs(value.imag) < CANCELLATION_ZERO_THRESHOLD_ABS else value.imag
    return complex(
        struct.unpack("!f", struct.pack("!f", real))[0],
        struct.unpack("!f", struct.pack("!f", imag))[0],
    )
