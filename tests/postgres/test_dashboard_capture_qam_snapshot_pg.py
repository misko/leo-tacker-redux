from __future__ import annotations

import hashlib

import psycopg
import pytest
from psycopg.rows import dict_row


@pytest.mark.integration
def test_qam_snapshot_routine_is_bounded_dashboard_only_and_cas_free(
    postgres_dsn: str,
) -> None:
    signature = "read_dashboard_capture_qam_snapshot_v0_1(bigint,bigint,integer,text[])"
    with psycopg.connect(postgres_dsn) as connection:
        privileges = connection.execute(
            """
            SELECT has_function_privilege('leo_dashboard',%s,'EXECUTE'),
                   has_function_privilege('leo_analysis',%s,'EXECUTE'),
                   has_function_privilege('leo_capture',%s,'EXECUTE'),
                   has_function_privilege('leo_maintenance',%s,'EXECUTE')
            """,
            (signature, signature, signature, signature),
        ).fetchone()
        definition = connection.execute(
            "SELECT pg_get_functiondef(%s::regprocedure)", (signature,)
        ).fetchone()
    assert privileges == (True, False, False, False)
    assert definition is not None
    source = str(definition[0])
    assert "cardinality($4) <= $3" in source
    assert "dashboard_capture_qam_summary_receipt_v0_2" in source
    assert "dashboard_capture_qam_candidate_v0_1" in source
    assert "hardware_receiver_chain" in source
    assert "object_blob" not in source
    assert "locator" not in source


@pytest.mark.integration
def test_qam_snapshot_returns_explicit_states_and_unpooled_assignments(
    postgres_dsn: str,
) -> None:
    recording_ids = (
        "rec_qam_complete",
        "rec_qam_none",
        "rec_qam_pending",
        "rec_qam_failed",
        "rec_qam_fresh",
    )
    with psycopg.connect(postgres_dsn, row_factory=dict_row) as connection:
        _seed_snapshot(connection, recording_ids)
        connection.execute("SET ROLE leo_dashboard")
        rows = connection.execute(
            "SELECT * FROM read_dashboard_capture_qam_snapshot_v0_1(%s,%s,%s,%s)",
            (100, 200, 5, list(recording_ids)),
        ).fetchall()

    by_recording: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        by_recording.setdefault(str(row["recording_id"]), []).append(row)
    assert {key: value[0]["summary_state"] for key, value in by_recording.items()} == {
        "rec_qam_complete": "complete",
        "rec_qam_none": "no_candidate",
        "rec_qam_pending": "pending",
        "rec_qam_failed": "failed",
        "rec_qam_fresh": "not_analyzed",
    }
    complete = by_recording["rec_qam_complete"]
    assert [
        (row["radio_id"], row["lnb_id"], row["receiver_chain_id"]) for row in complete
    ] == [
        ("radio_qam_0", "lnb_qam_a", "rx_qam_a"),
        ("radio_qam_0", "lnb_qam_b", "rx_qam_b"),
    ]
    assert all(row["receipt_candidate_count"] == 2 for row in complete)


