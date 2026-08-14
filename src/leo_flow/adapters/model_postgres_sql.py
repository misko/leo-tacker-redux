"""SQL for immutable authoritative model snapshots and release history."""

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
FOR UPDATE
"""

DATASET_INPUTS_SQL = """
SELECT ds.snapshot_id, ds.feature_membership_digest_algorithm,
       ds.feature_membership_digest_value,
       dm.feature_digest_algorithm, dm.feature_digest_value
FROM dataset_snapshot AS ds
JOIN dataset_member AS dm ON dm.snapshot_id = ds.snapshot_id
WHERE ds.snapshot_id = %(dataset_snapshot_id)s
  AND ds.feature_membership_digest_algorithm = %(dataset_membership_digest_algorithm)s
  AND ds.feature_membership_digest_value = %(dataset_membership_digest_value)s
ORDER BY dm.member_index
FOR SHARE OF ds, dm
"""

PUBLISH_MODEL_SQL = """
INSERT INTO model_snapshot
    (model_snapshot_id, model_run_id, dataset_snapshot_id,
     dataset_membership_digest_algorithm, dataset_membership_digest_value,
     request_digest_algorithm, request_digest_value,
     provenance_digest_algorithm, provenance_digest_value,
     bundle_digest_algorithm, bundle_digest_value, parameter_count,
     idempotency_key)
VALUES
    (%(model_snapshot_id)s, %(model_run_id)s, %(dataset_snapshot_id)s,
     %(dataset_membership_digest_algorithm)s, %(dataset_membership_digest_value)s,
     %(request_digest_algorithm)s, %(request_digest_value)s,
     %(provenance_digest_algorithm)s, %(provenance_digest_value)s,
     %(bundle_digest_algorithm)s, %(bundle_digest_value)s, %(parameter_count)s,
     %(idempotency_key)s)
ON CONFLICT DO NOTHING
RETURNING model_snapshot_id
"""

MODEL_SELECT = """
SELECT ms.model_snapshot_id, ms.model_run_id, ms.dataset_snapshot_id,
       ms.dataset_membership_digest_algorithm,
       ms.dataset_membership_digest_value,
       ms.request_digest_algorithm, ms.request_digest_value,
       ms.provenance_digest_algorithm, ms.provenance_digest_value,
       ms.parameter_count, ms.idempotency_key,
       ob.digest_algorithm AS bundle_digest_algorithm,
       ob.digest_value AS bundle_digest_value,
       ob.byte_count AS bundle_byte_count,
       ob.media_type AS bundle_media_type,
       ob.format_id AS bundle_format_id,
       ob.locator AS bundle_locator
FROM model_snapshot AS ms
JOIN object_blob AS ob
  ON (ob.digest_algorithm, ob.digest_value) =
     (ms.bundle_digest_algorithm, ms.bundle_digest_value)
"""

GET_CONFLICTS_SQL = (
    MODEL_SELECT
    + """
WHERE ms.model_snapshot_id = %(model_snapshot_id)s
   OR ms.model_run_id = %(model_run_id)s
   OR (ms.bundle_digest_algorithm, ms.bundle_digest_value) =
      (%(bundle_digest_algorithm)s, %(bundle_digest_value)s)
   OR ms.idempotency_key = %(idempotency_key)s
FOR UPDATE OF ms
"""
)

GET_EXACT_MODEL_SQL = (
    MODEL_SELECT
    + """
WHERE ms.model_snapshot_id = %(model_snapshot_id)s
  AND ms.model_run_id = %(model_run_id)s
  AND ms.bundle_digest_algorithm = %(bundle_digest_algorithm)s
  AND ms.bundle_digest_value = %(bundle_digest_value)s
"""
)

PUBLISH_RELEASE_SQL = """
INSERT INTO model_release
    (alias, model_snapshot_id, model_run_id,
     bundle_digest_algorithm, bundle_digest_value,
     approved_by, approved_utc_ns, rationale,
     approval_digest_algorithm, approval_digest_value, idempotency_key)
VALUES
    (%(alias)s, %(model_snapshot_id)s, %(model_run_id)s,
     %(bundle_digest_algorithm)s, %(bundle_digest_value)s,
     %(approved_by)s, %(approved_utc_ns)s, %(rationale)s,
     %(approval_digest_algorithm)s, %(approval_digest_value)s,
     %(idempotency_key)s)
ON CONFLICT DO NOTHING
RETURNING release_sequence
"""

RELEASE_SELECT = """
SELECT mr.release_sequence, mr.alias, mr.model_snapshot_id, mr.model_run_id,
       mr.approved_by, mr.approved_utc_ns, mr.rationale, mr.idempotency_key,
       ob.digest_algorithm AS bundle_digest_algorithm,
       ob.digest_value AS bundle_digest_value,
       ob.byte_count AS bundle_byte_count,
       ob.media_type AS bundle_media_type,
       ob.format_id AS bundle_format_id,
       ob.locator AS bundle_locator
FROM model_release AS mr
JOIN object_blob AS ob
  ON (ob.digest_algorithm, ob.digest_value) =
     (mr.bundle_digest_algorithm, mr.bundle_digest_value)
"""

GET_RELEASE_CONFLICTS_SQL = (
    RELEASE_SELECT
    + """
WHERE mr.idempotency_key = %(idempotency_key)s
   OR (mr.alias = %(alias)s
       AND mr.model_snapshot_id = %(model_snapshot_id)s
       AND mr.approval_digest_algorithm = %(approval_digest_algorithm)s
       AND mr.approval_digest_value = %(approval_digest_value)s)
FOR UPDATE OF mr
"""
)

GET_CURRENT_RELEASE_SQL = (
    RELEASE_SELECT
    + """
WHERE mr.alias = %(alias)s
ORDER BY mr.release_sequence DESC
LIMIT 1
"""
)
