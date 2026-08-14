"""PostgreSQL statements for immutable FeatureSet publication."""

REGISTER_OBJECT_SQL = """
INSERT INTO object_blob
    (digest_algorithm, digest_value, byte_count, media_type, format_id, locator)
VALUES
    (%(digest_algorithm)s, %(digest_value)s, %(byte_count)s,
     %(media_type)s, %(format_id)s, %(locator)s)
ON CONFLICT DO NOTHING
"""

VERIFY_OBJECT_SQL = """
SELECT byte_count, media_type, format_id, locator
FROM object_blob
WHERE digest_algorithm = %(digest_algorithm)s
  AND digest_value = %(digest_value)s
"""

PUBLISH_FEATURE_SET_SQL = """
INSERT INTO feature_set (
    feature_set_id, analysis_run_id, recording_id,
    input_recording_digest_algorithm, input_recording_digest_value,
    request_digest_algorithm, request_digest_value,
    bundle_digest_algorithm, bundle_digest_value,
    observation_count, method_score_count, idempotency_key
) VALUES (
    %(feature_set_id)s, %(analysis_run_id)s, %(recording_id)s,
    %(input_recording_digest_algorithm)s, %(input_recording_digest_value)s,
    %(request_digest_algorithm)s, %(request_digest_value)s,
    %(bundle_digest_algorithm)s, %(bundle_digest_value)s,
    %(observation_count)s, %(method_score_count)s, %(idempotency_key)s
)
ON CONFLICT DO NOTHING
RETURNING feature_set_id
"""

FEATURE_SET_SELECT = """
SELECT f.*,
       bundle.byte_count AS bundle_byte_count,
       bundle.media_type AS bundle_media_type,
       bundle.format_id AS bundle_format_id,
       bundle.locator AS bundle_locator
FROM feature_set AS f
JOIN object_blob AS bundle
  ON (bundle.digest_algorithm, bundle.digest_value) =
     (f.bundle_digest_algorithm, f.bundle_digest_value)
"""

GET_EXACT_FEATURE_SET_SQL = (
    FEATURE_SET_SELECT
    + """
WHERE f.feature_set_id = %(feature_set_id)s
  AND f.analysis_run_id = %(analysis_run_id)s
  AND f.bundle_digest_algorithm = %(bundle_digest_algorithm)s
  AND f.bundle_digest_value = %(bundle_digest_value)s
"""
)

GET_CONFLICTS_SQL = (
    FEATURE_SET_SELECT
    + """
WHERE f.feature_set_id = %(feature_set_id)s
   OR f.idempotency_key = %(idempotency_key)s
   OR (f.bundle_digest_algorithm, f.bundle_digest_value) =
      (%(bundle_digest_algorithm)s, %(bundle_digest_value)s)
FOR UPDATE OF f
"""
)
