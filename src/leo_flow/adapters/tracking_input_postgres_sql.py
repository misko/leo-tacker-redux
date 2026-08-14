"""SQL for immutable tracking-input snapshot publication and exact reads."""

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
SELECT public.publish_tracking_input_snapshot(%(publication)s::jsonb) AS inserted
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
       tis.bundle_digest_algorithm, tis.bundle_digest_value,
       tis.bundle_byte_count, tis.bundle_media_type, tis.bundle_format_id,
       ob.locator AS bundle_locator
FROM public.tracking_input_snapshot AS tis
JOIN public.object_blob AS ob
  ON (ob.digest_algorithm, ob.digest_value) =
     (tis.bundle_digest_algorithm, tis.bundle_digest_value)
 AND ob.byte_count = tis.bundle_byte_count
 AND ob.media_type = tis.bundle_media_type
 AND ob.format_id = tis.bundle_format_id
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
SELECT entry.*
FROM public.tracking_input_entry AS entry
JOIN public.recording_hardware_link AS hardware_link
  ON hardware_link.link_id = entry.hardware_link_id
 AND hardware_link.link_digest_algorithm = entry.hardware_link_digest_algorithm
 AND hardware_link.link_digest_value = entry.hardware_link_digest_value
 AND hardware_link.recording_id = entry.recording_id
 AND hardware_link.recording_identity_digest_algorithm =
     entry.recording_identity_digest_algorithm
 AND hardware_link.recording_identity_digest_value =
     entry.recording_identity_digest_value
 AND hardware_link.hardware_snapshot_id = entry.hardware_snapshot_id
 AND hardware_link.hardware_snapshot_digest_algorithm =
     entry.hardware_snapshot_digest_algorithm
 AND hardware_link.hardware_snapshot_digest_value =
     entry.hardware_snapshot_digest_value
JOIN public.hardware_receiver_chain AS receiver_chain
  ON receiver_chain.snapshot_id = entry.hardware_snapshot_id
 AND receiver_chain.receiver_chain_id = entry.receiver_chain_id
 AND receiver_chain.valid_from_utc_ns = entry.receiver_chain_valid_from_utc_ns
 AND receiver_chain.valid_from_utc_ns <= entry.midpoint_utc_ns
 AND (receiver_chain.valid_until_utc_ns IS NULL
      OR entry.midpoint_utc_ns < receiver_chain.valid_until_utc_ns)
WHERE entry.tracking_input_snapshot_id = %(catalog_snapshot_id)s
ORDER BY entry.entry_index
"""
