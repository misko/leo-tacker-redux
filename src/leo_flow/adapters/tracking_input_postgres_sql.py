"""SQL for immutable tracking-input snapshot publication and exact reads."""

REGISTER_OBJECT_SQL = """
SELECT register_live_object_blob(
    %(bundle_digest_algorithm)s, %(bundle_digest_value)s, %(bundle_byte_count)s,
    %(bundle_media_type)s, %(bundle_format_id)s, %(bundle_locator)s)
"""

VERIFY_OBJECT_SQL = """
SELECT byte_count, media_type, format_id, locator
FROM object_blob
WHERE digest_algorithm = %(bundle_digest_algorithm)s
  AND digest_value = %(bundle_digest_value)s
  AND lifecycle_state = 'live'
"""

PUBLISH_SQL = """
SELECT publish_tracking_input_snapshot(%(publication)s::jsonb) AS inserted
"""

SNAPSHOT_SELECT = """
SELECT tis.snapshot_id,
       tis.snapshot_digest_algorithm, tis.snapshot_digest_value,
       tis.membership_digest_algorithm, tis.membership_digest_value,
       tis.dataset_snapshot_id,
       tis.dataset_membership_digest_algorithm,
       tis.dataset_membership_digest_value,
       tis.dataset_snapshot_digest_algorithm,
       tis.dataset_snapshot_digest_value,
       tis.builder_artifact_id, tis.builder_digest_algorithm,
       tis.builder_digest_value, tis.builder_schema_id,
       tis.builder_schema_version,
       tis.selector_artifact_id, tis.selector_digest_algorithm,
       tis.selector_digest_value, tis.selector_schema_id,
       tis.selector_schema_version,
       tis.provenance_digest_algorithm, tis.provenance_digest_value,
       tis.entry_count, tis.idempotency_key,
       ob.digest_algorithm AS bundle_digest_algorithm,
       ob.digest_value AS bundle_digest_value,
       ob.byte_count AS bundle_byte_count,
       ob.media_type AS bundle_media_type,
       ob.format_id AS bundle_format_id,
       ob.locator AS bundle_locator
FROM tracking_input_snapshot AS tis
JOIN object_blob AS ob
  ON (ob.digest_algorithm, ob.digest_value) =
     (tis.bundle_digest_algorithm, tis.bundle_digest_value)
WHERE ob.lifecycle_state = 'live'
"""

GET_EXACT_SQL = (
    SNAPSHOT_SELECT
    + """
  AND tis.snapshot_id = %(snapshot_id)s
  AND tis.snapshot_digest_algorithm = %(snapshot_digest_algorithm)s
  AND tis.snapshot_digest_value = %(snapshot_digest_value)s
  AND tis.membership_digest_algorithm = %(membership_digest_algorithm)s
  AND tis.membership_digest_value = %(membership_digest_value)s
  AND ob.digest_algorithm = %(bundle_digest_algorithm)s
  AND ob.digest_value = %(bundle_digest_value)s
  AND ob.byte_count = %(bundle_byte_count)s
  AND ob.media_type = %(bundle_media_type)s
  AND ob.format_id = %(bundle_format_id)s
"""
)

GET_CONFLICTS_SQL = (
    SNAPSHOT_SELECT
    + """
  AND (tis.snapshot_id = %(snapshot_id)s
       OR (tis.snapshot_digest_algorithm, tis.snapshot_digest_value) =
          (%(snapshot_digest_algorithm)s, %(snapshot_digest_value)s)
       OR (tis.bundle_digest_algorithm, tis.bundle_digest_value) =
          (%(bundle_digest_algorithm)s, %(bundle_digest_value)s)
       OR tis.idempotency_key = %(idempotency_key)s)
"""
)

GET_ENTRIES_SQL = """
SELECT entry_index, feature_set_id, analysis_run_id,
       feature_bundle_digest_algorithm, feature_bundle_digest_value,
       feature_id, recording_id,
       recording_identity_digest_algorithm, recording_identity_digest_value,
       receiver_chain_id, midpoint_utc_ns,
       hardware_link_id, hardware_link_digest_algorithm,
       hardware_link_digest_value,
       ephemeris_link_id, ephemeris_link_digest_algorithm,
       ephemeris_link_digest_value,
       calibration_artifact_id, calibration_digest_algorithm,
       calibration_digest_value, calibration_schema_id,
       calibration_schema_version,
       prediction_policy_artifact_id, prediction_policy_digest_algorithm,
       prediction_policy_digest_value, prediction_policy_schema_id,
       prediction_policy_schema_version
FROM tracking_input_entry
WHERE tracking_input_snapshot_id = %(catalog_snapshot_id)s
ORDER BY entry_index
"""
