from __future__ import annotations

from leo_flow.adapters.dashboard_surrogate_distribution import _distribution


def test_qin_and_surrogate_distributions_keep_distinct_point_counts() -> None:
    qin = _distribution(
        ("glrt-32", "radio_a", "rx_a", "lower", "qin"),
        [("rec_a", 0.8), ("rec_b", 0.6)],
    )
    surrogate = _distribution(
        ("glrt-32", "radio_a", "rx_a", "lower", "surrogate"),
        [
            ("rec_a", 0.1),
            ("rec_a", 0.2),
            ("rec_a", 0.3),
            ("rec_a", 0.4),
            ("rec_b", 0.2),
            ("rec_b", 0.3),
            ("rec_b", 0.4),
            ("rec_b", 0.5),
        ],
    )

    assert qin.recording_count == surrogate.recording_count == 2
    assert qin.point_count == 2
    assert surrogate.point_count == 8
    assert sum(item.count for item in qin.bins) == 2
    assert sum(item.count for item in surrogate.bins) == 8
