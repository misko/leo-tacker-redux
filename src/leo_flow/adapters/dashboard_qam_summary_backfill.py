"""Bounded, idempotent CAS-to-summary backfill for pre-0054 QAM products."""

from __future__ import annotations

from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row

from leo_flow.adapters.starlink_acquired_constellation_postgres import (
    PostgresStarlinkAcquiredConstellationCatalogV0_3,
)
from leo_flow.adapters.starlink_adaptive_qam_postgres import (
    PostgresStarlinkAdaptiveQamCatalogV0_4,
)
from leo_flow.analysis.recording.starlink_acquired_constellation_persistence import (
    DurableStarlinkAcquiredConstellationStoreV0_3,
    StarlinkAcquiredConstellationBlobStore,
)
from leo_flow.analysis.recording.starlink_adaptive_qam_persistence import (
    DurableStarlinkAdaptiveQamStoreV0_4,
)
from leo_flow.contracts.core import RecordingId

from .dashboard_qam_summary_projection import (
    publish_acquired_qam_summary_with_cursor,
    publish_adaptive_qam_summary_with_cursor,
)

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresQamSummaryBackfillV0_1:
    """Read at most ``maximum_products`` legacy CAS bundles; never used by HTTP."""

    def __init__(
        self, connect: ConnectionFactory, blobs: StarlinkAcquiredConstellationBlobStore
    ) -> None:
        self._connect, self._blobs = connect, blobs

    def backfill(self, maximum_products: int = 25) -> int:
        if not 1 <= maximum_products <= 100:
            raise ValueError("QAM summary backfill bound is invalid")
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            pending = cursor.execute(_PENDING_SQL, (maximum_products,)).fetchall()
        acquired_catalog = PostgresStarlinkAcquiredConstellationCatalogV0_3(
            self._connect
        )
        adaptive_catalog = PostgresStarlinkAdaptiveQamCatalogV0_4(self._connect)
        acquired_store = DurableStarlinkAcquiredConstellationStoreV0_3(
            self._blobs, acquired_catalog
        )
        adaptive_store = DurableStarlinkAdaptiveQamStoreV0_4(
            self._blobs, adaptive_catalog
        )
        projected = 0
        for row in pending:
            recording_id = RecordingId(str(row["recording_id"]))
            if row["source_kind"] == "adaptive-v0.4":
                adaptive_ref = adaptive_catalog.latest_starlink_adaptive_qam(
                    recording_id
                )
                if adaptive_ref is None:
                    continue
                with (
                    adaptive_store.open(adaptive_ref) as bundle,
                    self._connect() as connection,
                    connection.cursor(row_factory=dict_row) as cursor,
                ):
                    publish_adaptive_qam_summary_with_cursor(cursor, bundle)
            else:
                acquired_ref = acquired_catalog.latest_starlink_acquired_constellation(
                    recording_id
                )
                if acquired_ref is None:
                    continue
                with (
                    acquired_store.open(acquired_ref) as bundle,
                    self._connect() as connection,
                    connection.cursor(row_factory=dict_row) as cursor,
                ):
                    publish_acquired_qam_summary_with_cursor(cursor, bundle)
            projected += 1
        return projected


_PENDING_SQL = """
WITH products AS (
  SELECT 'adaptive-v0.4'::text AS source_kind,analysis_id,recording_id,published_at_utc
    FROM public.recording_starlink_adaptive_qam_v0_4
  UNION ALL
  SELECT 'acquired-v0.3',analysis_id,recording_id,published_at_utc
    FROM public.recording_starlink_acquired_constellation_v0_3
), latest AS (
  SELECT DISTINCT ON (source_kind,recording_id) * FROM products
   ORDER BY source_kind,recording_id,published_at_utc DESC,analysis_id DESC
)
SELECT latest.source_kind,latest.recording_id
  FROM latest
 WHERE NOT EXISTS (
   SELECT 1 FROM public.dashboard_capture_qam_candidate_v0_1 summary
    WHERE summary.source_kind=latest.source_kind AND summary.analysis_id=latest.analysis_id
 )
 ORDER BY latest.published_at_utc,latest.analysis_id
 LIMIT %s
"""
