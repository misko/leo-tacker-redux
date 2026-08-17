from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

from leo_flow.capture.v5_station import (
    V5CaptureStation,
    load_v5_capture_station,
    require_disjoint_station_pair,
)
from leo_flow.contracts.capture_batch import (
    CaptureBatchDefinition,
    CaptureBatchMode,
    ExpectedCaptureAttempt,
)
from leo_flow.contracts.core import (
    CaptureAttemptId,
    CaptureBatchId,
    Digest,
    DigestAlgorithm,
    SchemaRef,
    UtcNs,
    canonical_digest,
)
from leo_flow.deployments.v5_dual_capture_operator import ExitCode, _pair_digest, main

RADIO_20_PATH = Path("deploy/v5-scan/gauss-radio-20-pluto-5d4d.station.json")
RADIO_21_PATH = Path("deploy/v5-scan/gauss-radio-21-pluto-19f2.station.json")
DUAL_RADIO_20_PATH = Path(
    "deploy/v5-scan/gauss-dual-independent-radio-20-pluto-5d4d.station.json"
)
DUAL_RADIO_21_PATH = Path(
    "deploy/v5-scan/gauss-dual-independent-radio-21-pluto-19f2.station.json"
)
DUAL_ATTEMPT2_RADIO_20_PATH = Path(
    "deploy/v5-scan/gauss-dual-independent-attempt2-radio-20-pluto-5d4d.station.json"
)
DUAL_ATTEMPT2_RADIO_21_PATH = Path(
    "deploy/v5-scan/gauss-dual-independent-attempt2-radio-21-pluto-19f2.station.json"
)
RX_INTEGRITY_RADIO_20_PATH = Path(
    "deploy/v5-scan/gauss-rx-integrity-candidate1-radio-20-pluto-5d4d.station.json"
)
RX_INTEGRITY_RADIO_21_PATH = Path(
    "deploy/v5-scan/gauss-rx-integrity-candidate1-radio-21-pluto-19f2.station.json"
)
HOSTPRIME_OLDRADIO_RADIO_20_PATH = Path(
    "deploy/v5-scan/gauss-hostprime-oldradio-radio-20-pluto-5d4d.diagnostic.station.json"
)
HOSTPRIME_POSTREBOOT_RADIO_20_PATHS = (
    Path(
        "deploy/v5-scan/gauss-hostprime-oldradio-postreboot-radio-20-pluto-5d4d.diagnostic.station.json"
    ),
    Path(
        "deploy/v5-scan/gauss-hostprime-oldradio-postreboot-5m-radio-20-pluto-5d4d.diagnostic.station.json"
    ),
)
RUNTIME_MANIFEST = Path(
    "/home/mouse9911/gits/leo-tracker-redux/deploy/v5-runtime/"
    "gauss-development.manifest.json"
)
RUNTIME_MANIFEST_DIGEST = Digest(
    DigestAlgorithm.SHA256,
    "1544c390d66a2a53c9b86dc0cf7a2fab63e9fca0a08563638744121b107f431f",
)
SHARED_CAS = Path("/home/mouse9911/.local/share/leo-flow/objects")
SHARED_MODE_LOCK = Path("/home/mouse9911/.local/state/leo-flow/pipeline-mode.lock")


def _stations() -> tuple[V5CaptureStation, V5CaptureStation]:
    return (
        load_v5_capture_station(RADIO_20_PATH),
        load_v5_capture_station(RADIO_21_PATH),
    )


def _dual_stations() -> tuple[V5CaptureStation, V5CaptureStation]:
    return (
        load_v5_capture_station(DUAL_RADIO_20_PATH),
        load_v5_capture_station(DUAL_RADIO_21_PATH),
    )


def _dual_attempt2_stations() -> tuple[V5CaptureStation, V5CaptureStation]:
    return (
        load_v5_capture_station(DUAL_ATTEMPT2_RADIO_20_PATH),
        load_v5_capture_station(DUAL_ATTEMPT2_RADIO_21_PATH),
    )


