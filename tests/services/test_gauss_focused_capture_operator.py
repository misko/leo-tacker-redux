from __future__ import annotations

from leo_flow.capture.scan_plan import starlink_edge_pilot_if_hz
from leo_flow.contracts.core import Digest, UtcNs
from leo_flow.deployments.gauss_focused_analysis_operator import (
    _parser as analysis_parser,
)
from leo_flow.deployments.gauss_focused_capture_operator import (
    BANDWIDTH_HZ,
    CHANNEL,
    DURATION_NS,
    EDGE,
    SAMPLE_RATE_HZ,
    FocusedCaptureDefinition,
    _parser,
)


def test_operator_policy_is_exact_ch4_lower_twenty_seconds() -> None:
    definition = FocusedCaptureDefinition(
        "focused_test",
        UtcNs(123),
        Digest.sha256(b"a"),
        Digest.sha256(b"b"),
    )

    assert CHANNEL == 4
    assert EDGE == "lower"
    assert SAMPLE_RATE_HZ == BANDWIDTH_HZ == 2_500_000
    assert DURATION_NS == 20_000_000_000
    assert (
        starlink_edge_pilot_if_hz(CHANNEL, EDGE, lnb_lo_hz=9_750_000_000)
        == 1_709_687_500
    )
    assert definition.document()["analysis_submission"] == (
        "immediate_after_terminal_pair"
    )
    assert str(definition.digest).startswith("sha256:")

    sixty_seconds = FocusedCaptureDefinition(
        "focused_test_60s",
        UtcNs(123),
        Digest.sha256(b"a"),
        Digest.sha256(b"b"),
        60_000_000_000,
    )
    assert sixty_seconds.document()["duration_ns"] == 60_000_000_000
    assert sixty_seconds.digest != definition.digest


def test_operator_help_names_focused_capture() -> None:
    text = _parser().format_help()
    assert text.startswith("usage: leo-gauss-focused-capture")
    assert "--duration-seconds DURATION_SECONDS" in text


def test_analysis_help_promises_no_radio_contact() -> None:
    text = analysis_parser().format_help()
    assert text.startswith("usage: leo-gauss-focused-analysis")
    assert "without radio contact" in text
    assert "--compute-workers COMPUTE_WORKERS" in text
