from __future__ import annotations

import hashlib

import psycopg
import pytest


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


@pytest.mark.integration
def test_completed_adaptive_work_requeues_by_exact_result_without_deleting_history(
    postgres_dsn: str,
) -> None:
    recording_id = "rec_adaptive_reanalysis_pg"
    timeline_id = "fdtl_" + "1" * 32
    suite_id = "slsuite_" + "2" * 32
    response_id = "slar_" + "3" * 32
    data, metadata, timeline, suite, response = (
        _digest(name) for name in ("data", "metadata", "timeline", "suite", "response")
    )
    with psycopg.connect(postgres_dsn) as connection:
        for digest in (data, metadata, timeline, suite, response):
            connection.execute(
                "SELECT public.register_live_object_blob('sha256',%s,64,'application/octet-stream','test-v0.1',%s)",
                (digest, f"cas:sha256:{digest}"),
            )
        connection.execute(
            "INSERT INTO public.recording(recording_id,data_digest_value,metadata_digest_value,manifest_digest_value,idempotency_key,state) VALUES(%s,%s,%s,%s,%s,'published')",
            (recording_id, data, metadata, _digest("manifest"), "recording:reanalysis"),
        )
        connection.execute(
            "INSERT INTO public.recording_starlink_detector_suite VALUES(%s,%s,'sha256',%s,'sha256',%s,'sha256',%s,'candidates',1,8,%s,DEFAULT)",
            (
                suite_id,
                recording_id,
                _digest("recording-identity"),
                _digest("suite-request"),
                suite,
                "suite:reanalysis",
            ),
        )
        connection.execute(
            "INSERT INTO public.recording_full_dwell_timeline_v0_1 VALUES(%s,%s,%s,%s,%s,%s,%s,'sha256',%s,1,1,20000,%s,DEFAULT)",
            (
                timeline_id,
                recording_id,
                _digest("recording-identity"),
                _digest("timeline-request"),
                data,
                metadata,
                _digest("manifest"),
                timeline,
                "timeline:reanalysis",
            ),
        )
        connection.execute(
            "INSERT INTO public.recording_starlink_adaptive_response_v0_1 VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'sha256',%s,1,1,8,%s,DEFAULT)",
            (
                response_id,
                recording_id,
                _digest("recording-identity"),
                timeline_id,
                timeline,
                suite_id,
                suite,
                _digest("adaptive-request"),
                response,
                "adaptive:reanalysis",
            ),
        )
        connection.execute(
            "INSERT INTO public.starlink_adaptive_response_work_v0_1(timeline_analysis_id,recording_id,request_json,state,attempt,result_analysis_id,result_bundle_digest_value,completed_at_utc) VALUES(%s,%s,'{}','succeeded',1,%s,%s,clock_timestamp())",
            (timeline_id, recording_id, response_id, response),
        )

        connection.execute("SET ROLE leo_analysis")
        changed = connection.execute(
            "SELECT public.requeue_starlink_adaptive_response_work_v0_1(%s,%s,%s)",
            (recording_id, response_id, "analysis-plan-cadence-v2"),
        ).fetchone()
        unchanged = connection.execute(
            "SELECT public.requeue_starlink_adaptive_response_work_v0_1(%s,%s,%s)",
            (recording_id, response_id, "analysis-plan-cadence-v2"),
        ).fetchone()
        connection.execute("RESET ROLE")
        state = connection.execute(
            "SELECT state,priority,attempt,result_analysis_id,result_bundle_digest_value,last_error FROM public.starlink_adaptive_response_work_v0_1 WHERE timeline_analysis_id=%s",
            (timeline_id,),
        ).fetchone()
        retained = connection.execute(
            "SELECT count(*) FROM public.recording_starlink_adaptive_response_v0_1 WHERE analysis_id=%s",
            (response_id,),
        ).fetchone()

    assert changed == (True,)
    assert unchanged == (False,)
    assert state == ("ready", 100, 0, None, None, "analysis-plan-cadence-v2")
    assert retained == (1,)
