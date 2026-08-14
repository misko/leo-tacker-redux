BEGIN;

CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE hardware_snapshot (
    snapshot_id text PRIMARY KEY
        CHECK (snapshot_id ~ '^hw_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    snapshot_digest_algorithm text NOT NULL CHECK (snapshot_digest_algorithm = 'sha256'),
    snapshot_digest_value text NOT NULL CHECK (snapshot_digest_value ~ '^[0-9a-f]{64}$'),
    bundle_digest_algorithm text NOT NULL DEFAULT 'sha256',
    bundle_digest_value text NOT NULL,
    station_id text NOT NULL CHECK (station_id ~ '^station_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    radio_count integer NOT NULL CHECK (radio_count > 0),
    chain_count integer NOT NULL CHECK (chain_count >= 0),
    idempotency_key text NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (snapshot_digest_algorithm, snapshot_digest_value),
    FOREIGN KEY (bundle_digest_algorithm, bundle_digest_value)
        REFERENCES object_blob (digest_algorithm, digest_value),
    CHECK (snapshot_digest_algorithm = bundle_digest_algorithm),
    CHECK (snapshot_digest_value = bundle_digest_value)
);

CREATE TABLE hardware_radio (
    snapshot_id text NOT NULL REFERENCES hardware_snapshot (snapshot_id),
    radio_index integer NOT NULL CHECK (radio_index >= 0),
    radio_id text NOT NULL
        CHECK (radio_id ~ '^radio_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    PRIMARY KEY (snapshot_id, radio_index),
    UNIQUE (snapshot_id, radio_id)
);

CREATE TABLE hardware_receiver_chain (
    snapshot_id text NOT NULL REFERENCES hardware_snapshot (snapshot_id),
    chain_index integer NOT NULL CHECK (chain_index >= 0),
    receiver_chain_id text NOT NULL
        CHECK (receiver_chain_id ~ '^rx_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    radio_id text NOT NULL,
    radio_channel integer NOT NULL CHECK (radio_channel >= 0),
    lnb_id text NOT NULL CHECK (lnb_id <> ''),
    polarization text CHECK (polarization IS NULL OR polarization <> ''),
    cable_id text CHECK (cable_id IS NULL OR cable_id <> ''),
    valid_from_utc_ns bigint NOT NULL CHECK (valid_from_utc_ns >= 0),
    valid_until_utc_ns bigint,
    PRIMARY KEY (snapshot_id, chain_index),
    UNIQUE (snapshot_id, receiver_chain_id, valid_from_utc_ns),
    FOREIGN KEY (snapshot_id, radio_id)
        REFERENCES hardware_radio (snapshot_id, radio_id),
    CHECK (valid_until_utc_ns IS NULL OR valid_until_utc_ns > valid_from_utc_ns),
    EXCLUDE USING gist (
        snapshot_id WITH =,
        receiver_chain_id WITH =,
        int8range(valid_from_utc_ns, valid_until_utc_ns, '[)') WITH &&
    )
);

CREATE INDEX hardware_receiver_effective_idx
    ON hardware_receiver_chain
    (snapshot_id, receiver_chain_id, valid_from_utc_ns, valid_until_utc_ns);

GRANT SELECT, INSERT ON object_blob, hardware_snapshot, hardware_radio,
    hardware_receiver_chain
TO leo_analysis;
GRANT SELECT ON hardware_snapshot, hardware_radio, hardware_receiver_chain
TO leo_capture, leo_dashboard;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON hardware_snapshot, hardware_radio,
    hardware_receiver_chain
FROM leo_capture, leo_dashboard;
REVOKE UPDATE, DELETE, TRUNCATE ON hardware_snapshot, hardware_radio,
    hardware_receiver_chain
FROM leo_analysis;

COMMIT;
