from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.analysis.recording.starlink_events import (
    cluster_starlink_beacon_events_v0_1,
)
from leo_flow.contracts.core import (
    V0_1,
    ArtifactRef,
    Digest,
    RadioId,
    ReceiverChainId,
    SchemaRef,
    UtcNs,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_events import (
    StarlinkCalibratedDetectionV0_1,
    StarlinkCoincidenceBasis,
)


def _ref(prefix: str, suffix: str) -> ArtifactRef:
    return ArtifactRef(
        f"{prefix}_{suffix}",
        Digest.sha256(f"{prefix}-{suffix}".encode()),
        SchemaRef(f"org.leo-flow.{prefix}", V0_1),
    )


def _detection(
    suffix: str,
    *,
    radio: str,
    receiver: str,
    start: int,
    stop: int,
    cfo: float,
    tuning: str = "primary",
) -> StarlinkCalibratedDetectionV0_1:
    return StarlinkCalibratedDetectionV0_1(
        SchemaRef(StarlinkCalibratedDetectionV0_1.SCHEMA_ID, V0_1),
        f"sldetection_{suffix}",
        f"slcandidate_{suffix}",
        Digest.sha256(f"candidate-{suffix}".encode()),
        _ref("starlink-evaluation", suffix),
        _ref("starlink-calibration", f"{radio}-{receiver}"),
        RadioId(f"radio_{radio}"),
        ReceiverChainId(f"rx_{receiver}"),
        1,
        StarlinkEdge.LOWER,
        Digest.sha256(f"tuning-{tuning}".encode()),
        UtcNs(start),
        UtcNs(stop),
        cfo,
        0.4,
        0.25,
    )


def test_radios_receivers_tunings_and_duplicate_rows_form_one_beacon_event() -> None:
    detections = (
        _detection(
            "20_rx0_a", radio="pluto20", receiver="0", start=100, stop=200, cfo=1_000
        ),
        _detection(
            "20_rx1_b",
            radio="pluto20",
            receiver="1",
            start=105,
            stop=205,
            cfo=1_020,
            tuning="overlap",
        ),
        _detection(
            "21_rx0_c", radio="pluto21", receiver="0", start=110, stop=210, cfo=980
        ),
    )
    events = cluster_starlink_beacon_events_v0_1(
        detections + (detections[0],),
        maximum_gap_ns=20,
        maximum_cfo_span_hz=100,
    )

    assert len(events) == 1
    event = events[0]
    assert len(event.detection_refs) == 3
    assert len(event.candidate_ids) == 3
    assert event.radio_ids == (RadioId("radio_pluto20"), RadioId("radio_pluto21"))
    assert (
        event.coincidence_basis
        is StarlinkCoincidenceBasis.SOFTWARE_COORDINATED_MULTI_RADIO
    )
    assert event.satellite_association_status == "not_evaluated"


def test_time_cfo_channel_and_edge_boundaries_create_distinct_events() -> None:
    base = _detection(
        "base", radio="pluto20", receiver="0", start=100, stop=200, cfo=1_000
    )
    late = _detection(
        "late", radio="pluto20", receiver="0", start=300, stop=400, cfo=1_000
    )
    other_cfo = _detection(
        "other_cfo", radio="pluto20", receiver="0", start=110, stop=210, cfo=2_000
    )
    other_edge = replace(
        _detection(
            "other_edge", radio="pluto20", receiver="0", start=110, stop=210, cfo=1_000
        ),
        edge=StarlinkEdge.UPPER,
    )

    events = cluster_starlink_beacon_events_v0_1(
        (base, late, other_cfo, other_edge),
        maximum_gap_ns=20,
        maximum_cfo_span_hz=100,
    )
    assert len(events) == 4
    assert all(
        event.coincidence_basis is StarlinkCoincidenceBasis.SINGLE_STREAM
        for event in events
    )


def test_same_candidate_cannot_have_conflicting_detection_rows() -> None:
    detection = _detection(
        "same", radio="pluto20", receiver="0", start=100, stop=200, cfo=1_000
    )
    with pytest.raises(ValueError, match="conflicting"):
        cluster_starlink_beacon_events_v0_1(
            (detection, replace(detection, winning_cfo_hz=1_001)),
            maximum_gap_ns=20,
            maximum_cfo_span_hz=100,
        )


def test_uncalibrated_or_below_threshold_input_cannot_be_constructed() -> None:
    detection = _detection(
        "below", radio="pluto20", receiver="0", start=100, stop=200, cfo=1_000
    )
    with pytest.raises(ValueError, match="pass its cited threshold"):
        replace(detection, score=0.2)
