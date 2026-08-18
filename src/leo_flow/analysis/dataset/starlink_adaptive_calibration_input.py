"""Offline assembly of adaptive response/QAM products for calibration v0.1."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from leo_flow.analysis.qam_goodness import qam_goodness_v0_2
from leo_flow.contracts.core import V0_1, Digest, SchemaRef, canonical_digest
from leo_flow.contracts.starlink_adaptive_calibration import (
    AdaptiveCalibrationDwellV0_1,
    AdaptiveCalibrationLabel,
    AdaptivePatternDwellEvidenceV0_1,
    AdaptivePatternRole,
    AdaptiveReceiverPatternEvidenceV0_1,
)
from leo_flow.contracts.starlink_adaptive_calibration_input import (
    AdaptiveCalibrationAssemblySpecV0_1,
    AdaptiveCalibrationEvidencePurpose,
    AdaptiveConditionedPositivePlumbingV0_1,
    AssembledAdaptiveCalibrationInputV0_1,
)
from leo_flow.contracts.starlink_adaptive_qam import StarlinkAdaptiveQamBundleV0_4
from leo_flow.contracts.starlink_adaptive_response import (
    StarlinkAdaptiveResponseBundleV0_1,
    StarlinkAdaptiveResponsePointV0_1,
    StarlinkAdaptiveResponseStreamV0_1,
)
from leo_flow.contracts.starlink_detector_suite import StarlinkDetectorMethod
from leo_flow.contracts.starlink_pattern_symmetric_qam import (
    PatternSymmetricAdaptiveQamBundleV0_5,
)


def adaptive_calibration_pattern_templates_v0_1(
    response: StarlinkAdaptiveResponseBundleV0_1,
    method: StarlinkDetectorMethod,
) -> tuple[Digest, ...]:
    """Return Qin plus the stable, ordered surrogate bank for one method."""

    memberships: set[tuple[Digest, ...]] = set()
    for stream in response.streams:
        declared = {item.digest for item in stream.selection.pattern_refs}
        for point in _method_points(stream, method):
            surrogates = tuple(item.template_digest for item in point.surrogates)
            qin = declared.difference(surrogates)
            if len(qin) != 1 or declared != {*qin, *surrogates}:
                raise ValueError(
                    "declared adaptive patterns differ from scored membership"
                )
            memberships.add((*qin, *surrogates))
    if len(memberships) != 1:
        raise ValueError("Qin/surrogate template membership changed within the dwell")
    return memberships.pop()


def adaptive_calibration_search_identity_v0_1(
    response: StarlinkAdaptiveResponseBundleV0_1,
    method: StarlinkDetectorMethod,
) -> Digest:
    """Identify the exact search without incorporating mutable LNB labels."""

    templates = adaptive_calibration_pattern_templates_v0_1(response, method)
    geometries = {
        (
            point.qin.search_mode,
            point.qin.aggregation,
            point.qin.effective_search_cell_count,
        )
        for stream in response.streams
        for point in _method_points(stream, method)
    }
    return canonical_digest(
        {
            "schema": "adaptive-calibration-search-identity-v0.1",
            "method": method,
            "source_suite": response.source_suite_ref,
            "search_grid": response.search_grid,
            "adaptive_plan": response.plan,
            "pattern_template_digests": templates,
            "search_geometries": tuple(sorted(geometries, key=str)),
            "score_correction": "none",
        }
    )


def assemble_adaptive_calibration_input_v0_1(
    spec: AdaptiveCalibrationAssemblySpecV0_1,
    response: StarlinkAdaptiveResponseBundleV0_1,
    qam: StarlinkAdaptiveQamBundleV0_4 | None = None,
) -> AssembledAdaptiveCalibrationInputV0_1:
    """Assemble one frozen member, retaining zero-score/no-candidate receivers."""

    if spec.purpose is not AdaptiveCalibrationEvidencePurpose.CALIBRATION:
        raise ValueError("conditioned positives are not calibration members")
    if response.digest != spec.response_bundle_digest:
        raise ValueError("adaptive response differs from frozen member")
    if (qam.digest if qam is not None else None) != spec.qam_bundle_digest:
        raise ValueError("adaptive QAM differs from frozen member")
    if qam is not None and spec.label is AdaptiveCalibrationLabel.NULL:
        raise ValueError("null calibration requires pattern-symmetric QAM evidence")
    templates = adaptive_calibration_pattern_templates_v0_1(response, spec.method)
    search_identity = adaptive_calibration_search_identity_v0_1(response, spec.method)
    if templates != spec.pattern_template_digests:
        raise ValueError("pattern bank differs from frozen member")
    if search_identity != spec.search_identity_digest:
        raise ValueError("adaptive search differs from frozen member")

    ordered_streams = tuple(
        sorted(
            response.streams,
            key=lambda item: (str(item.radio_id), str(item.receiver_chain_id)),
        )
    )
    identities = tuple(
        (str(stream.radio_id), str(stream.receiver_chain_id))
        for stream in ordered_streams
    )
    if identities != spec.receiver_identities or len(identities) != len(
        set(identities)
    ):
        raise ValueError("receiver membership differs from frozen member")
    _verify_qam_source(response, qam)
    qam_by_receiver, coherent = _qam_goodness_by_receiver(response, qam)

    patterns = []
    for pattern_index in range(len(templates)):
        receivers = []
        for stream in ordered_streams:
            points = _method_points(stream, spec.method)
            winners = tuple(
                point.qin
                if pattern_index == 0
                else point.surrogates[pattern_index - 1].winner
                for point in points
            )
            maximum = max(item.score for item in winners)
            candidate_count = sum(item.score > 0 for item in winners)
            qam_goodness, complete_frames = qam_by_receiver.get(
                (str(stream.radio_id), str(stream.receiver_chain_id)), (0.0, 0)
            )
            if pattern_index != 0:
                qam_goodness, complete_frames = 0.0, 0
            receivers.append(
                AdaptiveReceiverPatternEvidenceV0_1(
                    stream.radio_id,
                    stream.receiver_chain_id,
                    maximum,
                    candidate_count,
                    maximum,
                    qam_goodness,
                    complete_frames,
                )
            )
        patterns.append(
            AdaptivePatternDwellEvidenceV0_1(
                pattern_index,
                (
                    AdaptivePatternRole.QIN
                    if pattern_index == 0
                    else AdaptivePatternRole.SURROGATE
                ),
                tuple(receivers),
                coherent if pattern_index == 0 else False,
            )
        )
    dwell = AdaptiveCalibrationDwellV0_1(
        spec.dwell_id,
        spec.member_digest,
        spec.group_digest,
        spec.split,
        spec.label,
        spec.cell_identity_digest,
        tuple(patterns),
    )
    return AssembledAdaptiveCalibrationInputV0_1(
        SchemaRef(AssembledAdaptiveCalibrationInputV0_1.SCHEMA_ID, V0_1),
        spec.digest,
        spec.split_manifest_digest,
        response.digest,
        qam.digest if qam is not None else None,
        search_identity,
        templates,
        dwell,
        ("declared-time-windows", "coarse-cfo", "residual-cfo", "epoch"),
        "none",
        True,
    )


def assemble_pattern_symmetric_adaptive_calibration_input_v0_1(
    spec: AdaptiveCalibrationAssemblySpecV0_1,
    response: StarlinkAdaptiveResponseBundleV0_1,
    qam: PatternSymmetricAdaptiveQamBundleV0_5,
) -> AssembledAdaptiveCalibrationInputV0_1:
    """Assemble Qin and surrogate QAM symmetrically for null or positive dwells."""

    if (
        spec.qam_bundle_digest != qam.digest
        or qam.recording_id != response.recording_id
        or qam.recording_identity_digest != response.recording_identity_digest
        or qam.source_adaptive_response_digest != response.digest
        or qam.pattern_template_digests != spec.pattern_template_digests
    ):
        raise ValueError("pattern-symmetric QAM differs from frozen member")
    base = assemble_adaptive_calibration_input_v0_1(
        replace(spec, qam_bundle_digest=None), response
    )
    qam_streams = {
        (str(item.radio_id), str(item.receiver_chain_id)): item for item in qam.streams
    }
    expected = {
        (str(item.radio_id), str(item.receiver_chain_id)) for item in response.streams
    }
    if set(qam_streams) != expected:
        raise ValueError("pattern-symmetric QAM receiver membership differs")
    coherent = (
        len(qam_streams) >= 2
        and len(
            {
                tuple(
                    (window.start_sample, window.stop_sample)
                    for window in stream.patterns[0].windows
                )
                for stream in qam.streams
            }
        )
        == 1
    )
    patterns = []
    for pattern in base.dwell.patterns:
        receivers = []
        for receiver in pattern.receiver_evidence:
            evidence = qam_streams[receiver.identity].patterns[pattern.pattern_index]
            receivers.append(
                replace(
                    receiver,
                    qam_goodness=max(item.qam_goodness for item in evidence.windows),
                    qam_complete_frame_count=sum(
                        item.complete_frame_count for item in evidence.windows
                    ),
                )
            )
        patterns.append(
            replace(
                pattern,
                receiver_evidence=tuple(receivers),
                dual_rx_coherent_qam=coherent,
            )
        )
    return replace(
        base,
        assembly_spec_digest=spec.digest,
        qam_bundle_digest=qam.digest,
        dwell=replace(base.dwell, patterns=tuple(patterns)),
    )


def conditioned_positive_plumbing_v0_1(
    *,
    fixture_id: str,
    fixture_digest: Digest,
    receiver_accuracy_and_evm: Sequence[tuple[float, float]],
) -> AdaptiveConditionedPositivePlumbingV0_1:
    """Rate a historical known-positive without manufacturing a calibration dwell."""

    return AdaptiveConditionedPositivePlumbingV0_1(
        SchemaRef(AdaptiveConditionedPositivePlumbingV0_1.SCHEMA_ID, V0_1),
        fixture_id,
        fixture_digest,
        tuple(qam_goodness_v0_2(*item) for item in receiver_accuracy_and_evm),
        AdaptiveCalibrationEvidencePurpose.CONDITIONED_POSITIVE,
        False,
    )


def _method_points(
    stream: StarlinkAdaptiveResponseStreamV0_1,
    method: StarlinkDetectorMethod,
) -> tuple[StarlinkAdaptiveResponsePointV0_1, ...]:
    points = tuple(item for item in stream.points if item.method is method)
    if tuple(item.window_index for item in points) != tuple(
        item.window_index for item in stream.selection.exact_windows
    ):
        raise ValueError("adaptive response omits declared time-search windows")
    return points


def _verify_qam_source(
    response: StarlinkAdaptiveResponseBundleV0_1,
    qam: StarlinkAdaptiveQamBundleV0_4 | None,
) -> None:
    if qam is None:
        return
    if (
        qam.recording_id != response.recording_id
        or qam.recording_identity_digest != response.recording_identity_digest
        or qam.source_adaptive_response_ref.digest != response.digest
        or qam.source_suite_ref != response.source_suite_ref
    ):
        raise ValueError("adaptive QAM does not close over the response member")


def _qam_goodness_by_receiver(
    response: StarlinkAdaptiveResponseBundleV0_1,
    qam: StarlinkAdaptiveQamBundleV0_4 | None,
) -> tuple[dict[tuple[str, str], tuple[float, int]], bool]:
    if qam is None:
        return {}, False
    result: dict[tuple[str, str], tuple[float, int]] = {}
    window_memberships: list[tuple[tuple[int, int, int, int], ...]] = []
    for selected, evidence in zip(
        qam.stream_selections, qam.evidence_bundle.streams, strict=True
    ):
        key = (str(selected.radio_id), str(selected.receiver_chain_id))
        if key in result:
            raise ValueError("adaptive QAM repeats a receiver")
        result[key] = (
            qam_goodness_v0_2(
                evidence.overall.support_weighted_hard_symbol_accuracy,
                evidence.overall.support_weighted_rms_evm,
            ),
            evidence.overall.complete_frame_count,
        )
        window_memberships.append(
            tuple(
                (
                    chosen.qam_start_sample,
                    chosen.qam_stop_sample,
                    int(window.interval_start_utc_ns),
                    int(window.interval_stop_utc_ns),
                )
                for chosen, window in zip(
                    selected.windows, evidence.windows, strict=True
                )
            )
        )
    expected = {
        (str(item.radio_id), str(item.receiver_chain_id)) for item in response.streams
    }
    if set(result) != expected:
        raise ValueError("adaptive QAM receiver membership differs from response")
    coherent = len(window_memberships) >= 2 and len(set(window_memberships)) == 1
    return result, coherent
