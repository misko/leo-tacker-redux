from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from leo_flow.capture.drivers.v5_preflight import (
    ExpectedV5Radio,
    ObservedV5Radio,
    ObservedV5Runtime,
    attest_v5,
)
from leo_flow.capture.errors import RadioConfigurationError
from leo_flow.capture.scan_plan import (
    StarlinkEdgeScanSpec,
    build_starlink_edge_scan_plan,
)
from leo_flow.capture.v5_station import (
    V5CaptureStation,
    V5StationConfigurationError,
    load_v5_capture_station,
    require_disjoint_station_pair,
)
from leo_flow.contracts.capture import GainMode, GainSetting
from leo_flow.contracts.core import (
    Digest,
    PlanId,
    RadioId,
    ReceiverChainId,
    canonical_digest,
)
from leo_flow.deployments.v5_canary import V5RadioProvider
from leo_flow.deployments.v5_scan import DEVELOPMENT_STATION

STATION_PATH = Path("deploy/v5-scan/development-radio-15.station.json")
GAUSS_CANDIDATE_PATHS = (
    Path("deploy/v5-scan/gauss-radio-20-pluto-5d4d.station.json"),
    Path("deploy/v5-scan/gauss-radio-21-pluto-19f2.station.json"),
)


def _observed_runtime() -> ObservedV5Runtime:
    expected = DEVELOPMENT_STATION.expected_runtime
    return ObservedV5Runtime(
        runtime_id=expected.runtime_id,
        schema=expected.schema,
        iio_module_path=expected.iio_module_path,
        iio_version=expected.iio_version,
        iio_commit=expected.iio_commit,
        metadata_buffer_present=True,
        native_libiio_paths=(f"{expected.native_libiio_prefix}/lib/libiio.so.0.25",),
        available_backends=expected.required_backends,
        pyadi_version=expected.pyadi_version,
        pyadi_module_path=expected.pyadi_module_path,
        spf_module_path=expected.spf_module_path,
        spf_revision=expected.spf_revision,
        spf_import=expected.spf_import,
        metadata_protocol=expected.metadata_protocol,
    )


def test_checked_development_station_selects_radio_15_exactly() -> None:
    loaded = load_v5_capture_station(STATION_PATH)
    assert loaded == DEVELOPMENT_STATION
    assert loaded.radio.uri == "ip:192.168.1.15"
    assert loaded.radio.expected_serial == "104000b29905000e17000800065934759d"
    assert canonical_digest(loaded.capture_plan()) == loaded.plan.plan_digest
    assert loaded.capture_identity().radio_serial == loaded.radio.expected_serial
    assert loaded.radio_config().uri == "ip:192.168.1.15"
    assert loaded.runtime_manifest == Path(
        "/home/mouse9911/gits/leo-tracker-redux/deploy/v5-runtime/"
        "gauss-development.manifest.json"
    )
    assert loaded.expected_runtime.iio_module_path == (
        "/home/mouse9911/.cache/leo-flow/v5-runtime/lib/python3.11/site-packages/iio.py"
    )
    assert loaded.state.state_root == Path(
        "/home/mouse9911/.local/state/leo-flow/v5-scan/radio-15"
    )
    assert loaded.state.cas_root == Path(
        "/home/mouse9911/.local/share/leo-flow/objects"
    )
    assert loaded.state.mode_lock_path == Path(
        "/home/mouse9911/.local/state/leo-flow/pipeline-mode.lock"
    )
    assert loaded.state.require_cas_mount is False
    assert Digest.sha256(loaded.runtime_manifest.read_bytes()) == (
        loaded.runtime_manifest_digest
    )


def test_station_loader_rejects_unknown_fields_and_wrong_plan_digest(
    tmp_path: Path,
) -> None:
    document = json.loads(STATION_PATH.read_text(encoding="utf-8"))
    document["ambient_override"] = "forbidden"
    unknown = tmp_path / "unknown.json"
    unknown.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(V5StationConfigurationError, match="fields are not exact"):
        load_v5_capture_station(unknown)

    del document["ambient_override"]
    document["plan"]["sample_count"] = 524_288
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(V5StationConfigurationError, match="immutable digest"):
        load_v5_capture_station(changed)