def _rx_integrity_stations() -> tuple[V5CaptureStation, V5CaptureStation]:
    return (
        load_v5_capture_station(RX_INTEGRITY_RADIO_20_PATH),
        load_v5_capture_station(RX_INTEGRITY_RADIO_21_PATH),
    )


def _definition(
    stations: tuple[V5CaptureStation, V5CaptureStation],
) -> CaptureBatchDefinition:
    return CaptureBatchDefinition(
        SchemaRef(CaptureBatchDefinition.SCHEMA_ID),
        CaptureBatchId("cbatch_gauss_candidate_identity_test"),
        CaptureBatchMode.INDEPENDENT,
        tuple(
            ExpectedCaptureAttempt(
                CaptureAttemptId(f"cattempt_gauss_candidate_{index}"),
                station.radio.radio_id,
                station.plan.plan_id,
                UtcNs(index * 1_000),
            )
            for index, station in enumerate(stations, start=1)
        ),
    )


def test_checked_gauss_candidates_load_with_exact_immutable_identity() -> None:
    radio_20, radio_21 = _stations()

    assert (
        radio_20.radio.uri,
        radio_20.radio.expected_serial,
        str(radio_20.radio.radio_id),
        tuple(map(str, radio_20.radio.receiver_chain_ids)),
        str(radio_20.plan.plan_id),
        str(radio_20.plan.plan_digest),
        str(radio_20.specification_digest),
    ) == (
        "ip:192.168.1.20",
        "1040005e0b100007100010000bf33a5d4d",
        "radio_pluto_5d4d",
        ("rx_lnb_a", "rx_lnb_b"),
        "plan_v5_scan_pluto_5d4d_20260815_v1",
        "sha256:df1fe7e3bfb38cec07afa067c6347425cf283294d1745a49f067c485c8a3ed9a",
        "sha256:860a1fb319b638c13efdebdf4f9514def4ec0a288ca1c3d0bccf2967cc966319",
    )
    assert (
        radio_21.radio.uri,
        radio_21.radio.expected_serial,
        str(radio_21.radio.radio_id),
        tuple(map(str, radio_21.radio.receiver_chain_ids)),
        str(radio_21.plan.plan_id),
        str(radio_21.plan.plan_digest),
        str(radio_21.specification_digest),
    ) == (
        "ip:192.168.1.21",
        "10400056f695001322002d0010ad1719f2",
        "radio_pluto_19f2",
        ("rx_lnb_c", "rx_lnb_d"),
        "plan_v5_scan_pluto_19f2_20260815_v1",
        "sha256:3f75935fcad427fef273fa44ad42d9f57e2f1390c8933268d3ed4244245b57a5",
        "sha256:42e0e27718f0efd4ea05fd3c8b511922ef9b9f9aabc03e6b376d659437c266f9",
    )
    for station in (radio_20, radio_21):
        assert canonical_digest(station.capture_plan()) == station.plan.plan_digest
        assert station.runtime_manifest == RUNTIME_MANIFEST
        assert station.runtime_manifest_digest == RUNTIME_MANIFEST_DIGEST
        assert station.radio.firmware_release == (
            "v0.38-plutoplus-spf-libiio-metadata-v5"
        )
        assert station.radio.firmware_commit == (
            "d7c87a9a28094ee6f0b23cb47df9ff737b5a69d8"
        )


def test_checked_gauss_candidates_form_one_disjoint_shared_storage_pair() -> None:
    radio_20, radio_21 = _stations()

    require_disjoint_station_pair(radio_20, radio_21)
    assert radio_20.state.cas_root == radio_21.state.cas_root == SHARED_CAS
    assert (
        radio_20.state.mode_lock_path
        == radio_21.state.mode_lock_path
        == SHARED_MODE_LOCK
    )
    assert radio_20.state.require_cas_mount is False
    assert radio_21.state.require_cas_mount is False
    assert radio_20.state.state_root == Path(
        "/home/mouse9911/.local/state/leo-flow/v5-scan/pluto-5d4d"
    )
    assert radio_21.state.state_root == Path(
        "/home/mouse9911/.local/state/leo-flow/v5-scan/pluto-19f2"
    )
    for field in ("state_root", "recording_root", "spool_database", "lock_path"):
        assert getattr(radio_20.state, field) != getattr(radio_21.state, field)


