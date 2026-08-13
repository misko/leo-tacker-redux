BEGIN;

CREATE TABLE dashboard_capture_projection_identity (
    projection_kind text NOT NULL
        CHECK (projection_kind IN ('recording', 'activity')),
    logical_id text NOT NULL CHECK (logical_id <> ''),
    authoritative_identity_digest text NOT NULL
        CHECK (authoritative_identity_digest ~ '^sha256:[0-9a-f]{64}$'),
    authoritative_identity jsonb NOT NULL
        CHECK (jsonb_typeof(authoritative_identity) = 'object'),
    first_projection_sequence bigint NOT NULL CHECK (first_projection_sequence >= 0),
    PRIMARY KEY (projection_kind, logical_id)
);

CREATE TABLE dashboard_analysis_projection_identity (
    projection_kind text NOT NULL
        CHECK (projection_kind IN ('feature', 'model', 'release', 'track')),
    logical_id text NOT NULL CHECK (logical_id <> ''),
    authoritative_identity_digest text NOT NULL
        CHECK (authoritative_identity_digest ~ '^sha256:[0-9a-f]{64}$'),
    authoritative_identity jsonb NOT NULL
        CHECK (jsonb_typeof(authoritative_identity) = 'object'),
    first_projection_sequence bigint NOT NULL CHECK (first_projection_sequence >= 0),
    PRIMARY KEY (projection_kind, logical_id)
);

GRANT SELECT, INSERT ON dashboard_capture_projection_identity TO leo_capture;
GRANT SELECT, INSERT ON dashboard_analysis_projection_identity TO leo_analysis;

REVOKE ALL ON
    dashboard_capture_projection_identity,
    dashboard_analysis_projection_identity
FROM leo_dashboard;

REVOKE UPDATE, DELETE, TRUNCATE ON
    dashboard_capture_projection_identity
FROM leo_capture;

REVOKE UPDATE, DELETE, TRUNCATE ON
    dashboard_analysis_projection_identity
FROM leo_analysis;

COMMIT;
