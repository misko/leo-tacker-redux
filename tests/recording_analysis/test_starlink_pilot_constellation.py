from __future__ import annotations

import json
from collections.abc import Iterator, Sequence

import pytest

from leo_flow.analysis.recording.starlink_detector_suite import (
    StarlinkDetectorSuiteConfigV0_2,
    StarlinkDetectorSuiteV0_2,
    StarlinkInjectionCaseV0_2,
    synthesize_starlink_injection_v0_2,
)
from leo_flow.analysis.recording.starlink_pilot_constellation import (
    StarlinkPilotConstellationAnalyzerV0_1,
    StarlinkPilotConstellationConfigV0_1,
)
from leo_flow.analysis.recording.starlink_pilot_constellation_codec import (
    MalformedStarlinkPilotConstellationError,
    decode_starlink_pilot_constellation,
    encode_starlink_pilot_constellation,
)
from leo_flow.analysis.recording.starlink_templates import (
    qin_edge_pilot_template_pair_v0_1,
)
from leo_flow.contracts.core import Digest, ReceiverChainId, RecordingId, SegmentId
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_pilot_constellation import MAX_CONSTELLATION_POINTS

from .fakes import execution_context

SAMPLE_RATE_HZ = 2_500_000.0


def _fixture() -> tuple[tuple[complex, ...], object]:
    templates = qin_edge_pilot_template_pair_v0_1(SAMPLE_RATE_HZ, StarlinkEdge.LOWER)
    case = StarlinkInjectionCaseV0_2(
        "qam-oracle",
        901,
        14_000,
        2.0,
        0.025,
        3,
        1_000.0,
        0.0,
        (0, 1, 2, 3),
    )
    samples = synthesize_starlink_injection_v0_2(templates, case)
    suite = StarlinkDetectorSuiteV0_2(
        StarlinkDetectorSuiteConfigV0_2(
            (0, 3, 6),
            (0.0, 1_000.0),
            (-100.0, 0.0, 100.0),
        ),
        execution_context(),
    ).analyze_receiver(
        samples,
        recording_id=RecordingId("rec_qam_oracle"),
        recording_identity_digest=Digest.sha256(b"qam-oracle-recording"),
        segment_id=SegmentId("seg_qam_oracle"),
        receiver_chain_id=ReceiverChainId("rx_qam_oracle"),
        templates=templates,
    )
    return samples, suite


def _analyze(samples: Sequence[complex], suite: object):
    return StarlinkPilotConstellationAnalyzerV0_1(
        StarlinkPilotConstellationConfigV0_1(maximum_probe_samples=20_000),
        execution_context(),
    ).analyze(samples, suite)  # type: ignore[arg-type]


def test_constellation_binds_acquire_and_labels_scope() -> None:
    samples, suite = _fixture()
    evidence = _analyze(samples, suite)

    assert evidence.source_suite_digest == suite.digest
    assert evidence.winning_epoch_sample == 3
    assert evidence.winning_coarse_cfo_hz == 1_000.0
    assert (
        evidence.observation_count == len(evidence.points) == MAX_CONSTELLATION_POINTS
    )
    assert evidence.complete_frame_count == 4
    assert evidence.hard_symbol_accuracy > 0.99
    assert evidence.rms_evm < 0.2
    assert evidence.candidate_only is True
    assert evidence.known_synchronization_pilot is True
    assert evidence.payload_decoded is False
    assert [item.subcarrier_index for item in evidence.subcarriers] == list(
        range(528, 536)
    )


def test_fixed_reference_oracle_metrics_do_not_drift() -> None:
    """Pinned from leo-tracker decode.py; reference is not a runtime dependency."""

    samples, suite = _fixture()
    evidence = _analyze(samples, suite)

    # These literals are an intentional oracle fixture, not regenerated output.
    assert evidence.residual_cfo_refinement_hz == pytest.approx(-0.189117, abs=1e-5)
    assert evidence.hard_symbol_accuracy == pytest.approx(1.0, abs=1e-12)
    assert evidence.effective_frame_count == pytest.approx(4.0, rel=2e-3)


class _ObservedSequence(Sequence[complex]):
    def __init__(self, values: tuple[complex, ...]) -> None:
        self._values = values
        self.maximum_slice = 0

    def __len__(self) -> int:
        return len(self._values)

    def __getitem__(self, key: int | slice) -> complex | tuple[complex, ...]:
        if isinstance(key, slice):
            start, stop, step = key.indices(len(self))
            self.maximum_slice = max(self.maximum_slice, len(range(start, stop, step)))
        return self._values[key]

    def __iter__(self) -> Iterator[complex]:
        raise AssertionError("analyzer must not materialize the complete input")


def test_streaming_working_set_never_requests_a_full_sample_copy() -> None:
    samples, suite = _fixture()
    observed = _ObservedSequence(samples)

    evidence = _analyze(observed, suite)

    assert evidence.complete_frame_count == 4
    assert observed.maximum_slice <= 12


def test_codec_is_canonical_strict_and_bounded() -> None:
    samples, suite = _fixture()
    evidence = _analyze(samples, suite)
    payload = encode_starlink_pilot_constellation(evidence)

    assert decode_starlink_pilot_constellation(payload) == evidence
    value = json.loads(payload)
    value["payload_decoded"] = True
    with pytest.raises(MalformedStarlinkPilotConstellationError):
        decode_starlink_pilot_constellation(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        )
    value = json.loads(payload)
    value["surprise"] = 1
    with pytest.raises(MalformedStarlinkPilotConstellationError):
        decode_starlink_pilot_constellation(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        )


def test_resource_and_source_identity_guards_fail_closed() -> None:
    samples, suite = _fixture()
    analyzer = StarlinkPilotConstellationAnalyzerV0_1(
        StarlinkPilotConstellationConfigV0_1(maximum_probe_samples=10),
        execution_context(),
    )
    with pytest.raises(ValueError, match="configured bound"):
        analyzer.analyze(samples, suite)
    with pytest.raises(ValueError, match="sample count"):
        _analyze(samples[:-1], suite)