def test_rx_integrity_candidate_pair_binds_fresh_runtime_firmware_and_state() -> None:
    radio_20, radio_21 = _rx_integrity_stations()

    require_disjoint_station_pair(radio_20, radio_21)
    assert (
        str(radio_20.plan.plan_digest),
        str(radio_20.specification_digest),
    ) == (
        "sha256:33d00b74b5bb9ddadd41d4a7a79195f7bb306b66b8631e28dc6cababbfa9ad50",
        "sha256:a9febb7ef8cfdce1f0adc6521a580261710e9d900d347e0e5930d9ddeb12c97c",
    )
    assert (
        str(radio_21.plan.plan_digest),
        str(radio_21.specification_digest),
    ) == (
        "sha256:e9889626bd8d5d138fbb556228b42692fa11a8554681018305f75a2b04419b80",
        "sha256:1404f32470617fd63f55609dc9353aaa4f79995566d388da56657775a89b1d8c",
    )
    old_radio_20, old_radio_21 = _stations()
    for candidate, old in zip(
        (radio_20, radio_21),
        (old_radio_20, old_radio_21),
        strict=True,
    ):
        assert candidate.radio.radio_id == old.radio.radio_id
        assert candidate.radio.expected_serial == old.radio.expected_serial
        assert candidate.radio.firmware_release == (
            "v0.38-plutoplus-spf-libiio-metadata-v5-rx-integrity-candidate1"
        )
        assert candidate.expected_runtime.runtime_id == (
            "gauss-pluto-v5-rx-integrity-close-barrier-1"
        )
        assert candidate.runtime_manifest_digest == Digest(
            DigestAlgorithm.SHA256,
            "0a9cf278bf836655afbf7a9a324a21c5dc41235d1b251386a0013eb0f299f123",
        )
        assert candidate.plan.plan_id != old.plan.plan_id
        assert candidate.state.state_root != old.state.state_root
        assert candidate.plan.sample_rate_hz == 2_500_000.0
        assert candidate.plan.sample_count == 100_000
        assert candidate.plan.hardware_block_samples == 100_000
    assert radio_20.state.cas_root == radio_21.state.cas_root == SHARED_CAS
    assert (
        radio_20.state.mode_lock_path
        == radio_21.state.mode_lock_path
        == SHARED_MODE_LOCK
    )


def test_hostprime_diagnostic_is_fresh_and_keeps_old_radio_identity() -> None:
    diagnostic = load_v5_capture_station(HOSTPRIME_OLDRADIO_RADIO_20_PATH)
    old_radio_20, _ = _stations()

    assert diagnostic.radio == old_radio_20.radio
    assert diagnostic.expected_runtime.runtime_id == (
        "gauss-pluto-v5-rx-integrity-close-barrier-1"
    )
    assert diagnostic.plan.plan_id != old_radio_20.plan.plan_id
    assert diagnostic.state.state_root != old_radio_20.state.state_root
    assert str(diagnostic.plan.plan_digest) == (
        "sha256:651c9443c5baff6a63c1b7b727f04feecdd416c78cadb1f4ba42d7fadd19e419"
    )
    assert diagnostic.plan.sample_rate_hz == 2_500_000.0
    assert diagnostic.plan.sample_count == 100_000
    assert diagnostic.plan.hardware_block_samples == 100_000


