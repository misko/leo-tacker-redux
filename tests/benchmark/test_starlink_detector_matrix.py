from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import benchmark.starlink_detector_matrix as detector_matrix
from benchmark.starlink_detector_matrix import (
    METHOD_IDS,
    PER_SNR_INTERPRETATION,
    _condition_arms,
    expand_cases,
    load_matrix_spec,
)
from leo_flow.analysis.dataset import DatasetSplit


def test_benchmark_runtime_has_no_test_only_imports() -> None:
    modules = (
        detector_matrix,
        __import__("benchmark.starlink_e2e_calibration", fromlist=("*",)),
        __import__("benchmark.starlink_scan_fixture", fromlist=("*",)),
    )
    for module in modules:
        path = Path(module.__file__ or "")
        imported_modules: list[str] = []
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_modules.append(node.module)
        assert not any(
            name == "tests" or name.startswith("tests.") for name in imported_modules
        )


def test_frozen_matrix_has_explicit_group_safe_partitions() -> None:
    spec = load_matrix_spec()
    cases = expand_cases(spec)

    assert len(spec.groups) == 24
    assert Counter(group.split for group in spec.groups) == {
        DatasetSplit.TRAIN: 12,
        DatasetSplit.VALIDATION: 6,
        DatasetSplit.LOCKED_TEST: 6,
    }
    assert len(cases) == 144
    assert len({case.case_id for case in cases}) == len(cases)
    for group in spec.groups:
        members = tuple(case for case in cases if case.group == group)
        assert len(members) == 6
        assert sum(not case.condition.signal_present for case in members) == 1
        assert {case.group.split for case in members} == {group.split}


def test_matrix_covers_predeclared_signal_and_receiver_dimensions() -> None:
    spec = load_matrix_spec()
    cases = expand_cases(spec)
    positives = tuple(case for case in cases if case.condition.signal_present)

    assert {case.condition.snr_db for case in positives} == {
        -18.0,
        -10.0,
        -2.0,
        6.0,
        14.0,
    }
    assert {case.edge for case in positives} == {"lower", "upper"}
    assert {case.condition.pilot_subset for case in positives} == {"inner", "full"}
    assert {len(case.pilot_indices) for case in positives} == {2, 8}
    assert {case.condition.cfo_hz for case in positives} == {
        -60_000.0,
        -30_000.0,
        0.0,
        30_000.0,
        60_000.0,
    }
    assert {case.target_channel for case in positives} == {1, 2, 3, 4}
    assert {case.group.second_receiver.integer_delay_samples for case in positives} == {
        0,
        1,
        2,
        3,
        4,
    }
    assert len({case.group.second_receiver.gain_linear for case in positives}) == 5
    assert len({case.group.second_receiver.phase_offset_rad for case in positives}) == 5
    near = tuple(case for case in positives if case.condition.near_clipping)
    assert len(near) == 24
    assert all(case.condition.snr_db == 14.0 for case in near)
    assert all(case.condition.pilot_subset == "full" for case in near)
    assert METHOD_IDS == tuple(sorted(METHOD_IDS))


def test_per_snr_results_declare_composite_condition_arms() -> None:
    arms = _condition_arms(load_matrix_spec())

    assert "not an isolated SNR response curve" in PER_SNR_INTERPRETATION
    assert set(arms) == {"-18", "-10", "-2", "6", "14"}
    assert arms["-18"] == [
        {
            "condition_id": "snr_m18_inner",
            "pilot_subset": "inner",
            "edge_flip": False,
            "cfo_hz": -60_000.0,
            "target_channel_offset": 0,
            "near_clipping": False,
        }
    ]
    assert arms["14"] == [
        {
            "condition_id": "snr_p14_full_near_clip",
            "pilot_subset": "full",
            "edge_flip": False,
            "cfo_hz": 60_000.0,
            "target_channel_offset": 0,
            "near_clipping": True,
        }
    ]


def test_spec_and_case_expansion_are_deterministic() -> None:
    first = load_matrix_spec()
    second = load_matrix_spec()

    assert first == second
    assert first.digest == second.digest
    assert expand_cases(first) == expand_cases(second)
    assert first.detector_config.window_samples == first.sample_count == 4096
    assert first.detector_config.stride_samples == first.sample_count
    assert first.detector_config.clip_threshold_abs == first.converter_max + 1