def _seed_snapshot(
    connection: psycopg.Connection[dict[str, object]], recording_ids: tuple[str, ...]
) -> None:
    connection.execute("SET session_replication_role=replica")
    for recording_id in recording_ids:
        digests = [
            hashlib.sha256(f"{recording_id}:{part}".encode()).hexdigest()
            for part in ("data", "meta")
        ]
        for digest, part in zip(digests, ("data", "meta"), strict=True):
            connection.execute(
                "INSERT INTO object_blob(digest_algorithm,digest_value,byte_count,media_type,format_id,locator) VALUES('sha256',%s,1,'test/qam','test-v1',%s)",
                (digest, f"object://qam-snapshot/{recording_id}/{part}"),
            )
        connection.execute(
            "INSERT INTO recording(recording_id,data_digest_value,metadata_digest_value,manifest_digest_value,idempotency_key,state) VALUES(%s,%s,%s,%s,%s,'published')",
            (
                recording_id,
                digests[0],
                digests[1],
                hashlib.sha256(f"{recording_id}:manifest".encode()).hexdigest(),
                f"qam-snapshot-{recording_id}",
            ),
        )
    hardware_digest = hashlib.sha256(b"qam snapshot hardware").hexdigest()
    connection.execute(
        "INSERT INTO object_blob(digest_algorithm,digest_value,byte_count,media_type,format_id,locator) VALUES('sha256',%s,1,'test/qam','test-v1','object://qam-snapshot/hardware')",
        (hardware_digest,),
    )
    connection.execute(
        "INSERT INTO hardware_snapshot(snapshot_id,snapshot_digest_algorithm,snapshot_digest_value,bundle_digest_algorithm,bundle_digest_value,station_id,radio_count,chain_count,idempotency_key) VALUES('hw_qam_snapshot','sha256',%s,'sha256',%s,'station_qam_snapshot',5,6,'qam-snapshot-hardware')",
        (hardware_digest, hardware_digest),
    )
    for index, recording_id in enumerate(recording_ids):
        radio_id = f"radio_qam_{index}"
        connection.execute(
            "INSERT INTO hardware_radio(snapshot_id,radio_index,radio_id) VALUES('hw_qam_snapshot',%s,%s)",
            (index, radio_id),
        )
        chains = (
            (("rx_qam_a", "lnb_qam_a"), ("rx_qam_b", "lnb_qam_b"))
            if index == 0
            else ((f"rx_qam_{index}", f"lnb_qam_{index}"),)
        )
        for offset, (receiver_id, lnb_id) in enumerate(chains):
            connection.execute(
                "INSERT INTO hardware_receiver_chain(snapshot_id,chain_index,receiver_chain_id,radio_id,radio_channel,lnb_id,valid_from_utc_ns) VALUES('hw_qam_snapshot',%s,%s,%s,%s,%s,0)",
                (index * 2 + offset, receiver_id, radio_id, offset, lnb_id),
            )
        link_digest = hashlib.sha256(f"{recording_id}:link".encode()).hexdigest()
        connection.execute(
            "INSERT INTO recording_hardware_link(link_id,recording_id,recording_identity_digest_algorithm,recording_identity_digest_value,hardware_snapshot_id,hardware_snapshot_digest_algorithm,hardware_snapshot_digest_value,link_digest_algorithm,link_digest_value,idempotency_key) VALUES(%s,%s,'sha256',%s,'hw_qam_snapshot','sha256',%s,'sha256',%s,%s)",
            (
                f"hwlink_{hashlib.md5(recording_id.encode(), usedforsecurity=False).hexdigest()}",
                recording_id,
                hashlib.sha256(recording_id.encode()).hexdigest(),
                hardware_digest,
                link_digest,
                f"qam-snapshot-link-{index}",
            ),
        )
        sequence = connection.execute(
            "INSERT INTO dashboard_capture_batch_projection(schema_id,schema_version,batch_id,capture_revision,mode,coordination_claim,requested_start_utc_ns,requested_start_skew_ns,paired_analysis_eligibility,semantic_view) VALUES('org.leo-flow.dashboard.capture-batch','0.1',%s,1,'independent','none',%s,0,'eligible','{}') RETURNING projection_sequence",
            (f"cbatch_qam_{index}", 110 + index),
        ).fetchone()
        assert sequence is not None
        analysis_state = (
            "failed" if index == 3 else ("pending" if index == 2 else "complete")
        )
        connection.execute(
            "INSERT INTO dashboard_capture_attempt_projection(projection_sequence,attempt_position,attempt_id,radio_id,plan_id,requested_start_utc_ns,capture_state,observed_start_utc_ns,recording_id,analysis_state,analysis_result_available) VALUES(%s,0,%s,%s,%s,%s,'succeeded',%s,%s,%s,%s)",
            (
                sequence["projection_sequence"],
                f"cattempt_qam_{index}",
                radio_id,
                f"plan_qam_{index}",
                110 + index,
                110 + index,
                recording_id,
                analysis_state,
                analysis_state == "complete",
            ),
        )
    for index, recording_id in enumerate(recording_ids[:3]):
        analysis_id = f"slqam3rec_{index + 1:032x}"
        digest = hashlib.sha256(f"{analysis_id}:bundle".encode()).hexdigest()
        connection.execute(
            "INSERT INTO object_blob(digest_algorithm,digest_value,byte_count,media_type,format_id,locator) VALUES('sha256',%s,1,'test/qam','test-v1',%s)",
            (digest, f"object://qam-snapshot/{analysis_id}"),
        )
        connection.execute(
            "INSERT INTO recording_starlink_acquired_constellation_v0_3(analysis_id,recording_id,input_recording_digest_algorithm,input_recording_digest_value,source_suite_analysis_id,source_suite_bundle_digest_algorithm,source_suite_bundle_digest_value,source_suite_request_digest_algorithm,source_suite_request_digest_value,request_digest_algorithm,request_digest_value,bundle_digest_algorithm,bundle_digest_value,stream_count,window_count,point_count,calibration_required,idempotency_key) VALUES(%s,%s,'sha256',%s,%s,'sha256',%s,'sha256',%s,'sha256',%s,'sha256',%s,1,1,2400,true,%s)",
            (
                analysis_id,
                recording_id,
                "1" * 64,
                f"slsuite_{index + 1:032x}",
                "2" * 64,
                "3" * 64,
                "4" * 64,
                digest,
                f"qam-product-{index}",
            ),
        )
        if index < 2:
            candidate_count = 2 if index == 0 else 0
            connection.execute(
                "INSERT INTO dashboard_capture_qam_summary_receipt_v0_2(source_kind,analysis_id,recording_id,source_request_digest_algorithm,source_request_digest_value,source_product_digest_algorithm,source_product_digest_value,summary_config_digest_algorithm,summary_config_digest_value,candidate_set_digest_algorithm,candidate_set_digest_value,terminal_outcome,candidate_count,candidate_only,calibration_required) VALUES('acquired-v0.3',%s,%s,'sha256',%s,'sha256',%s,'sha256',%s,'sha256',%s,%s,%s,true,true)",
                (
                    analysis_id,
                    recording_id,
                    "4" * 64,
                    digest,
                    "0edbfe0faec9485ee75409640ad001c7a1dd6e2dafa280debc4e94b575fb31a3",
                    "5" * 64,
                    "complete" if index == 0 else "no-candidate",
                    candidate_count,
                ),
            )
        if index == 0:
            for receiver_id, lnb_id, edge in (
                ("rx_qam_a", "lnb_qam_a", "lower"),
                ("rx_qam_b", "lnb_qam_b", "upper"),
            ):
                connection.execute(
                    "INSERT INTO dashboard_capture_qam_candidate_v0_1(source_kind,analysis_id,recording_id,radio_id,lnb_id,receiver_chain_id,segment_id,edge,qam_goodness,hard_symbol_accuracy,rms_evm,window_count) VALUES('acquired-v0.3',%s,%s,'radio_qam_0',%s,%s,%s,%s,0.8,0.9,0.2,3)",
                    (
                        analysis_id,
                        recording_id,
                        lnb_id,
                        receiver_id,
                        f"seg_{receiver_id}",
                        edge,
                    ),
                )
    connection.execute("SET session_replication_role=origin")
