BEGIN;

DO $roles$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'leo_maintenance') THEN
        CREATE ROLE leo_maintenance NOLOGIN;
    END IF;
END
$roles$;

ALTER TABLE object_blob
    ADD COLUMN registered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    ADD COLUMN lifecycle_state text NOT NULL DEFAULT 'live'
        CHECK (lifecycle_state IN ('live', 'gc_claimed', 'gc_delete_failed', 'gc_deleted')),
    ADD COLUMN gc_claim_token text,
    ADD COLUMN gc_claimed_at timestamptz,
    ADD COLUMN gc_claim_expires_at timestamptz,
    ADD COLUMN gc_last_error text,
    ADD COLUMN gc_deleted_at timestamptz,
    ADD CONSTRAINT object_blob_gc_state_consistent CHECK (
        (lifecycle_state = 'gc_claimed'
         AND gc_claim_token IS NOT NULL
         AND gc_claimed_at IS NOT NULL
         AND gc_claim_expires_at > gc_claimed_at
         AND gc_deleted_at IS NULL)
        OR
        (lifecycle_state <> 'gc_claimed'
         AND gc_claim_token IS NULL
         AND gc_claimed_at IS NULL
         AND gc_claim_expires_at IS NULL)
    ),
    ADD CONSTRAINT object_blob_deleted_state_consistent CHECK (
        (lifecycle_state = 'gc_deleted') = (gc_deleted_at IS NOT NULL)
    );

