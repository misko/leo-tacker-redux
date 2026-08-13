BEGIN;

CREATE TABLE ephemeris_snapshot (
    snapshot_id text PRIMARY KEY
        CHECK (snapshot_id ~ '^eph_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    retrieval_id text NOT NULL UNIQUE
        CHECK (retrieval_id ~ '^ephret_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    source text NOT NULL CHECK (source IN ('space-track', 'huggingface')),
    scope text NOT NULL CHECK (scope ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    retrieved_at_utc_ns bigint NOT NULL CHECK (retrieved_at_utc_ns >= 0),
    raw_digest_algorithm text NOT NULL DEFAULT 'sha256',
    raw_digest_value text NOT NULL,
    normalized_digest_algorithm text NOT NULL DEFAULT 'sha256',
    normalized_digest_value text NOT NULL,
    provenance_digest_algorithm text NOT NULL DEFAULT 'sha256',
    provenance_digest_value text NOT NULL,
    parser_artifact_id text NOT NULL
        CHECK (parser_artifact_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    parser_digest_algorithm text NOT NULL CHECK (parser_digest_algorithm = 'sha256'),
    parser_digest_value text NOT NULL CHECK (parser_digest_value ~ '^[0-9a-f]{64}$'),
    parser_schema_id text,
    parser_schema_version text,
    satellite_count integer NOT NULL CHECK (satellite_count >= 0),
    norad_id_set_digest_algorithm text NOT NULL CHECK (norad_id_set_digest_algorithm = 'sha256'),
    norad_id_set_digest_value text NOT NULL CHECK (norad_id_set_digest_value ~ '^[0-9a-f]{64}$'),
    element_epoch_min_utc_ns bigint NOT NULL CHECK (element_epoch_min_utc_ns >= 0),
    element_epoch_max_utc_ns bigint NOT NULL CHECK (element_epoch_max_utc_ns >= element_epoch_min_utc_ns),
    validation_policy_artifact_id text NOT NULL
        CHECK (validation_policy_artifact_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    validation_policy_digest_algorithm text NOT NULL CHECK (validation_policy_digest_algorithm = 'sha256'),
    validation_policy_digest_value text NOT NULL CHECK (validation_policy_digest_value ~ '^[0-9a-f]{64}$'),
    validation_policy_schema_id text,
    validation_policy_schema_version text,
    validation_reason_codes jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(validation_reason_codes) = 'array'),
    attribution text NOT NULL CHECK (attribution <> ''),
    request_spec_digest text NOT NULL CHECK (request_spec_digest ~ '^[0-9a-f]{64}$'),
    published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (raw_digest_algorithm, raw_digest_value)
        REFERENCES object_blob (digest_algorithm, digest_value),
    FOREIGN KEY (normalized_digest_algorithm, normalized_digest_value)
        REFERENCES object_blob (digest_algorithm, digest_value),
    FOREIGN KEY (provenance_digest_algorithm, provenance_digest_value)
        REFERENCES object_blob (digest_algorithm, digest_value),
    CHECK (raw_digest_value <> normalized_digest_value),
    CHECK (raw_digest_value <> provenance_digest_value),
    CHECK (normalized_digest_value <> provenance_digest_value),
    CHECK ((parser_schema_id IS NULL) = (parser_schema_version IS NULL)),
    CHECK (
        (validation_policy_schema_id IS NULL) =
        (validation_policy_schema_version IS NULL)
    )
);

CREATE INDEX ephemeris_snapshot_history_idx
    ON ephemeris_snapshot (source, scope, retrieved_at_utc_ns, snapshot_id);

GRANT SELECT, INSERT ON object_blob, ephemeris_snapshot TO leo_analysis;
GRANT SELECT ON ephemeris_snapshot TO leo_dashboard;
REVOKE ALL ON ephemeris_snapshot FROM leo_capture;
REVOKE UPDATE, DELETE, TRUNCATE ON ephemeris_snapshot FROM leo_analysis, leo_dashboard;

COMMIT;