def test_postreboot_diagnostics_are_fresh_exact_terminal_evidence() -> None:
    before_reboot = load_v5_capture_station(HOSTPRIME_OLDRADIO_RADIO_20_PATH)
    lower_rate, supported_rate = tuple(
        load_v5_capture_station(path) for path in HOSTPRIME_POSTREBOOT_RADIO_20_PATHS
    )

    assert lower_rate.radio == supported_rate.radio == before_reboot.radio
    assert {
        str(lower_rate.plan.plan_id),
        str(supported_rate.plan.plan_id),
        str(before_reboot.plan.plan_id),
    } == {
        "plan_v5_hostprime_oldradio_pluto_5d4d_20260816_d1",
        "plan_v5_hostprime_oldradio_pluto_5d4d_20260816_d2",
        "plan_v5_hostprime_oldradio_pluto_5d4d_20260816_d3",
    }
    assert str(lower_rate.plan.plan_digest) == (
        "sha256:5abfb66e5bf40b9b4e54856a409e5000bb1b5338f935379646f03f4e1a944838"
    )
    assert str(supported_rate.plan.plan_digest) == (
        "sha256:c17052be3f36a8d121aa90c4b3acf5b8886f9ccd2d8430a94899cfe711a6fc97"
    )
    assert str(lower_rate.specification_digest) == (
        "sha256:27d004c7ce8ec29c7cde811f532f6265b48208807ef1b39323edb21a085d73db"
    )
    assert str(supported_rate.specification_digest) == (
        "sha256:d816abecfe1e74c03dafd5acb16c9b0c3b1de7426b6091ba62fe09c38aa9ef27"
    )
    assert lower_rate.plan.sample_rate_hz == 2_500_000.0
    assert (
        lower_rate.plan.sample_count
        == lower_rate.plan.hardware_block_samples
        == 100_000
    )
    assert supported_rate.plan.sample_rate_hz == 5_000_000.0
    assert (
        supported_rate.plan.sample_count
        == supported_rate.plan.hardware_block_samples
        == 200_000
    )
    assert (
        len(
            {
                before_reboot.state.state_root,
                lower_rate.state.state_root,
                supported_rate.state.state_root,
            }
        )
        == 3
    )


def test_checked_independent_dual_pair_has_exact_new_plan_and_station_digests() -> None:
    radio_20, radio_21 = _dual_stations()

    assert (
        str(radio_20.plan.plan_id),
        str(radio_20.plan.plan_digest),
        str(radio_20.specification_digest),
    ) == (
        "plan_v5_dual_independent_pluto_5d4d_20260815_v1",
        "sha256:a5a069532b9a83f9b6d54d2b70a86ca4c5b17ed94d8eefacacf18cadd0650c14",
        "sha256:f03d253310cec3f2306b4aa51fb26c1a575d96921096d1bbfab599099c0877d7",
    )
    assert (
        str(radio_21.plan.plan_id),
        str(radio_21.plan.plan_digest),
        str(radio_21.specification_digest),
    ) == (
        "plan_v5_dual_independent_pluto_19f2_20260815_v1",
        "sha256:547b83e59bd5f76e7f2cf4ebf9f0392c39750323aba6969fe1d468b1979769ab",
        "sha256:52cb027fa5e3f5a66e5cf8793a3d63dc9d7479fafd06da01764575dffa52ae27",
    )
    for station in (radio_20, radio_21):
        assert canonical_digest(station.capture_plan()) == station.plan.plan_digest


def test_independent_dual_pair_is_disjoint_and_cannot_replay_single_canaries() -> None:
    single_stations = _stations()
    dual_stations = _dual_stations()

    require_disjoint_station_pair(*dual_stations)
    assert {item.plan.plan_id for item in dual_stations}.isdisjoint(
        item.plan.plan_id for item in single_stations
    )
    assert {item.plan.plan_digest for item in dual_stations}.isdisjoint(
        item.plan.plan_digest for item in single_stations
    )
    for single, dual in zip(single_stations, dual_stations, strict=True):
        assert dual.radio == single.radio
        assert dual.hardware_snapshot_id == single.hardware_snapshot_id
        assert dual.expected_runtime == single.expected_runtime
        assert dual.runtime_manifest_digest == single.runtime_manifest_digest
        for field in ("state_root", "recording_root", "spool_database", "lock_path"):
            assert getattr(dual.state, field) != getattr(single.state, field)
    assert (
        dual_stations[0].state.cas_root == dual_stations[1].state.cas_root == SHARED_CAS
    )
    assert (
        dual_stations[0].state.mode_lock_path
        == dual_stations[1].state.mode_lock_path
        == SHARED_MODE_LOCK
    )


