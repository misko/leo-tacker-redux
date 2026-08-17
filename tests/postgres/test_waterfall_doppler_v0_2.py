from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType

import psycopg
import pytest

from leo_flow.adapters.dashboard_doppler_projection import (
    DurableDashboardDopplerProjectionV0_1,
)
from leo_flow.adapters.waterfall_doppler_postgres import (
    AtomicPostgresWaterfallDopplerCommitterV0_1,
    PostgresWaterfallDopplerQueryV0_1,
)
from leo_flow.analysis.recording.waterfall_doppler_pipeline import (
    AdvancedBlindDopplerAnalyzerV0_1,
    PreparedTileDopplerV0_1,
    PreparedWaterfallDopplerV0_1,
)
from leo_flow.analysis.recording.waterfall_v0_2_persistence import (
    DurableWaterfallReaderV0_2,
)
from leo_flow.analysis.tracking.doppler_persistence import DurableDopplerReaderV0_1
from leo_flow.contracts.core import ArtifactRef, Digest, SchemaRef
from leo_flow.contracts.dashboard_doppler import (
    DopplerVisualizationState,
    DopplerWaterfallLayer,
)
from leo_flow.contracts.waterfall_v0_2 import (
    V0_2,
    WaterfallAnalysisRequestV0_2,
    WaterfallBundleV0_2,
)
from leo_flow.deployments import dashboard_v1
from leo_flow.services.bootstrap import AdapterBuildContext, Capability, Process
from leo_flow.services.waterfall_doppler_analysis import (
    PreparedCombinedWaterfallAnalysisV0_1,
)
from leo_flow.storage.filesystem import FileSystemBlobStore
from tests.postgres.test_waterfall_analysis_atomic import _claimed, _connect
from tests.recording_analysis.test_waterfall_doppler_pipeline import _basic, _bundle


@pytest.mark.integration
def test_waterfall_doppler_tables_and_functions_are_least_privilege(
    postgres_dsn: str,
) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        rows = connection.execute(
            """
            SELECT c.relname, pg_catalog.pg_get_userbyid(c.relowner)
              FROM pg_catalog.pg_class c
             WHERE c.relname IN (
                 'recording_waterfall_v0_2', 'recording_doppler_analysis')
             ORDER BY c.relname
            """
        ).fetchall()
        assert rows == [
            ("recording_doppler_analysis", "leo_routine_owner"),
            ("recording_waterfall_v0_2", "leo_routine_owner"),
        ]
        for role in ("leo_capture", "leo_dashboard", "leo_maintenance"):
            assert connection.execute(
                """
                SELECT has_table_privilege(%s,'recording_waterfall_v0_2','SELECT'),
                       has_table_privilege(%s,'recording_doppler_analysis','SELECT')
                """,
                (role, role),
            ).fetchone() == (False, False)
        assert connection.execute(
            """
            SELECT has_function_privilege(
                       'leo_dashboard','read_recording_doppler_analysis(text)','EXECUTE'),
                   has_function_privilege(
                       'leo_dashboard','read_recording_waterfall_v0_2(text)','EXECUTE'),
                   has_function_privilege(
                       'leo_dashboard',
                       'publish_recording_waterfall_v0_2(text,text,text,text,text,text,text,text,text,text,text,integer,integer,text)',
                       'EXECUTE')
            """
        ).fetchone() == (True, True, False)


@pytest.mark.integration
def test_waterfall_doppler_read_functions_are_empty_and_bounded_for_dashboard(
    postgres_dsn: str,
) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute("SET ROLE leo_dashboard")
        assert (
            connection.execute(
                "SELECT * FROM public.read_recording_doppler_analysis(%s)",
                ("rec_absent",),
            ).fetchall()
            == []
        )