def test_candidate_firmware_requires_exact_candidate_host_runtime(
    tmp_path: Path,
) -> None:
    document = json.loads(STATION_PATH.read_text(encoding="utf-8"))
    document["radio"]["firmware_release"] = (
        "v0.38-plutoplus-spf-libiio-metadata-v5-rx-integrity-candidate1"
    )
    document["radio"]["firmware_commit"] = (
        "de830094a177daf4f577b60b9d3324b41f99ae58+libiio.patch.195bddceada230ef"
    )
    mixed = tmp_path / "mixed-candidate.json"
    mixed.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(
        V5StationConfigurationError,
        match="requires its exact candidate host runtime",
    ):
        load_v5_capture_station(mixed)


def test_candidate_firmware_identity_rejects_unreviewed_patch(
    tmp_path: Path,
) -> None:
    document = json.loads(STATION_PATH.read_text(encoding="utf-8"))
    document["radio"]["firmware_release"] = (
        "v0.38-plutoplus-spf-libiio-metadata-v5-rx-integrity-candidate1"
    )
    document["radio"]["firmware_commit"] = "unreviewed"
    unreviewed = tmp_path / "unreviewed-candidate.json"
    unreviewed.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(V5StationConfigurationError, match="exact reviewed V5 build"):
        load_v5_capture_station(unreviewed)


def test_station_radio_accepts_exact_canonical_usb_context() -> None:
    station = DEVELOPMENT_STATION
    usb_radio = replace(station.radio, uri="usb:5.3.5")

    assert replace(station, radio=usb_radio).radio_config().uri == "usb:5.3.5"


@pytest.mark.parametrize(
    "uri",
    (
        "usb:",
        "usb:05.3.5",
        "usb:5.03.5",
        "usb:5.3.05",
        "usb:5.3",
        "usb:5.3.5.0",
        "usb:5.3.-1",
        "serial:104000bac4950008230026001b440a003a",
    ),
)
def test_station_radio_rejects_noncanonical_usb_context(uri: str) -> None:
    with pytest.raises(V5StationConfigurationError, match="URI"):
        replace(DEVELOPMENT_STATION.radio, uri=uri)


@pytest.mark.parametrize(
    "observed",
    (
        replace(_observed_runtime(), iio_module_path="/tmp/fallback/iio.py"),
        replace(_observed_runtime(), metadata_buffer_present=False),
    ),
)
def test_station_runtime_failure_precedes_any_radio_open(
    observed: ObservedV5Runtime,
) -> None:
    station = DEVELOPMENT_STATION
    expected_radio = ExpectedV5Radio(
        serial=station.radio.expected_serial,
        firmware_release=station.radio.firmware_release,
        firmware_commit=station.radio.firmware_commit,
    )
    observations: list[tuple[Path, object]] = []
    opened: list[str] = []

    def observe(path: Path, digest: object) -> ObservedV5Runtime:
        observations.append((path, digest))
        return observed

    def forbidden_open(uri: str) -> object:
        opened.append(uri)
        raise AssertionError("host runtime failure must precede radio open")

    provider = V5RadioProvider(
        station.radio_config(),
        expected_radio=expected_radio,
        expected_runtime=station.expected_runtime,
        runtime_manifest=station.runtime_manifest,
        runtime_manifest_digest=station.runtime_manifest_digest,
        runtime_observer=observe,
        device_factory=forbidden_open,  # type: ignore[arg-type]
    )
    with pytest.raises(RadioConfigurationError, match="V5 preflight failed"):
        provider.open()
    assert observations == [(station.runtime_manifest, station.runtime_manifest_digest)]
    assert opened == []