def test_independent_dual_plans_pin_new_activity_segments_and_one_refill() -> None:
    single_stations = _stations()
    dual_stations = _dual_stations()
    single_activities = {
        activity.activity_id
        for station in single_stations
        for activity in station.capture_plan().activities
    }
    single_segments = {
        segment.segment_id
        for station in single_stations
        for activity in station.capture_plan().activities
        for segment in activity.segments
    }

    for station, radio_suffix in zip(dual_stations, ("5d4d", "19f2"), strict=True):
        plan = station.capture_plan()
        assert len(plan.activities) == 1
        activity = plan.activities[0]
        assert str(activity.activity_id) == (
            f"act_plan_v5_dual_independent_pluto_{radio_suffix}_20260815_v1_scan"
        )
        assert activity.activity_id not in single_activities
        assert tuple(str(segment.segment_id) for segment in activity.segments) == tuple(
            f"seg_plan_v5_dual_independent_pluto_{radio_suffix}_20260815_v1_"
            f"{index:02d}_ch{channel}_{edge}"
            for index, (channel, edge) in enumerate(
                (
                    (1, "lower"),
                    (1, "upper"),
                    (2, "lower"),
                    (2, "upper"),
                    (3, "lower"),
                    (3, "upper"),
                    (4, "lower"),
                    (4, "upper"),
                )
            )
        )
        assert all(
            segment.segment_id not in single_segments
            and segment.receiver_chain_ids == station.radio.receiver_chain_ids
            and segment.sample_count == station.plan.sample_count == 262_144
            for segment in activity.segments
        )
        assert station.plan.hardware_block_samples == station.plan.sample_count

    assert (
        tuple(
            (segment.center_frequency_hz, segment.sample_rate_hz, segment.bandwidth_hz)
            for segment in dual_stations[0].capture_plan().activities[0].segments
        )
        == tuple(
            (segment.center_frequency_hz, segment.sample_rate_hz, segment.bandwidth_hz)
            for segment in dual_stations[1].capture_plan().activities[0].segments
        )
        == tuple(
            (segment.center_frequency_hz, segment.sample_rate_hz, segment.bandwidth_hz)
            for segment in single_stations[0].capture_plan().activities[0].segments
        )
    )


def test_checked_attempt2_pair_is_fresh_exact_and_disjoint_from_prior_runs() -> None:
    singles = _stations()
    attempt1 = _dual_stations()
    attempt2 = _dual_attempt2_stations()

    require_disjoint_station_pair(*attempt2)
    assert (
        str(attempt2[0].plan.plan_id),
        str(attempt2[0].plan.plan_digest),
        str(attempt2[0].specification_digest),
    ) == (
        "plan_v5_dual_independent_attempt2_pluto_5d4d_20260815_v1",
        "sha256:04575e5cfe491fec0afe5165bd39e1b034872b518b30a9976ef8f241a25ad27e",
        "sha256:2e35f6f92161ffb1b766b02889a07a4e9137b948ba90f9d2e26cb404062b3314",
    )
    assert (
        str(attempt2[1].plan.plan_id),
        str(attempt2[1].plan.plan_digest),
        str(attempt2[1].specification_digest),
    ) == (
        "plan_v5_dual_independent_attempt2_pluto_19f2_20260815_v1",
        "sha256:eaf951c634ff14946cc73a4c7799d786dcba3a7727b5484def4ea5738c726b01",
        "sha256:b12a5365086f115c71f5ff55297623b6f23a2edb147fb83e26e6d79f656a2fc3",
    )
    prior = singles + attempt1
    assert {item.plan.plan_id for item in attempt2}.isdisjoint(
        item.plan.plan_id for item in prior
    )
    assert {item.plan.plan_digest for item in attempt2}.isdisjoint(
        item.plan.plan_digest for item in prior
    )
    for station, prior_station in zip(attempt2, attempt1, strict=True):
        assert station.radio == prior_station.radio
        assert station.expected_runtime == prior_station.expected_runtime
        assert station.runtime_manifest_digest == prior_station.runtime_manifest_digest
        assert canonical_digest(station.capture_plan()) == station.plan.plan_digest
        assert (
            station.plan.sample_count == station.plan.hardware_block_samples == 262_144
        )
        assert len(station.capture_plan().activities[0].segments) == 8
        for field in ("state_root", "recording_root", "spool_database", "lock_path"):
            assert getattr(station.state, field) not in {
                getattr(item.state, field) for item in prior
            }
    assert attempt2[0].state.cas_root == attempt2[1].state.cas_root == SHARED_CAS
    assert (
        attempt2[0].state.mode_lock_path
        == attempt2[1].state.mode_lock_path
        == SHARED_MODE_LOCK
    )


