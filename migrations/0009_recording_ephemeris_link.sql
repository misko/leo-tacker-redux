BEGIN;

ALTER TABLE ephemeris_snapshot ADD CONSTRAINT ephemeris_snapshot_exact_ref_unique
    UNIQUE (snapshot_id, raw_digest_algorithm, raw_digest_value,
            normalized_digest_algorithm, normalized_digest_value);

CREATE FUNCTION serialize_ephemeris_history_insert() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended(length(NEW.source)::text || ':' || NEW.source || NEW.scope, 0)
    );
    RETURN NEW;
END
$$;

CREATE TRIGGER serialize_ephemeris_history_insert
BEFORE INSERT ON ephemeris_snapshot FOR EACH ROW
EXECUTE FUNCTION serialize_ephemeris_history_insert();

CREATE TABLE recording_ephemeris_link (
    link_id text PRIMARY KEY CHECK (link_id ~ '^ephlink_[0-9a-f]{32}$'),
    recording_id text NOT NULL REFERENCES recording(recording_id),
    recording_identity_digest_algorithm text NOT NULL CHECK (recording_identity_digest_algorithm = 'sha256'),
    recording_identity_digest_value text NOT NULL CHECK (recording_identity_digest_value ~ '^[0-9a-f]{64}$'),
    recording_started_utc_ns bigint NOT NULL CHECK (recording_started_utc_ns >= 0),
    recording_finished_utc_ns bigint NOT NULL CHECK (recording_finished_utc_ns > recording_started_utc_ns),
    source text NOT NULL CHECK (source IN ('space-track', 'huggingface')),
    scope text NOT NULL CHECK (scope ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    selection_policy text NOT NULL CHECK (selection_policy IN ('available_then', 'first_after')),
    policy_artifact_id text NOT NULL CHECK (policy_artifact_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    policy_digest_algorithm text NOT NULL CHECK (policy_digest_algorithm = 'sha256'),
    policy_digest_value text NOT NULL CHECK (policy_digest_value ~ '^[0-9a-f]{64}$'),
    policy_schema_id text,
    policy_schema_version text,
    as_of_utc_ns bigint NOT NULL CHECK (as_of_utc_ns >= 0),
    snapshot_id text NOT NULL REFERENCES ephemeris_snapshot(snapshot_id),
    raw_digest_algorithm text NOT NULL,
    raw_digest_value text NOT NULL,
    normalized_digest_algorithm text NOT NULL,
    normalized_digest_value text NOT NULL,
    link_digest_algorithm text NOT NULL CHECK (link_digest_algorithm = 'sha256'),
    link_digest_value text NOT NULL CHECK (link_digest_value ~ '^[0-9a-f]{64}$'),
    idempotency_key text NOT NULL UNIQUE,
    published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (recording_id, source, scope, selection_policy, policy_digest_value, as_of_utc_ns),
    CHECK ((policy_schema_id IS NULL) = (policy_schema_version IS NULL)),
    FOREIGN KEY (snapshot_id, raw_digest_algorithm, raw_digest_value,
                 normalized_digest_algorithm, normalized_digest_value)
        REFERENCES ephemeris_snapshot(snapshot_id, raw_digest_algorithm, raw_digest_value,
                                      normalized_digest_algorithm, normalized_digest_value)
);

GRANT SELECT, INSERT ON recording_ephemeris_link TO leo_analysis;
GRANT SELECT ON recording_ephemeris_link TO leo_dashboard;
REVOKE ALL ON recording_ephemeris_link FROM leo_capture;
REVOKE UPDATE, DELETE, TRUNCATE ON recording_ephemeris_link FROM leo_analysis, leo_dashboard;

COMMIT;