@pytest.mark.parametrize("endpoint_index", (0, 1))
def test_gauss_candidate_endpoint_rejects_peer_radio_serial(
    endpoint_index: int,
) -> None:
    stations = tuple(load_v5_capture_station(path) for path in GAUSS_CANDIDATE_PATHS)
    station = stations[endpoint_index]
    peer = stations[1 - endpoint_index]
    expected = ExpectedV5Radio(
        serial=station.radio.expected_serial,
        firmware_release=station.radio.firmware_release,
        firmware_commit=station.radio.firmware_commit,
    )
    observed = ObservedV5Radio(
        serial=peer.radio.expected_serial,
        firmware_release=station.radio.firmware_release,
        metadata_capability=expected.metadata_capability,
        enabled_scan_mask=expected.enabled_scan_mask,
        channel_count=expected.channel_count,
        component_layout=expected.component_layout,
        tx2_hardware_gain_db=expected.maximum_tx2_hardware_gain_db,
        tx2_dds_scales=tuple(
            (channel_id, 0.0) for channel_id in expected.tx2_dds_channel_ids
        ),
    )

    with pytest.raises(RadioConfigurationError, match="radio serial mismatch"):
        attest_v5(
            uri=station.radio.uri,
            expected_runtime=station.expected_runtime,
            observed_runtime=_observed_runtime(),
            expected_radio=expected,
            observed_radio=observed,
        )


def test_dual_station_candidate_has_disjoint_radio_plan_state_and_lock() -> None:
    first = DEVELOPMENT_STATION
    radio = replace(
        first.radio,
        uri="ip:192.168.1.21",
        expected_serial="second-radio-serial",
        radio_id=RadioId("radio_pluto_v5_21"),
        receiver_chain_ids=(
            ReceiverChainId("rx_v5_21_1"),
            ReceiverChainId("rx_v5_21_2"),
        ),
    )
    provisional_plan = replace(
        first.plan,
        plan_id=PlanId("plan_v5_scan_radio_21_v1"),
    )
    capture_plan = build_starlink_edge_scan_plan(
        StarlinkEdgeScanSpec(
            plan_id=provisional_plan.plan_id,
            radio_id=radio.radio_id,
            receiver_chain_ids=radio.receiver_chain_ids,
            gain=GainSetting(GainMode.AGC),
            sample_rate_hz=provisional_plan.sample_rate_hz,
            bandwidth_hz=provisional_plan.bandwidth_hz,
            sample_count=provisional_plan.sample_count,
            edge_order=provisional_plan.edge_order,
            lnb_lo_hz=provisional_plan.lnb_lo_hz,
            edge_order_draw_u32=provisional_plan.edge_order_draw_u32,
            arm_name=provisional_plan.arm_name,
            hardware_block_samples=provisional_plan.hardware_block_samples,
        )
    )
    second = V5CaptureStation(
        station_id=first.station_id,
        radio=radio,
        hardware_snapshot_id=first.hardware_snapshot_id,
        clock_status=first.clock_status,
        capture_implementation=first.capture_implementation,
        runtime_manifest=first.runtime_manifest,
        runtime_manifest_digest=first.runtime_manifest_digest,
        expected_runtime=first.expected_runtime,
        plan=replace(provisional_plan, plan_digest=canonical_digest(capture_plan)),
        state=replace(
            first.state,
            state_root=Path("/var/lib/leo-flow-v5-scan-21"),
            recording_root=Path("/var/lib/leo-flow-v5-scan-21/recordings"),
            spool_database=Path("/var/lib/leo-flow-v5-scan-21/capture-spool.sqlite3"),
            lock_path=Path("/run/leo-flow-v5-scan-21/instance.lock"),
        ),
    )

    require_disjoint_station_pair(first, second)
    assert first.state.cas_root == second.state.cas_root
    with pytest.raises(V5StationConfigurationError, match="collide"):
        require_disjoint_station_pair(first, replace(second, state=first.state))
    with pytest.raises(V5StationConfigurationError, match="mode lock divergence"):
        require_disjoint_station_pair(
            first,
            replace(
                second,
                state=replace(
                    second.state,
                    mode_lock_path=Path("/tmp/different-mode.lock"),
                ),
            ),
        )
    with pytest.raises(V5StationConfigurationError, match="CAS root divergence"):
        require_disjoint_station_pair(
            first,
            replace(
                second,
                state=replace(second.state, cas_root=Path("/tmp/different-cas")),
            ),
        )