@pytest.mark.integration
def test_combined_commit_is_atomic_and_dashboard_can_read_exact_blobs(
    postgres_dsn: str, tmp_path
) -> None:
    _, lease, legacy = _claimed(postgres_dsn)
    recording_digest = legacy.request.recording_object_ref.identity_digest()
    waterfall = _bundle()
    provenance = replace(
        waterfall.provenance,
        input_digests=(recording_digest,),
        dependency_digests=(Digest.sha256(b"algorithm"),),
    )
    waterfall = replace(
        waterfall,
        recording_id=legacy.request.recording_id,
        input_recording_identity_digest=recording_digest,
        provenance=provenance,
    )
    spectrogram, basic = _basic(waterfall)
    advanced = AdvancedBlindDopplerAnalyzerV0_1().analyze(spectrogram, basic)
    enhanced_request = WaterfallAnalysisRequestV0_2(
        SchemaRef(WaterfallAnalysisRequestV0_2.SCHEMA_ID, V0_2),
        legacy.request.recording_id,
        legacy.request.recording_object_ref,
        ArtifactRef("waterfall-v0.2-test", Digest.sha256(b"algorithm")),
        ArtifactRef("waterfall-v0.2-config", Digest.sha256(b"config")),
        (),
        SchemaRef(WaterfallBundleV0_2.SCHEMA_ID, V0_2),
    )
    prepared = PreparedCombinedWaterfallAnalysisV0_1(
        legacy.request,
        legacy.bundle,
        PreparedWaterfallDopplerV0_1(
            enhanced_request,
            waterfall,
            (PreparedTileDopplerV0_1(spectrogram, basic, advanced),),
        ),
    )
    blobs = FileSystemBlobStore(tmp_path / "cas")
    AtomicPostgresWaterfallDopplerCommitterV0_1(
        blobs, _connect(postgres_dsn, role=True)
    ).commit_waterfall(lease, prepared)

    query = PostgresWaterfallDopplerQueryV0_1(_connect(postgres_dsn, role=True))
    refs = query.list_recording_doppler(legacy.request.recording_id)
    assert len(refs) == 1
    with DurableWaterfallReaderV0_2(blobs, query).open(
        str(waterfall.product_id)
    ) as durable_waterfall:
        assert durable_waterfall.bundle == waterfall
    with DurableDopplerReaderV0_1(blobs, query).open(
        legacy.request.recording_id, refs[0].doppler_id
    ) as durable_doppler:
        assert durable_doppler.basic == basic
        assert durable_doppler.advanced == advanced
    dashboard = DurableDashboardDopplerProjectionV0_1(
        query,
        DurableWaterfallReaderV0_2(blobs, query),
        DurableDopplerReaderV0_1(blobs, query),
    ).recording_doppler_visualization(
        legacy.request.recording_id, DopplerWaterfallLayer.RESIDUAL
    )
    assert dashboard.state is DopplerVisualizationState.COMPLETE
    assert dashboard.candidate_only is True
    assert dashboard.calibrated_detection_count is None
    assert len(dashboard.tiles) == 1
    assert len(dashboard.candidates) == len(basic.candidates)
    assert len(dashboard.advanced_evidence) == 1
    composed = dashboard_v1._postgres_query_projection(
        AdapterBuildContext(
            Process.DASHBOARD,
            Capability.QUERY_PROJECTION,
            dashboard_v1.QUERY_PROJECTION_REF,
            MappingProxyType(
                {
                    dashboard_v1.DATABASE_SECRET: postgres_dsn,
                    dashboard_v1.CAS_ROOT_SECRET: str(tmp_path / "cas"),
                }
            ),
        )
    ).recording_doppler_visualization(
        legacy.request.recording_id, DopplerWaterfallLayer.HIGH_PERCENTILE
    )
    assert composed.state is DopplerVisualizationState.COMPLETE
    assert composed.selected_layer is DopplerWaterfallLayer.HIGH_PERCENTILE
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT state FROM job WHERE job_id=%s", (str(lease.job_id),)
        ).fetchone() == ("succeeded",)
        assert connection.execute(
            "SELECT count(*) FROM recording_waterfall_v0_2"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM recording_doppler_analysis"
        ).fetchone() == (1,)
        assert (
            connection.execute(
                "SELECT * FROM public.read_recording_waterfall_v0_2(%s)",
                ("waterfall_" + "0" * 32,),
            ).fetchall()
            == []
        )
