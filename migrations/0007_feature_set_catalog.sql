BEGIN;

CREATE TABLE feature_set (
    feature_set_id text PRIMARY KEY
        CHECK (feature_set_id ~ '^fset_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    analysis_run_id text NOT NULL
        CHECK (analysis_run_id ~ '^arun_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    recording_id text NOT NULL REFERENCES recording (recording_id),
    input_recording_digest_algorithm text NOT NULL
        CHECK (input_recording_digest_algorithm = 'sha256'),
    input_recording_digest_value text NOT NULL
        CHECK (input_recording_digest_value ~ '^[0-9a-f]{64}$'),
    request_digest_algorithm text NOT NULL
        CHECK (request_digest_algorithm = 'sha256'),
    request_digest_value text NOT NULL
        CHECK (request_digest_value ~ '^[0-9a-f]{64}$'),
    bundle_digest_algorithm text NOT NULL DEFAULT 'sha256',
    bundle_digest_value text NOT NULL,
    observation_count integer NOT NULL CHECK (observation_count >= 0),
    method_score_count integer NOT NULL CHECK (method_score_count >= 0),
    idempotency_key text NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (bundle_digest_algorithm, bundle_digest_value),
    UNIQUE (
        feature_set_id, analysis_run_id,
        bundle_digest_algorithm, bundle_digest_value
    ),
    FOREIGN KEY (bundle_digest_algorithm, bundle_digest_value)
        REFERENCES object_blob (digest_algorithm, digest_value)
);

ALTER TABLE dataset_member
    ADD CONSTRAINT dataset_member_authoritative_feature_fk
    FOREIGN KEY (
        feature_set_id, analysis_run_id,
        feature_digest_algorithm, feature_digest_value
    ) REFERENCES feature_set (
        feature_set_id, analysis_run_id,
        bundle_digest_algorithm, bundle_digest_value
    );

CREATE INDEX feature_set_recording_idx
    ON feature_set (recording_id, feature_set_id);

GRANT SELECT, INSERT ON feature_set TO leo_analysis;
GRANT SELECT ON feature_set TO leo_dashboard;
REVOKE ALL ON feature_set FROM leo_capture;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON feature_set FROM leo_dashboard;
REVOKE UPDATE, DELETE, TRUNCATE ON feature_set FROM leo_analysis;

COMMIT;
