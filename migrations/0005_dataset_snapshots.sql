BEGIN;

CREATE TABLE dataset_snapshot (
    snapshot_id text PRIMARY KEY
        CHECK (snapshot_id ~ '^dataset_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    feature_membership_digest_algorithm text NOT NULL
        CHECK (feature_membership_digest_algorithm = 'sha256'),
    feature_membership_digest_value text NOT NULL
        CHECK (feature_membership_digest_value ~ '^[0-9a-f]{64}$'),
    snapshot_digest_algorithm text NOT NULL
        CHECK (snapshot_digest_algorithm = 'sha256'),
    snapshot_digest_value text NOT NULL
        CHECK (snapshot_digest_value ~ '^[0-9a-f]{64}$'),
    bundle_digest_algorithm text NOT NULL DEFAULT 'sha256',
    bundle_digest_value text NOT NULL,
    evaluated_method_id text NOT NULL CHECK (evaluated_method_id <> ''),
    selection_spec text NOT NULL CHECK (selection_spec <> ''),
    selection_cutoff_utc_ns bigint NOT NULL CHECK (selection_cutoff_utc_ns >= 0),
    promoted boolean NOT NULL,
    promotion_warnings jsonb NOT NULL
        CHECK (jsonb_typeof(promotion_warnings) = 'array'),
    member_count integer NOT NULL CHECK (member_count > 0),
    idempotency_key text NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (snapshot_digest_algorithm, snapshot_digest_value),
    FOREIGN KEY (bundle_digest_algorithm, bundle_digest_value)
        REFERENCES object_blob (digest_algorithm, digest_value),
    CHECK (promoted = (promotion_warnings = '[]'::jsonb))
);

CREATE TABLE dataset_member (
    snapshot_id text NOT NULL REFERENCES dataset_snapshot (snapshot_id),
    member_index integer NOT NULL CHECK (member_index >= 0),
    feature_set_id text NOT NULL
        CHECK (feature_set_id ~ '^fset_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    analysis_run_id text NOT NULL
        CHECK (analysis_run_id ~ '^arun_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    feature_digest_algorithm text NOT NULL
        CHECK (feature_digest_algorithm = 'sha256'),
    feature_digest_value text NOT NULL
        CHECK (feature_digest_value ~ '^[0-9a-f]{64}$'),
    feature_byte_count bigint NOT NULL CHECK (feature_byte_count >= 0),
    feature_media_type text NOT NULL CHECK (feature_media_type <> ''),
    feature_format_id text NOT NULL CHECK (feature_format_id <> ''),
    feature_locator text NOT NULL CHECK (feature_locator <> ''),
    split_group_id text NOT NULL CHECK (split_group_id <> ''),
    split text NOT NULL CHECK (split IN ('train', 'validation', 'locked_test')),
    role text NOT NULL CHECK (role IN ('scored_truth', 'context_only')),
    truth jsonb NOT NULL CHECK (jsonb_typeof(truth) = 'object'),
    PRIMARY KEY (snapshot_id, member_index),
    UNIQUE (snapshot_id, feature_set_id)
);

-- Feature bundle identity is projected exactly, but intentionally has no
-- object_blob foreign key until an authoritative feature publication catalog
-- exists. The dataset bundle, not this row, remains the source of truth.

CREATE INDEX dataset_member_split_idx
    ON dataset_member (snapshot_id, split, role, member_index);

GRANT SELECT, INSERT ON object_blob, dataset_snapshot, dataset_member
TO leo_analysis;

GRANT SELECT ON dataset_snapshot, dataset_member TO leo_dashboard;

REVOKE ALL ON dataset_snapshot, dataset_member FROM leo_capture;

REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON dataset_snapshot, dataset_member
FROM leo_dashboard;

REVOKE UPDATE, DELETE, TRUNCATE ON dataset_snapshot, dataset_member
FROM leo_analysis;

COMMIT;
