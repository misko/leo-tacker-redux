BEGIN;

ALTER TABLE hardware_snapshot
    ADD CONSTRAINT hardware_snapshot_exact_ref_unique
    UNIQUE (snapshot_id, snapshot_digest_algorithm, snapshot_digest_value);

CREATE TABLE recording_hardware_link (
    link_id text PRIMARY KEY CHECK (link_id ~ '^hwlink_[0-9a-f]{32}$'),
    recording_id text NOT NULL UNIQUE REFERENCES recording(recording_id),
    recording_identity_digest_algorithm text NOT NULL
        CHECK (recording_identity_digest_algorithm = 'sha256'),
    recording_identity_digest_value text NOT NULL
        CHECK (recording_identity_digest_value ~ '^[0-9a-f]{64}$'),
    hardware_snapshot_id text NOT NULL,
    hardware_snapshot_digest_algorithm text NOT NULL
        CHECK (hardware_snapshot_digest_algorithm = 'sha256'),
    hardware_snapshot_digest_value text NOT NULL
        CHECK (hardware_snapshot_digest_value ~ '^[0-9a-f]{64}$'),
    link_digest_algorithm text NOT NULL CHECK (link_digest_algorithm = 'sha256'),
    link_digest_value text NOT NULL UNIQUE
        CHECK (link_digest_value ~ '^[0-9a-f]{64}$'),
    idempotency_key text NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (
        hardware_snapshot_id,
        hardware_snapshot_digest_algorithm,
        hardware_snapshot_digest_value
    ) REFERENCES hardware_snapshot (
        snapshot_id,
        snapshot_digest_algorithm,
        snapshot_digest_value
    )
);

GRANT SELECT, INSERT ON recording_hardware_link TO leo_analysis;
GRANT SELECT ON recording_hardware_link TO leo_dashboard;
REVOKE ALL ON recording_hardware_link FROM leo_capture;
REVOKE UPDATE, DELETE, TRUNCATE ON recording_hardware_link
FROM leo_analysis, leo_dashboard;

COMMIT;
