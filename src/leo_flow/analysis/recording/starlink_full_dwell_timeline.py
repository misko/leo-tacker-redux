"""Read-only projection of every persisted full-dwell prescreen window."""

from __future__ import annotations

from leo_flow.analysis.recording.starlink_full_dwell_response_persistence import (
    DurableStarlinkFullDwellStoreV0_1,
    StarlinkFullDwellCatalogV0_1,
    StarlinkFullDwellNotFoundError,
)
from leo_flow.contracts.core import V0_1, ArtifactRef, SchemaRef
from leo_flow.contracts.dashboard_full_dwell_timeline import (
    FullDwellTimelineQueryV0_1,
    FullDwellTimelineStreamV0_1,
    FullDwellTimelineWindowV0_1,
    RecordingFullDwellTimelineViewV0_1,
)
from leo_flow.contracts.starlink_full_dwell_response import (
    StarlinkFullDwellPrescreenWindowV0_1,
)


class DurableRecordingFullDwellTimelineQueryV0_1:
    """Project dense cheap evidence without changing the published V15 view."""

    def __init__(
        self,
        store: DurableStarlinkFullDwellStoreV0_1,
        catalog: StarlinkFullDwellCatalogV0_1,
    ) -> None:
        self._store, self._catalog = store, catalog

    def recording_full_dwell_timeline(
        self, query: FullDwellTimelineQueryV0_1
    ) -> RecordingFullDwellTimelineViewV0_1:
        ref = self._catalog.latest_starlink_full_dwell(query.recording_id)
        if ref is None:
            raise StarlinkFullDwellNotFoundError("recording has no full-dwell product")
        with self._store.open(ref) as bundle:
            selected = tuple(
                stream
                for stream in bundle.streams
                if (not query.radio_ids or stream.radio_id in query.radio_ids)
                and (
                    not query.receiver_chain_ids
                    or stream.receiver_chain_id in query.receiver_chain_ids
                )
                and (not query.edges or stream.edge in query.edges)
            )
            original = sum(len(stream.prescreen_windows) for stream in selected)
            remaining = query.maximum_windows
            views: list[FullDwellTimelineStreamV0_1] = []
            for index, stream in enumerate(selected):
                budget = max(1, remaining // (len(selected) - index))
                shown = _bounded_windows(stream.prescreen_windows, budget)
                remaining -= len(shown)
                views.append(
                    FullDwellTimelineStreamV0_1(
                        stream.radio_id,
                        stream.segment_id,
                        stream.receiver_chain_id,
                        stream.channel_number,
                        stream.edge,
                        stream.sample_rate_hz,
                        stream.segment_sample_count,
                        len(stream.prescreen_windows),
                        stream.prescreen_coverage_fraction,
                        stream.exact_coverage_fraction,
                        tuple(
                            FullDwellTimelineWindowV0_1(
                                item.window_index,
                                item.start_sample,
                                item.stop_sample,
                                item.interval_start_utc_ns,
                                item.interval_stop_utc_ns,
                                item.mean_complex_power,
                                item.selected_for_exact_refinement,
                            )
                            for item in shown
                        ),
                    )
                )
            shown_count = sum(len(stream.windows) for stream in views)
            warnings = tuple(
                dict.fromkeys(
                    (
                        *bundle.warnings,
                        "power-prescreen-is-not-starlink-detection",
                    )
                )
            )
            return RecordingFullDwellTimelineViewV0_1(
                SchemaRef(RecordingFullDwellTimelineViewV0_1.SCHEMA_ID, V0_1),
                bundle.recording_id,
                ArtifactRef(bundle.analysis_id, ref.bundle_ref.digest, bundle.schema),
                bundle.plan.coarse_window_sample_count,
                bundle.plan.coarse_stride_samples,
                tuple(views),
                original,
                shown_count,
                shown_count < original,
                "none"
                if shown_count == original
                else "endpoints-global-extrema-even-time",
                True,
                None,
                warnings,
            )


def _bounded_windows(
    windows: tuple[StarlinkFullDwellPrescreenWindowV0_1, ...], maximum: int
) -> tuple[StarlinkFullDwellPrescreenWindowV0_1, ...]:
    if len(windows) <= maximum:
        return windows
    if maximum == 1:
        return (max(windows, key=lambda item: item.mean_complex_power),)
    selected = {0, len(windows) - 1}
    if maximum >= 3:
        selected.add(
            max(
                range(len(windows)), key=lambda index: windows[index].mean_complex_power
            )
        )
    if maximum >= 4:
        selected.add(
            min(
                range(len(windows)), key=lambda index: windows[index].mean_complex_power
            )
        )
    if len(selected) < maximum:
        denominator = maximum - 1
        selected.update(
            round(index * (len(windows) - 1) / denominator) for index in range(maximum)
        )
    if len(selected) > maximum:
        required = {0, len(windows) - 1}
        mean_power = sum(item.mean_complex_power for item in windows) / len(windows)
        ranked = sorted(
            selected - required,
            key=lambda index: (
                -abs(windows[index].mean_complex_power - mean_power),
                index,
            ),
        )
        selected = required | set(ranked[: maximum - len(required)])
    return tuple(windows[index] for index in sorted(selected))
