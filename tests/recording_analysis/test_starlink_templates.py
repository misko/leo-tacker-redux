from __future__ import annotations

import pytest

from leo_flow.analysis.recording.starlink_templates import (
    QIN_EDGE_PILOT_HEX_V1,
    qin_edge_pilot_artifacts_v0_1,
    qin_edge_pilot_frame_v1,
    qin_edge_pilot_states_v1,
    qin_edge_pilot_template_pair_v0_1,
)
from leo_flow.contracts.starlink import StarlinkEdge


def test_qin_appendix_a_codes_decode_to_the_published_symbol_states() -> None:
    assert len(QIN_EDGE_PILOT_HEX_V1) == 16
    assert all(len(value) == 150 for value in QIN_EDGE_PILOT_HEX_V1.values())
    lower = qin_edge_pilot_states_v1(StarlinkEdge.LOWER)

    assert len(lower) == 300
    assert all(len(row) == 8 for row in lower)
    assert [lower[index][0] for index in (0, 1, -2, -1)] == [3, 0, 1, 0]


@pytest.mark.parametrize(
    "edge,roll,expected",
    [
        (StarlinkEdge.LOWER, 0, (0.49843961, 1.17021346)),
        (StarlinkEdge.LOWER, 17, (-1.10067379, 0.12092048)),
        (StarlinkEdge.UPPER, 0, (0.79843050, -0.08149827)),
        (StarlinkEdge.UPPER, 17, (0.30846128, -0.18039572)),
    ],
)
def test_sampled_waveform_matches_leo_tracker_numerical_oracle(
    edge: StarlinkEdge,
    roll: int,
    expected: tuple[float, float],
) -> None:
    # Frozen from leo-tracker's edge_pilot_frame at 2.5 MS/s. The oracle is not
    # imported: it remains a development comparator, never a runtime dependency.
    frame = qin_edge_pilot_frame_v1(2_500_000.0, edge, symbol_roll=roll)

    assert len(frame) == 3333
    assert frame[22].real == pytest.approx(expected[0], abs=1e-7)
    assert frame[22].imag == pytest.approx(expected[1], abs=1e-7)


def test_template_pair_pins_exact_and_roll17_sample_identities() -> None:
    pair = qin_edge_pilot_template_pair_v0_1(2_500_000.0, StarlinkEdge.LOWER)

    assert pair.exact_ref.digest.value == (
        "53b5bb1d72349c5038adad7a9a8944f3b7aa9174db0ce026ad09761d4e91d929"
    )
    assert pair.conditioned_control_ref.digest.value == (
        "8e8e867d36a55d1452cdb54a3d5e449b2cbd1873d2b146dc2bf08b88d19ccf49"
    )
    assert pair.control_symbol_roll == 17
    assert pair.pilot_indices == tuple(range(528, 536))


def test_both_edges_have_frozen_cf32le_oracle_payload_digests() -> None:
    expected = {
        StarlinkEdge.LOWER: (
            "53b5bb1d72349c5038adad7a9a8944f3b7aa9174db0ce026ad09761d4e91d929",
            "8e8e867d36a55d1452cdb54a3d5e449b2cbd1873d2b146dc2bf08b88d19ccf49",
        ),
        StarlinkEdge.UPPER: (
            "d524303e4c088bf74e9a0896542b967101448620c63fc1be4ba3959fc0c67c4e",
            "a30198ba7accf4fc2575116a53c7b48cadf57b91d4e678c436669e0a205716e9",
        ),
    }
    for edge, digests in expected.items():
        exact, control = qin_edge_pilot_artifacts_v0_1(2_500_000.0, edge)
        assert (
            exact.sample_bytes_digest.value,
            control.sample_bytes_digest.value,
        ) == digests
        assert exact.byte_order == control.byte_order == "little-endian"
        assert exact.sample_encoding == "interleaved-complex-float32"
        assert exact.symbol_roll == 0
        assert control.symbol_roll == 17
