"""SQL for exact tracking-model publication and reads."""

REGISTER_OBJECT_SQL = """
SELECT public.register_live_object_blob(
    %(bundle_digest_algorithm)s, %(bundle_digest_value)s, %(bundle_byte_count)s,
    %(bundle_media_type)s, %(bundle_format_id)s, %(bundle_locator)s)
"""

VERIFY_OBJECT_SQL = """
SELECT byte_count, media_type, format_id, locator
FROM public.object_blob
WHERE digest_algorithm = %(bundle_digest_algorithm)s
  AND digest_value = %(bundle_digest_value)s
  AND lifecycle_state = 'live'
"""

PUBLISH_SQL = """
SELECT public.publish_tracking_model_snapshot(%(publication)s::jsonb) AS inserted
"""

BASE_SELECT = """
SELECT tms.model_snapshot_id, tms.model_run_id,
       tms.scientific_snapshot_digest_algorithm,
       tms.scientific_snapshot_digest_value,
       tms.run_digest_algorithm, tms.run_digest_value,
       tms.output_digest_algorithm, tms.output_digest_value,
       tms.evidence_digest_algorithm, tms.evidence_digest_value,
       tms.provenance_digest_algorithm, tms.provenance_digest_value,
       tms.tracking_input_snapshot_id,
       tms.tracking_input_snapshot_digest_algorithm,
       tms.tracking_input_snapshot_digest_value,
       tms.tracking_input_membership_digest_algorithm,
       tms.tracking_input_membership_digest_value,
       tms.tracking_input_bundle_digest_algorithm,
       tms.tracking_input_bundle_digest_value,
       tms.tracking_input_bundle_byte_count,
       tms.tracking_input_bundle_media_type,
       tms.tracking_input_bundle_format_id,
       tms.parameter_block_count, tms.accepted_association_count,
       tms.rejected_association_count, tms.warning_count,
       tms.bundle_digest_algorithm, tms.bundle_digest_value,
       tms.bundle_byte_count, tms.bundle_media_type, tms.bundle_format_id,
       ob.locator AS bundle_locator, tms.idempotency_key
FROM public.tracking_model_snapshot AS tms
JOIN public.object_blob AS ob
  ON (ob.digest_algorithm, ob.digest_value) =
     (tms.bundle_digest_algorithm, tms.bundle_digest_value)
 AND ob.byte_count = tms.bundle_byte_count
 AND ob.media_type = tms.bundle_media_type
 AND ob.format_id = tms.bundle_format_id
 AND ob.lifecycle_state = 'live'
JOIN public.tracking_input_snapshot AS tis
  ON (tis.snapshot_id, tis.snapshot_digest_algorithm,
      tis.snapshot_digest_value, tis.membership_digest_algorithm,
      tis.membership_digest_value, tis.bundle_digest_algorithm,
      tis.bundle_digest_value, tis.bundle_byte_count,
      tis.bundle_media_type, tis.bundle_format_id) =
     (tms.tracking_input_snapshot_id,
      tms.tracking_input_snapshot_digest_algorithm,
      tms.tracking_input_snapshot_digest_value,
      tms.tracking_input_membership_digest_algorithm,
      tms.tracking_input_membership_digest_value,
      tms.tracking_input_bundle_digest_algorithm,
      tms.tracking_input_bundle_digest_value,
      tms.tracking_input_bundle_byte_count,
      tms.tracking_input_bundle_media_type,
      tms.tracking_input_bundle_format_id)
"""

GET_EXACT_SQL = (
    BASE_SELECT
    + """
WHERE tms.model_snapshot_id = %(model_snapshot_id)s
  AND tms.model_run_id = %(model_run_id)s
  AND tms.output_digest_algorithm = %(output_digest_algorithm)s
  AND tms.output_digest_value = %(output_digest_value)s
  AND tms.bundle_digest_algorithm = %(bundle_digest_algorithm)s
  AND tms.bundle_digest_value = %(bundle_digest_value)s
  AND tms.bundle_byte_count = %(bundle_byte_count)s
  AND tms.bundle_media_type = %(bundle_media_type)s
  AND tms.bundle_format_id = %(bundle_format_id)s
"""
)

GET_CONFLICTS_SQL = (
    BASE_SELECT
    + """
WHERE tms.model_run_id = %(model_run_id)s
   OR (tms.output_digest_algorithm, tms.output_digest_value) =
      (%(output_digest_algorithm)s, %(output_digest_value)s)
   OR (tms.bundle_digest_algorithm, tms.bundle_digest_value) =
      (%(bundle_digest_algorithm)s, %(bundle_digest_value)s)
   OR tms.idempotency_key = %(idempotency_key)s
"""
)
