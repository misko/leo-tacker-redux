BEGIN;

CREATE TABLE object_blob (
    digest_algorithm text NOT NULL CHECK (digest_algorithm = 'sha256'),
    digest_value text NOT NULL CHECK (digest_value ~ '^[0-9a-f]{64}$'),
    byte_count bigint NOT NULL CHECK (byte_count >= 0),
    media_type text NOT NULL,
    format_id text NOT NULL,
    locator text NOT NULL UNIQUE,
    verified_at timestamptz,
    PRIMARY KEY (digest_algorithm, digest_value)
);

CREATE TABLE recording (
    recording_id text PRIMARY KEY,
    data_digest_algorithm text NOT NULL DEFAULT 'sha256',
    data_digest_value text NOT NULL,
    metadata_digest_algorithm text NOT NULL DEFAULT 'sha256',
    metadata_digest_value text NOT NULL,
    manifest_digest_value text NOT NULL CHECK (manifest_digest_value ~ '^[0-9a-f]{64}$'),
    idempotency_key text NOT NULL UNIQUE,
    state text NOT NULL CHECK (state = 'published'),
    published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (data_digest_value <> metadata_digest_value),
    FOREIGN KEY (data_digest_algorithm, data_digest_value)
        REFERENCES object_blob (digest_algorithm, digest_value),
    FOREIGN KEY (metadata_digest_algorithm, metadata_digest_value)
        REFERENCES object_blob (digest_algorithm, digest_value)
);

CREATE TABLE job (
    job_id text PRIMARY KEY,
    job_type text NOT NULL,
    payload_schema_id text NOT NULL,
    payload_schema_version text NOT NULL,
    payload jsonb NOT NULL,
    state text NOT NULL CHECK (state IN ('ready', 'leased', 'failed', 'succeeded')),
    available_at_utc timestamptz NOT NULL,
    attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    lease_token text,
    lease_generation bigint NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
    lease_expires_utc timestamptz,
    result_ref jsonb,
    last_error text,
    CHECK (
        (state = 'leased' AND lease_token IS NOT NULL AND lease_expires_utc IS NOT NULL)
        OR
        (state <> 'leased' AND lease_token IS NULL AND lease_expires_utc IS NULL)
    )
);

CREATE INDEX job_claim_idx
    ON job (job_type, available_at_utc, job_id)
    WHERE state IN ('ready', 'failed', 'leased');

COMMIT;
