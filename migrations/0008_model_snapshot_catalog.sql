BEGIN;

ALTER TABLE dataset_snapshot
    ADD CONSTRAINT dataset_snapshot_model_authority_key
    UNIQUE (
        snapshot_id,
        feature_membership_digest_algorithm,
        feature_membership_digest_value
    );

CREATE TABLE model_snapshot (
    model_snapshot_id text PRIMARY KEY
        CHECK (model_snapshot_id ~ '^model_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    model_run_id text NOT NULL UNIQUE
        CHECK (model_run_id ~ '^mrun_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    dataset_snapshot_id text NOT NULL,
    dataset_membership_digest_algorithm text NOT NULL
        CHECK (dataset_membership_digest_algorithm = 'sha256'),
    dataset_membership_digest_value text NOT NULL
        CHECK (dataset_membership_digest_value ~ '^[0-9a-f]{64}$'),
    request_digest_algorithm text NOT NULL
        CHECK (request_digest_algorithm = 'sha256'),
    request_digest_value text NOT NULL
        CHECK (request_digest_value ~ '^[0-9a-f]{64}$'),
    provenance_digest_algorithm text NOT NULL
        CHECK (provenance_digest_algorithm = 'sha256'),
    provenance_digest_value text NOT NULL
        CHECK (provenance_digest_value ~ '^[0-9a-f]{64}$'),
    bundle_digest_algorithm text NOT NULL DEFAULT 'sha256',
    bundle_digest_value text NOT NULL,
    parameter_count integer NOT NULL CHECK (parameter_count >= 0),
    idempotency_key text NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (bundle_digest_algorithm, bundle_digest_value),
    UNIQUE (
        model_snapshot_id, model_run_id,
        bundle_digest_algorithm, bundle_digest_value
    ),
    FOREIGN KEY (bundle_digest_algorithm, bundle_digest_value)
        REFERENCES object_blob (digest_algorithm, digest_value),
    FOREIGN KEY (
        dataset_snapshot_id,
        dataset_membership_digest_algorithm,
        dataset_membership_digest_value
    ) REFERENCES dataset_snapshot (
        snapshot_id,
        feature_membership_digest_algorithm,
        feature_membership_digest_value
    )
);

CREATE INDEX model_snapshot_dataset_idx
    ON model_snapshot (dataset_snapshot_id, model_snapshot_id);

CREATE TABLE model_release (
    release_sequence bigserial PRIMARY KEY,
    alias text NOT NULL CHECK (alias <> ''),
    model_snapshot_id text NOT NULL,
    model_run_id text NOT NULL,
    bundle_digest_algorithm text NOT NULL DEFAULT 'sha256',
    bundle_digest_value text NOT NULL,
    approved_by text NOT NULL CHECK (approved_by <> ''),
    approved_utc_ns bigint NOT NULL CHECK (approved_utc_ns >= 0),
    rationale text NOT NULL CHECK (rationale <> ''),
    approval_digest_algorithm text NOT NULL
        CHECK (approval_digest_algorithm = 'sha256'),
    approval_digest_value text NOT NULL
        CHECK (approval_digest_value ~ '^[0-9a-f]{64}$'),
    idempotency_key text NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    released_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (
        alias, model_snapshot_id,
        approval_digest_algorithm, approval_digest_value
    ),
    FOREIGN KEY (
        model_snapshot_id, model_run_id,
        bundle_digest_algorithm, bundle_digest_value
    ) REFERENCES model_snapshot (
        model_snapshot_id, model_run_id,
        bundle_digest_algorithm, bundle_digest_value
    )
);

CREATE INDEX model_release_current_idx
    ON model_release (alias, release_sequence DESC);

GRANT SELECT, INSERT ON model_snapshot, model_release TO leo_analysis;
GRANT USAGE, SELECT ON SEQUENCE model_release_release_sequence_seq TO leo_analysis;
GRANT SELECT ON model_snapshot, model_release TO leo_dashboard;
REVOKE ALL ON model_snapshot, model_release FROM leo_capture;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON model_snapshot, model_release
FROM leo_dashboard;
REVOKE UPDATE, DELETE, TRUNCATE ON model_snapshot, model_release
FROM leo_analysis;
REVOKE USAGE ON SEQUENCE model_release_release_sequence_seq FROM leo_dashboard;

COMMIT;
