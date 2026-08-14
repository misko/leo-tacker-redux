"""PostgreSQL statements for immutable ephemeris snapshot publication."""

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
FOR SHARE
"""

PUBLISH_SNAPSHOT_SQL = """
INSERT INTO ephemeris_snapshot (
    snapshot_id, retrieval_id, source, scope, retrieved_at_utc_ns,
    raw_digest_algorithm, raw_digest_value,
    normalized_digest_algorithm, normalized_digest_value,
    provenance_digest_algorithm, provenance_digest_value,
    parser_artifact_id, parser_digest_algorithm, parser_digest_value,
    parser_schema_id, parser_schema_version,
    satellite_count, norad_id_set_digest_algorithm, norad_id_set_digest_value,
    element_epoch_min_utc_ns, element_epoch_max_utc_ns,
    validation_policy_artifact_id, validation_policy_digest_algorithm,
    validation_policy_digest_value, validation_policy_schema_id,
    validation_policy_schema_version, validation_reason_codes, attribution,
    request_spec_digest
) VALUES (
    %(snapshot_id)s, %(retrieval_id)s, %(source)s, %(scope)s,
    %(retrieved_at_utc_ns)s, %(raw_digest_algorithm)s, %(raw_digest_value)s,
    %(normalized_digest_algorithm)s, %(normalized_digest_value)s,
    %(provenance_digest_algorithm)s, %(provenance_digest_value)s,
    %(parser_artifact_id)s, %(parser_digest_algorithm)s, %(parser_digest_value)s,
    %(parser_schema_id)s, %(parser_schema_version)s,
    %(satellite_count)s, %(norad_id_set_digest_algorithm)s,
    %(norad_id_set_digest_value)s, %(element_epoch_min_utc_ns)s,
    %(element_epoch_max_utc_ns)s, %(validation_policy_artifact_id)s,
    %(validation_policy_digest_algorithm)s, %(validation_policy_digest_value)s,
    %(validation_policy_schema_id)s, %(validation_policy_schema_version)s,
    %(validation_reason_codes)s, %(attribution)s, %(request_spec_digest)s
)
ON CONFLICT DO NOTHING
RETURNING snapshot_id
"""

SNAPSHOT_SELECT = """
SELECT e.*,
       raw.byte_count AS raw_byte_count,
       raw.media_type AS raw_media_type,
       raw.format_id AS raw_format_id,
       raw.locator AS raw_locator,
       normalized.byte_count AS normalized_byte_count,
       normalized.media_type AS normalized_media_type,
       normalized.format_id AS normalized_format_id,
       normalized.locator AS normalized_locator,
       provenance.byte_count AS provenance_byte_count,
       provenance.media_type AS provenance_media_type,
       provenance.format_id AS provenance_format_id,
       provenance.locator AS provenance_locator
FROM ephemeris_snapshot AS e
JOIN object_blob AS raw
  ON (raw.digest_algorithm, raw.digest_value) =
     (e.raw_digest_algorithm, e.raw_digest_value)
JOIN object_blob AS normalized
  ON (normalized.digest_algorithm, normalized.digest_value) =
     (e.normalized_digest_algorithm, e.normalized_digest_value)
JOIN object_blob AS provenance
  ON (provenance.digest_algorithm, provenance.digest_value) =
     (e.provenance_digest_algorithm, e.provenance_digest_value)
"""

GET_BY_SNAPSHOT_SQL = SNAPSHOT_SELECT + " WHERE e.snapshot_id = %(snapshot_id)s"

GET_BY_RETRIEVAL_SQL = SNAPSHOT_SELECT + " WHERE e.retrieval_id = %(retrieval_id)s"

GET_CONFLICTS_SQL = (
    SNAPSHOT_SELECT
    + """ WHERE e.snapshot_id = %(snapshot_id)s
              OR e.retrieval_id = %(retrieval_id)s
          FOR UPDATE OF e"""
)

HISTORY_SQL = """
SELECT snapshot_id, source, retrieved_at_utc_ns,
       raw_digest_algorithm, raw_digest_value,
       normalized_digest_algorithm, normalized_digest_value
FROM ephemeris_snapshot
WHERE source = %(source)s AND scope = %(scope)s
ORDER BY retrieved_at_utc_ns, snapshot_id
"""

LOCK_ACTIVE_LEASE_SQL = """
SELECT job_id
FROM job
WHERE job_id = %(job_id)s
  AND job_type = 'ephemeris_retrieval'
  AND state = 'leased'
  AND lease_token = %(lease_token)s
  AND lease_generation = %(lease_generation)s
  AND lease_expires_utc > clock_timestamp()
FOR UPDATE
"""
