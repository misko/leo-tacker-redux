"""PostgreSQL statements for atomic immutable dataset publication."""

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

PUBLISH_SNAPSHOT_SQL = """
INSERT INTO dataset_snapshot (
    snapshot_id,
    feature_membership_digest_algorithm, feature_membership_digest_value,
    snapshot_digest_algorithm, snapshot_digest_value,
    bundle_digest_algorithm, bundle_digest_value,
    evaluated_method_id, selection_spec, selection_cutoff_utc_ns,
    promoted, promotion_warnings, member_count, idempotency_key
) VALUES (
    %(snapshot_id)s,
    %(feature_membership_digest_algorithm)s,
    %(feature_membership_digest_value)s,
    %(snapshot_digest_algorithm)s, %(snapshot_digest_value)s,
    %(bundle_digest_algorithm)s, %(bundle_digest_value)s,
    %(evaluated_method_id)s, %(selection_spec)s, %(selection_cutoff_utc_ns)s,
    %(promoted)s, %(promotion_warnings)s, %(member_count)s, %(idempotency_key)s
)
ON CONFLICT DO NOTHING
RETURNING snapshot_id
"""

PUBLISH_MEMBER_SQL = """
INSERT INTO dataset_member (
    snapshot_id, member_index, feature_set_id, analysis_run_id,
    feature_digest_algorithm, feature_digest_value, feature_byte_count,
    feature_media_type, feature_format_id, feature_locator,
    split_group_id, split, role, truth
) VALUES (
    %(snapshot_id)s, %(member_index)s, %(feature_set_id)s, %(analysis_run_id)s,
    %(feature_digest_algorithm)s, %(feature_digest_value)s,
    %(feature_byte_count)s, %(feature_media_type)s, %(feature_format_id)s,
    %(feature_locator)s, %(split_group_id)s, %(split)s, %(role)s, %(truth)s
)
"""

SNAPSHOT_SELECT = """
SELECT d.*,
       bundle.byte_count AS bundle_byte_count,
       bundle.media_type AS bundle_media_type,
       bundle.format_id AS bundle_format_id,
       bundle.locator AS bundle_locator
FROM dataset_snapshot AS d
JOIN object_blob AS bundle
  ON (bundle.digest_algorithm, bundle.digest_value) =
     (d.bundle_digest_algorithm, d.bundle_digest_value)
"""

GET_EXACT_SNAPSHOT_SQL = (
    SNAPSHOT_SELECT
    + """
WHERE d.snapshot_id = %(snapshot_id)s
  AND d.feature_membership_digest_algorithm =
      %(feature_membership_digest_algorithm)s
  AND d.feature_membership_digest_value = %(feature_membership_digest_value)s
  AND d.snapshot_digest_algorithm = %(snapshot_digest_algorithm)s
  AND d.snapshot_digest_value = %(snapshot_digest_value)s
"""
)

GET_CONFLICTS_SQL = (
    SNAPSHOT_SELECT
    + """
WHERE d.snapshot_id = %(snapshot_id)s
   OR d.idempotency_key = %(idempotency_key)s
   OR (d.snapshot_digest_algorithm, d.snapshot_digest_value) =
      (%(snapshot_digest_algorithm)s, %(snapshot_digest_value)s)
"""
)

GET_MEMBERS_SQL = """
SELECT *
FROM dataset_member
WHERE snapshot_id = %(snapshot_id)s
ORDER BY member_index
"""
