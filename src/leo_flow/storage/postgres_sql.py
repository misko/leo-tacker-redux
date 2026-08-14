"""Driver-neutral SQL statements for a future PostgreSQL recording repository."""

REGISTER_OBJECT_SQL = """
SELECT register_live_object_blob(
    %(digest_algorithm)s, %(digest_value)s, %(byte_count)s,
     %(media_type)s, %(format_id)s, %(locator)s)
"""

VERIFY_OBJECT_SQL = """
SELECT byte_count, media_type, format_id, locator
FROM object_blob
WHERE digest_algorithm = %(digest_algorithm)s
  AND digest_value = %(digest_value)s
  AND lifecycle_state = 'live'
"""

# The caller must execute this after both object registrations in the same
# transaction.  A single non-null row is the only catalog visibility point.
PUBLISH_RECORDING_SQL = """
INSERT INTO recording
    (recording_id, data_digest_algorithm, data_digest_value,
     metadata_digest_algorithm, metadata_digest_value, manifest_digest_value,
     idempotency_key, state)
VALUES
    (%(recording_id)s, %(data_digest_algorithm)s, %(data_digest_value)s,
     %(metadata_digest_algorithm)s, %(metadata_digest_value)s,
     %(manifest_digest_value)s, %(idempotency_key)s, 'published')
ON CONFLICT DO NOTHING
RETURNING recording_id
"""

GET_RECORDING_SQL = """
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
WHERE r.recording_id = %(recording_id)s
  AND r.state = 'published'
"""
