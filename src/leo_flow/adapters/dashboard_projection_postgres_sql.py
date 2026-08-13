"""Static SQL for append-only PostgreSQL dashboard projection writers."""

LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended(%(key)s, 0))"

CAPTURE_IDENTITY_SQL = """
SELECT authoritative_identity_digest, authoritative_identity,
       first_projection_sequence
FROM dashboard_capture_projection_identity
WHERE projection_kind = %(projection_kind)s AND logical_id = %(logical_id)s
"""

INSERT_CAPTURE_IDENTITY_SQL = """
INSERT INTO dashboard_capture_projection_identity
    (projection_kind, logical_id, authoritative_identity_digest,
     authoritative_identity, first_projection_sequence)
VALUES
    (%(projection_kind)s, %(logical_id)s, %(authoritative_identity_digest)s,
     %(authoritative_identity)s::jsonb, %(first_projection_sequence)s)
"""

ANALYSIS_IDENTITY_SQL = """
SELECT authoritative_identity_digest, authoritative_identity,
       first_projection_sequence
FROM dashboard_analysis_projection_identity
WHERE projection_kind = %(projection_kind)s AND logical_id = %(logical_id)s
"""

INSERT_ANALYSIS_IDENTITY_SQL = """
INSERT INTO dashboard_analysis_projection_identity
    (projection_kind, logical_id, authoritative_identity_digest,
     authoritative_identity, first_projection_sequence)
VALUES
    (%(projection_kind)s, %(logical_id)s, %(authoritative_identity_digest)s,
     %(authoritative_identity)s::jsonb, %(first_projection_sequence)s)
"""

CATALOG_RECORDING_SQL = """
SELECT r.recording_id,
       data.digest_algorithm AS data_digest_algorithm,
       data.digest_value AS data_digest_value,
       data.byte_count AS data_byte_count,
       data.media_type AS data_media_type,
       data.format_id AS data_format_id,
       data.locator AS data_locator,
       metadata.digest_algorithm AS metadata_digest_algorithm,
       metadata.digest_value AS metadata_digest_value,
       metadata.byte_count AS metadata_byte_count,
       metadata.media_type AS metadata_media_type,
       metadata.format_id AS metadata_format_id,
       metadata.locator AS metadata_locator,
       r.manifest_digest_value
FROM recording AS r
JOIN object_blob AS data
  ON (data.digest_algorithm, data.digest_value) =
     (r.data_digest_algorithm, r.data_digest_value)
JOIN object_blob AS metadata
  ON (metadata.digest_algorithm, metadata.digest_value) =
     (r.metadata_digest_algorithm, r.metadata_digest_value)
WHERE r.recording_id = %(recording_id)s AND r.state = 'published'
"""

LATEST_RECORDING_SQL = """
SELECT projection_sequence, recording_id, radio_id, started_utc_ns, finished_utc_ns,
       analysis_state, segment_count, recording_object_available
FROM dashboard_recording_projection
WHERE recording_id = %(recording_id)s
ORDER BY projection_sequence DESC LIMIT 1
"""

INSERT_RECORDING_SQL = """
INSERT INTO dashboard_recording_projection
    (recording_id, radio_id, started_utc_ns, finished_utc_ns,
     analysis_state, segment_count, recording_object_available)
VALUES
    (%(recording_id)s, %(radio_id)s, %(started_utc_ns)s, %(finished_utc_ns)s,
     %(analysis_state)s, %(segment_count)s, %(recording_object_available)s)
RETURNING projection_sequence
"""

LATEST_ACTIVITIES_SQL = """
SELECT DISTINCT ON (activity_id)
       activity_id, projection_sequence, recording_id, radio_id, kind, started_utc_ns
FROM dashboard_activity_projection
WHERE activity_id = ANY(%(activity_ids)s::text[])
ORDER BY activity_id, projection_sequence DESC
"""

INSERT_ACTIVITY_SQL = """
INSERT INTO dashboard_activity_projection
    (activity_id, recording_id, radio_id, kind, started_utc_ns)
VALUES
    (%(activity_id)s, %(recording_id)s, %(radio_id)s, %(kind)s, %(started_utc_ns)s)
RETURNING projection_sequence
"""

LATEST_FEATURES_SQL = """
SELECT DISTINCT ON (feature_id)
       feature_id, projection_sequence, recording_id, method_id, score, score_semantics
FROM dashboard_feature_projection
WHERE feature_id = ANY(%(feature_ids)s::text[])
ORDER BY feature_id, projection_sequence DESC
"""

INSERT_FEATURE_SQL = """
INSERT INTO dashboard_feature_projection
    (feature_id, recording_id, method_id, score, score_semantics)
VALUES
    (%(feature_id)s, %(recording_id)s, %(method_id)s, %(score)s, %(score_semantics)s)
RETURNING projection_sequence
"""

MODEL_ROWS_SQL = """
SELECT projection_sequence, model_snapshot_id, release_alias, parameter_count, warnings
FROM dashboard_model_projection
WHERE model_snapshot_id = %(model_snapshot_id)s
   OR (%(release_alias)s::text IS NOT NULL AND release_alias = %(release_alias)s)
ORDER BY projection_sequence
"""

INSERT_MODEL_SQL = """
INSERT INTO dashboard_model_projection
    (model_snapshot_id, release_alias, parameter_count, warnings)
VALUES
    (%(model_snapshot_id)s, %(release_alias)s, %(parameter_count)s, %(warnings)s::jsonb)
RETURNING projection_sequence
"""

LATEST_TRACK_SQL = """
SELECT projection_sequence, track_id, model_snapshot_id, radio_id,
       started_utc_ns, finished_utc_ns
FROM dashboard_track_projection
WHERE track_id = %(track_id)s
ORDER BY projection_sequence DESC LIMIT 1
"""

INSERT_TRACK_SQL = """
INSERT INTO dashboard_track_projection
    (track_id, model_snapshot_id, radio_id, started_utc_ns, finished_utc_ns)
VALUES
    (%(track_id)s, %(model_snapshot_id)s, %(radio_id)s,
     %(started_utc_ns)s, %(finished_utc_ns)s)
RETURNING projection_sequence
"""

LATEST_STORAGE_SQL = """
SELECT projection_sequence, available, total_bytes, free_bytes
FROM dashboard_storage_health_projection
ORDER BY projection_sequence DESC LIMIT 1
"""

INSERT_STORAGE_SQL = """
INSERT INTO dashboard_storage_health_projection (available, total_bytes, free_bytes)
VALUES (%(available)s, %(total_bytes)s, %(free_bytes)s)
RETURNING projection_sequence
"""
