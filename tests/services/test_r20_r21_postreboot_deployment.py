from pathlib import Path

from leo_flow.capture.v5_station import load_v5_capture_station
from leo_flow.deployments.gauss_v5_campaign_operator import (
    load_gauss_campaign_runtime_config,
)

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy/gauss-campaign-r20-r21-postreboot-v1"
STATION_A = ROOT / "deploy/v5-scan/gauss-r20-science-postreboot-passive-v1.station.json"
STATION_B = ROOT / "deploy/v5-scan/gauss-r21-science-postreboot-passive-v1.station.json"


def test_r20_r21_runtime_and_sources_bind_exact_postreboot_science_radios() -> None:
    runtime = load_gauss_campaign_runtime_config(DEPLOY / "runtime.json")
    first = load_v5_capture_station(STATION_A)
    second = load_v5_capture_station(STATION_B)

    assert runtime.radio_ips == ("192.168.1.20", "192.168.1.21")
    assert (first.radio.uri, second.radio.uri) == (
        "ip:192.168.1.20",
        "ip:192.168.1.21",
    )
    assert (first.radio.expected_serial, second.radio.expected_serial) == (
        "1040005e0b100007100010000bf33a5d4d",
        "10400056f695001322002d0010ad1719f2",
    )
    assert tuple(map(str, first.radio.receiver_chain_ids)) == (
        "rx_lnb_a",
        "rx_lnb_b",
    )
    assert tuple(map(str, second.radio.receiver_chain_ids)) == (
        "rx_lnb_c",
        "rx_lnb_d",
    )
    assert first.expected_runtime == second.expected_runtime
    assert first.radio.require_both_tx_muted
    assert second.radio.require_both_tx_muted
    assert first.expected_runtime.runtime_id == (
        "gauss-pluto-v5-rx-integrity-close-barrier-1"
    )
    assert {
        (first.radio.firmware_release, first.radio.firmware_commit),
        (second.radio.firmware_release, second.radio.firmware_commit),
    } == {
        (
            "v0.38-plutoplus-spf-libiio-metadata-v5",
            "d7c87a9a28094ee6f0b23cb47df9ff737b5a69d8",
        )
    }


def test_obsolete_checkout_bound_system_units_are_removed() -> None:
    assert not (DEPLOY / "leo-v5-continuous-r20-r21-capture.service").exists()
    assert not (DEPLOY / "leo-v5-continuous-r20-r21-analysis.service").exists()
    assert not (ROOT / "deploy/gauss-continuous-v1/leo-v5-continuous.service").exists()
