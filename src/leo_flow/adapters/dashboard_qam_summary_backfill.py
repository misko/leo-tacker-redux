"""Bounded, idempotent CAS-to-summary backfill for pre-0056 QAM products."""

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
    StarlinkAcquiredConstellationNotFoundError,
)
from leo_flow.analysis.recording.starlink_adaptive_qam_persistence import (
    DurableStarlinkAdaptiveQamStoreV0_4,
    StarlinkAdaptiveQamNotFoundError,
)
from leo_flow.contracts.core import Digest, DigestAlgorithm, RecordingId
from leo_flow.contracts.starlink_acquired_constellation_pipeline import (
    StarlinkAcquiredConstellationProductRefV0_3,
    StarlinkAcquiredConstellationRecordingBundleV0_3,
)
from leo_flow.contracts.starlink_adaptive_qam import (
    StarlinkAdaptiveQamBundleV0_4,
    StarlinkAdaptiveQamProductRefV0_4,
)
from leo_flow.contracts.storage import ObjectRef

from .dashboard_qam_summary_projection import (
    publish_acquired_qam_summary_with_cursor,
    publish_adaptive_qam_summary_with_cursor,
)

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresQamSummaryBackfillV0_2:
    """Project exact unreceipted CAS bundles; never used by HTTP or a daemon."""

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
            if row["source_kind"] == "adaptive-v0.4":
                adaptive_ref = self._adaptive_ref(row)
                if adaptive_ref is None:
                    continue
                try:
                    with adaptive_store.open(adaptive_ref) as bundle:
                        self._publish_adaptive(bundle)
                except StarlinkAdaptiveQamNotFoundError:
                    continue
            elif row["source_kind"] == "acquired-v0.3":
                acquired_ref = self._acquired_ref(row)
                if acquired_ref is None:
                    continue
                try:
                    with acquired_store.open(acquired_ref) as bundle:
                        self._publish_acquired(bundle)
                except StarlinkAcquiredConstellationNotFoundError:
                    continue
            else:
                raise RuntimeError("pending QAM summary source kind is invalid")
            projected += 1
        return projected

    def _adaptive_ref(
        self, pending: dict[str, object]
    ) -> StarlinkAdaptiveQamProductRefV0_4 | None:
        row = self._exact_source(
            "read_exact_recording_starlink_adaptive_qam_v0_4", pending
        )
        if row is None:
            return None
        return StarlinkAdaptiveQamProductRefV0_4(
            str(row["analysis_id"]),
            RecordingId(str(row["recording_id"])),
            _object_ref(row),
        )

    def _acquired_ref(
        self, pending: dict[str, object]
    ) -> StarlinkAcquiredConstellationProductRefV0_3 | None:
        row = self._exact_source(
            "read_exact_recording_starlink_acquired_constellation_v0_3", pending
        )
        if row is None:
            return None
        return StarlinkAcquiredConstellationProductRefV0_3(
            str(row["analysis_id"]),
            RecordingId(str(row["recording_id"])),
            _object_ref(row),
        )

    def _exact_source(
        self, function: str, pending: dict[str, object]
    ) -> dict[str, object] | None:
        values = (
            str(pending["analysis_id"]),
            str(pending["recording_id"]),
            "sha256",
            str(pending["source_product_digest_value"]),
        )
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            row = cursor.execute(
                f"SELECT * FROM public.{function}(%s,%s,%s,%s)", values
            ).fetchone()
        if row is None:
            return None
        if (
            str(row["analysis_id"]) != values[0]
            or str(row["recording_id"]) != values[1]
            or str(row["request_digest_value"])
            != str(pending["source_request_digest_value"])
            or str(row["bundle_digest_algorithm"]) != values[2]
            or str(row["bundle_digest_value"]) != values[3]
        ):
            raise RuntimeError("pending QAM summary source closure differs")
        return row

    def _publish_adaptive(self, bundle: StarlinkAdaptiveQamBundleV0_4) -> None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            publish_adaptive_qam_summary_with_cursor(cursor, bundle)

    def _publish_acquired(
        self, bundle: StarlinkAcquiredConstellationRecordingBundleV0_3
    ) -> None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            publish_acquired_qam_summary_with_cursor(cursor, bundle)


# Compatibility name for integrations which constructed the pre-receipt backfill.
PostgresQamSummaryBackfillV0_1 = PostgresQamSummaryBackfillV0_2


def _object_ref(row: dict[str, object]) -> ObjectRef:
    byte_count = row["bundle_byte_count"]
    if isinstance(byte_count, bool) or not isinstance(byte_count, int):
        raise TypeError("QAM source byte count is invalid")
    return ObjectRef(
        Digest(
            DigestAlgorithm(str(row["bundle_digest_algorithm"])),
            str(row["bundle_digest_value"]),
        ),
        byte_count,
        str(row["bundle_media_type"]),
        str(row["bundle_format_id"]),
        str(row["bundle_locator"]),
    )


_PENDING_SQL = (
    "SELECT * FROM public.read_pending_dashboard_capture_qam_products_v0_2(%s)"
)