CREATE TABLE object_retention_policy (
    policy_id text PRIMARY KEY,
    retain_for_seconds bigint NOT NULL CHECK (retain_for_seconds >= 0),
    grace_period_seconds bigint NOT NULL CHECK (grace_period_seconds > 0),
    allow_remote_delete boolean NOT NULL,
    rationale text NOT NULL CHECK (rationale <> ''),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE object_retention_assignment (
    digest_algorithm text NOT NULL,
    digest_value text NOT NULL,
    policy_id text NOT NULL REFERENCES object_retention_policy (policy_id),
    assigned_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    assigned_by text NOT NULL CHECK (assigned_by <> ''),
    PRIMARY KEY (digest_algorithm, digest_value, policy_id),
    FOREIGN KEY (digest_algorithm, digest_value)
        REFERENCES object_blob (digest_algorithm, digest_value)
);

CREATE TABLE object_gc_attempt (
    attempt_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    digest_algorithm text NOT NULL,
    digest_value text NOT NULL,
    claim_token text NOT NULL,
    event text NOT NULL CHECK (event IN ('claimed', 'delete_failed', 'deleted')),
    event_at timestamptz NOT NULL,
    detail text,
    UNIQUE (claim_token, event)
);

-- This is the exhaustive list of direct object_blob consumers at migration 0013.
-- Adding a new object FK requires extending this view and installing the same
-- live-reference trigger in that owning migration.
CREATE VIEW object_blob_live_reference AS
    SELECT data_digest_algorithm AS digest_algorithm,
           data_digest_value AS digest_value,
           'recording.data'::text AS reference_kind,
           recording_id::text AS owner_id
    FROM recording
UNION ALL
    SELECT metadata_digest_algorithm, metadata_digest_value,
           'recording.metadata', recording_id::text
    FROM recording
UNION ALL
    SELECT raw_digest_algorithm, raw_digest_value,
           'ephemeris_snapshot.raw', snapshot_id::text
    FROM ephemeris_snapshot
UNION ALL
    SELECT normalized_digest_algorithm, normalized_digest_value,
           'ephemeris_snapshot.normalized', snapshot_id::text
    FROM ephemeris_snapshot
UNION ALL
    SELECT provenance_digest_algorithm, provenance_digest_value,
           'ephemeris_snapshot.provenance', snapshot_id::text
    FROM ephemeris_snapshot
UNION ALL
    SELECT bundle_digest_algorithm, bundle_digest_value,
           'dataset_snapshot.bundle', snapshot_id::text
    FROM dataset_snapshot
UNION ALL
    SELECT bundle_digest_algorithm, bundle_digest_value,
           'feature_set.bundle', feature_set_id::text
    FROM feature_set
UNION ALL
    SELECT bundle_digest_algorithm, bundle_digest_value,
           'model_snapshot.bundle', model_snapshot_id::text
    FROM model_snapshot
UNION ALL
    SELECT bundle_digest_algorithm, bundle_digest_value,
           'hardware_snapshot.bundle', snapshot_id::text
    FROM hardware_snapshot
UNION ALL
    SELECT report_digest_algorithm, report_digest_value,
           'detector_evaluation_report.report', evaluation_id::text
    FROM detector_evaluation_report;

CREATE VIEW object_retention_status AS
SELECT b.digest_algorithm,
       b.digest_value,
       b.lifecycle_state,
       count(DISTINCT a.policy_id) AS policy_count,
       coalesce(bool_and(p.allow_remote_delete), false) AS all_policies_allow_delete,
       max(a.assigned_at + make_interval(
           secs => p.retain_for_seconds + p.grace_period_seconds
       )) AS eligible_after,
       (SELECT count(*)
          FROM object_blob_live_reference r
         WHERE r.digest_algorithm = b.digest_algorithm
           AND r.digest_value = b.digest_value) AS live_reference_count
FROM object_blob b
LEFT JOIN object_retention_assignment a
  ON a.digest_algorithm = b.digest_algorithm
 AND a.digest_value = b.digest_value
LEFT JOIN object_retention_policy p ON p.policy_id = a.policy_id
GROUP BY b.digest_algorithm, b.digest_value, b.lifecycle_state;

CREATE VIEW object_gc_candidate AS
SELECT *
  FROM object_retention_status
 WHERE lifecycle_state IN ('live', 'gc_delete_failed')
   AND policy_count > 0
   AND all_policies_allow_delete
   AND eligible_after <= clock_timestamp()
   AND live_reference_count = 0;

CREATE FUNCTION object_blob_assert_live_reference() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    referenced_algorithm text := to_jsonb(NEW) ->> TG_ARGV[0];
    referenced_digest text := to_jsonb(NEW) ->> TG_ARGV[1];
BEGIN
    PERFORM 1
      FROM object_blob
     WHERE digest_algorithm = referenced_algorithm
       AND digest_value = referenced_digest
       AND lifecycle_state = 'live'
     FOR KEY SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'object reference does not identify a live catalog object'
            USING ERRCODE = '23503';
    END IF;
    RETURN NEW;
END
$function$;

CREATE FUNCTION register_live_object_blob(
    p_digest_algorithm text,
    p_digest_value text,
    p_byte_count bigint,
    p_media_type text,
    p_format_id text,
    p_locator text
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    INSERT INTO object_blob
        (digest_algorithm, digest_value, byte_count, media_type, format_id, locator)
    VALUES
        (p_digest_algorithm, p_digest_value, p_byte_count,
         p_media_type, p_format_id, p_locator)
    ON CONFLICT DO NOTHING;

    UPDATE object_blob
       SET lifecycle_state = 'live',
           gc_deleted_at = NULL,
           gc_last_error = NULL,
           verified_at = NULL,
           registered_at = clock_timestamp()
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value
       AND lifecycle_state = 'gc_deleted'
       AND byte_count = p_byte_count
       AND media_type = p_media_type
       AND format_id = p_format_id
       AND locator = p_locator;

END
$function$;

CREATE TRIGGER recording_data_object_must_be_live
BEFORE INSERT OR UPDATE OF data_digest_algorithm, data_digest_value ON recording
FOR EACH ROW EXECUTE FUNCTION object_blob_assert_live_reference(
    'data_digest_algorithm', 'data_digest_value');
CREATE TRIGGER recording_metadata_object_must_be_live
BEFORE INSERT OR UPDATE OF metadata_digest_algorithm, metadata_digest_value ON recording
FOR EACH ROW EXECUTE FUNCTION object_blob_assert_live_reference(
    'metadata_digest_algorithm', 'metadata_digest_value');
CREATE TRIGGER ephemeris_raw_object_must_be_live
BEFORE INSERT OR UPDATE OF raw_digest_algorithm, raw_digest_value ON ephemeris_snapshot
FOR EACH ROW EXECUTE FUNCTION object_blob_assert_live_reference(
    'raw_digest_algorithm', 'raw_digest_value');
CREATE TRIGGER ephemeris_normalized_object_must_be_live
BEFORE INSERT OR UPDATE OF normalized_digest_algorithm, normalized_digest_value ON ephemeris_snapshot
FOR EACH ROW EXECUTE FUNCTION object_blob_assert_live_reference(
    'normalized_digest_algorithm', 'normalized_digest_value');
CREATE TRIGGER ephemeris_provenance_object_must_be_live
BEFORE INSERT OR UPDATE OF provenance_digest_algorithm, provenance_digest_value ON ephemeris_snapshot
FOR EACH ROW EXECUTE FUNCTION object_blob_assert_live_reference(
    'provenance_digest_algorithm', 'provenance_digest_value');
CREATE TRIGGER dataset_bundle_object_must_be_live
BEFORE INSERT OR UPDATE OF bundle_digest_algorithm, bundle_digest_value ON dataset_snapshot
FOR EACH ROW EXECUTE FUNCTION object_blob_assert_live_reference(
    'bundle_digest_algorithm', 'bundle_digest_value');
CREATE TRIGGER feature_bundle_object_must_be_live
BEFORE INSERT OR UPDATE OF bundle_digest_algorithm, bundle_digest_value ON feature_set
FOR EACH ROW EXECUTE FUNCTION object_blob_assert_live_reference(
    'bundle_digest_algorithm', 'bundle_digest_value');
CREATE TRIGGER model_bundle_object_must_be_live
BEFORE INSERT OR UPDATE OF bundle_digest_algorithm, bundle_digest_value ON model_snapshot
FOR EACH ROW EXECUTE FUNCTION object_blob_assert_live_reference(
    'bundle_digest_algorithm', 'bundle_digest_value');
CREATE TRIGGER hardware_bundle_object_must_be_live
BEFORE INSERT OR UPDATE OF bundle_digest_algorithm, bundle_digest_value ON hardware_snapshot
FOR EACH ROW EXECUTE FUNCTION object_blob_assert_live_reference(
    'bundle_digest_algorithm', 'bundle_digest_value');
CREATE TRIGGER evaluation_report_object_must_be_live
BEFORE INSERT OR UPDATE OF report_digest_algorithm, report_digest_value ON detector_evaluation_report
FOR EACH ROW EXECUTE FUNCTION object_blob_assert_live_reference(
    'report_digest_algorithm', 'report_digest_value');

CREATE FUNCTION gc_claim_object(
    p_digest_algorithm text,
    p_digest_value text,
    p_claim_token text,
    p_now timestamptz,
    p_claim_expires_at timestamptz
) RETURNS SETOF object_blob
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    candidate object_blob%ROWTYPE;
    status object_retention_status%ROWTYPE;
BEGIN
    IF p_claim_token IS NULL OR p_claim_token = '' OR p_claim_expires_at <= p_now THEN
        RAISE EXCEPTION 'invalid GC claim';
    END IF;
    SELECT * INTO candidate
      FROM object_blob
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value
       AND (lifecycle_state IN ('live', 'gc_delete_failed')
            OR (lifecycle_state = 'gc_claimed' AND gc_claim_expires_at <= p_now))
     FOR UPDATE;
    IF NOT FOUND THEN RETURN; END IF;

    SELECT * INTO status
      FROM object_retention_status
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value;
    IF status.policy_count = 0
       OR NOT status.all_policies_allow_delete
       OR status.eligible_after > p_now
       OR status.live_reference_count <> 0 THEN
        RETURN;
    END IF;

    UPDATE object_blob
       SET lifecycle_state = 'gc_claimed',
           gc_claim_token = p_claim_token,
           gc_claimed_at = p_now,
           gc_claim_expires_at = p_claim_expires_at,
           gc_last_error = NULL
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value
     RETURNING * INTO candidate;
    INSERT INTO object_gc_attempt
        (digest_algorithm, digest_value, claim_token, event, event_at)
    VALUES (p_digest_algorithm, p_digest_value, p_claim_token, 'claimed', p_now);
    RETURN NEXT candidate;
END
$function$;

CREATE FUNCTION gc_record_delete_failure(
    p_digest_algorithm text,
    p_digest_value text,
    p_claim_token text,
    p_now timestamptz,
    p_detail text
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    IF p_detail IS NULL OR p_detail = '' OR length(p_detail) > 1000 THEN
        RAISE EXCEPTION 'invalid GC failure detail';
    END IF;
    UPDATE object_blob
       SET lifecycle_state = 'gc_delete_failed',
           gc_claim_token = NULL,
           gc_claimed_at = NULL,
           gc_claim_expires_at = NULL,
           gc_last_error = p_detail
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value
       AND lifecycle_state = 'gc_claimed'
       AND gc_claim_token = p_claim_token;
    IF NOT FOUND THEN RETURN false; END IF;
    INSERT INTO object_gc_attempt
        (digest_algorithm, digest_value, claim_token, event, event_at, detail)
    VALUES (p_digest_algorithm, p_digest_value, p_claim_token,
            'delete_failed', p_now, p_detail);
    RETURN true;
END
$function$;

CREATE FUNCTION gc_complete_object_delete(
    p_digest_algorithm text,
    p_digest_value text,
    p_claim_token text,
    p_now timestamptz
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    UPDATE object_blob
       SET lifecycle_state = 'gc_deleted',
           gc_claim_token = NULL,
           gc_claimed_at = NULL,
           gc_claim_expires_at = NULL,
           gc_last_error = NULL,
           gc_deleted_at = p_now
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value
       AND lifecycle_state = 'gc_claimed'
       AND gc_claim_token = p_claim_token;
    IF NOT FOUND THEN RETURN false; END IF;
    INSERT INTO object_gc_attempt
        (digest_algorithm, digest_value, claim_token, event, event_at)
    VALUES (p_digest_algorithm, p_digest_value, p_claim_token, 'deleted', p_now);
    RETURN true;
END
$function$;

REVOKE ALL ON object_retention_policy, object_retention_assignment,
    object_gc_attempt FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard;
REVOKE UPDATE, DELETE, TRUNCATE ON object_blob
    FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard, leo_maintenance;
REVOKE ALL ON FUNCTION object_blob_assert_live_reference() FROM PUBLIC;
REVOKE ALL ON FUNCTION register_live_object_blob(text, text, bigint, text, text, text)
    FROM PUBLIC, leo_dashboard;
REVOKE ALL ON FUNCTION gc_claim_object(text, text, text, timestamptz, timestamptz)
    FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard;
REVOKE ALL ON FUNCTION gc_record_delete_failure(text, text, text, timestamptz, text)
    FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard;
REVOKE ALL ON FUNCTION gc_complete_object_delete(text, text, text, timestamptz)
    FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard;

GRANT USAGE ON SCHEMA public TO leo_maintenance;
GRANT SELECT ON object_blob, object_blob_live_reference, object_retention_status,
    object_gc_candidate,
    object_retention_policy, object_retention_assignment, object_gc_attempt
TO leo_maintenance;
GRANT INSERT ON object_retention_policy, object_retention_assignment TO leo_maintenance;
GRANT USAGE, SELECT ON SEQUENCE object_gc_attempt_attempt_id_seq TO leo_maintenance;
GRANT EXECUTE ON FUNCTION gc_claim_object(text, text, text, timestamptz, timestamptz),
    gc_record_delete_failure(text, text, text, timestamptz, text),
    gc_complete_object_delete(text, text, text, timestamptz)
TO leo_maintenance;
GRANT EXECUTE ON FUNCTION register_live_object_blob(text, text, bigint, text, text, text)
TO leo_capture, leo_analysis;

GRANT SELECT ON object_retention_status, object_gc_candidate TO leo_dashboard;

COMMIT;