def test_attempt2_pair_preserves_tunings_but_not_activity_or_segment_ids() -> None:
    attempt1 = _dual_stations()
    attempt2 = _dual_attempt2_stations()

    for old, new in zip(attempt1, attempt2, strict=True):
        old_activity = old.capture_plan().activities[0]
        new_activity = new.capture_plan().activities[0]
        assert new_activity.activity_id != old_activity.activity_id
        assert {item.segment_id for item in new_activity.segments}.isdisjoint(
            item.segment_id for item in old_activity.segments
        )
        assert tuple(
            (
                item.center_frequency_hz,
                item.sample_rate_hz,
                item.bandwidth_hz,
                item.sample_count,
            )
            for item in new_activity.segments
        ) == tuple(
            (
                item.center_frequency_hz,
                item.sample_rate_hz,
                item.bandwidth_hz,
                item.sample_count,
            )
            for item in old_activity.segments
        )


def test_attempt2_pair_passes_operator_validation_without_runtime_io() -> None:
    stations = _dual_attempt2_stations()
    definition = _definition(stations)
    forbidden = lambda *_values: (_ for _ in ()).throw(AssertionError("unexpected I/O"))
    stdout, stderr = StringIO(), StringIO()

    code = main(
        [
            "validate",
            "--station-a",
            str(DUAL_ATTEMPT2_RADIO_20_PATH),
            "--station-b",
            str(DUAL_ATTEMPT2_RADIO_21_PATH),
            "--batch",
            "unused.json",
        ],
        stdout=stdout,
        stderr=stderr,
        batch_loader=lambda _path: definition,
        store_factory=forbidden,
        credential_factory=forbidden,
        mode_lock_factory=forbidden,
        runner_builder=forbidden,
        publisher_builder=forbidden,
        drain_gate_builder=forbidden,
        process_supervisor_factory=forbidden,
    )

    assert code == ExitCode.OK
    assert stderr.getvalue() == ""
    payload = json.loads(stdout.getvalue())
    assert payload["event"] == "dual_configuration_valid"
    assert [item["plan_id"] for item in payload["radios"]] == [
        str(item.plan.plan_id) for item in stations
    ]


def test_swapped_serial_arm_confirmation_rejects_before_any_runtime_io(
    tmp_path: Path,
) -> None:
    stations = _stations()
    definition = _definition(stations)
    forbidden = lambda *_values: (_ for _ in ()).throw(AssertionError("unexpected I/O"))
    stdout, stderr = StringIO(), StringIO()

    code = main(
        [
            "capture",
            "--station-a",
            str(RADIO_20_PATH),
            "--station-b",
            str(RADIO_21_PATH),
            "--batch",
            "unused.json",
            "--arm",
            "--confirm-analysis-stopped",
            "--confirm-radio-a-serial",
            stations[1].radio.expected_serial,
            "--confirm-radio-b-serial",
            stations[0].radio.expected_serial,
            "--confirm-batch-digest",
            str(canonical_digest(definition)),
            "--confirm-pair-digest",
            str(_pair_digest(definition, stations)),
            "--credential-directory",
            str(tmp_path),
            "--batch-database",
            str(tmp_path / "batch.sqlite3"),
        ],
        stdout=stdout,
        stderr=stderr,
        batch_loader=lambda _path: definition,
        store_factory=forbidden,
        credential_factory=forbidden,
        mode_lock_factory=forbidden,
        runner_builder=forbidden,
        publisher_builder=forbidden,
        drain_gate_builder=forbidden,
    )

    assert code == ExitCode.ARM_REJECTED
    assert stdout.getvalue() == ""
    assert json.loads(stderr.getvalue()) == {"event": "dual_capture_arm_rejected"}
