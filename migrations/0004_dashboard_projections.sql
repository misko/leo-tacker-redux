BEGIN;

CREATE SEQUENCE dashboard_projection_sequence AS bigint;

CREATE TABLE dashboard_recording_projection (
    projection_sequence bigint PRIMARY KEY
        DEFAULT nextval('dashboard_projection_sequence'),
    recording_id text NOT NULL REFERENCES recording (recording_id),
    radio_id text NOT NULL CHECK (radio_id <> ''),
    started_utc_ns bigint NOT NULL CHECK (started_utc_ns >= 0),
    finished_utc_ns bigint NOT NULL CHECK (finished_utc_ns > started_utc_ns),
    analysis_state text NOT NULL CHECK (analysis_state <> ''),
    segment_count integer NOT NULL CHECK (segment_count >= 0),
    recording_object_available boolean NOT NULL,
    UNIQUE (recording_id, projection_sequence)
);

CREATE INDEX dashboard_recording_recent_idx
    ON dashboard_recording_projection
       (started_utc_ns DESC, recording_id DESC, projection_sequence);

CREATE TABLE dashboard_activity_projection (
    projection_sequence bigint PRIMARY KEY
        DEFAULT nextval('dashboard_projection_sequence'),
    activity_id text NOT NULL CHECK (activity_id <> ''),
    recording_id text NOT NULL REFERENCES recording (recording_id),
    radio_id text NOT NULL CHECK (radio_id <> ''),
    kind text NOT NULL CHECK (kind IN ('scan', 'dwell', 'calibration', 'test')),
    started_utc_ns bigint NOT NULL CHECK (started_utc_ns >= 0),
    UNIQUE (activity_id, projection_sequence)
);

CREATE INDEX dashboard_activity_count_idx
    ON dashboard_activity_projection
       (started_utc_ns, radio_id, kind, projection_sequence);

CREATE TABLE dashboard_feature_projection (
    projection_sequence bigint PRIMARY KEY
        DEFAULT nextval('dashboard_projection_sequence'),
    feature_id text NOT NULL CHECK (feature_id <> ''),
    recording_id text NOT NULL REFERENCES recording (recording_id),
    method_id text NOT NULL CHECK (method_id <> ''),
    score double precision NOT NULL
        CHECK (score NOT IN ('NaN'::double precision,
                             'Infinity'::double precision,
                             '-Infinity'::double precision)),
    score_semantics text NOT NULL CHECK (score_semantics <> ''),
    UNIQUE (feature_id, projection_sequence)
);

CREATE INDEX dashboard_feature_recording_idx
    ON dashboard_feature_projection
       (recording_id, feature_id, projection_sequence);

CREATE TABLE dashboard_model_projection (
    projection_sequence bigint PRIMARY KEY
        DEFAULT nextval('dashboard_projection_sequence'),
    model_snapshot_id text NOT NULL CHECK (model_snapshot_id <> ''),
    release_alias text CHECK (release_alias <> ''),
    parameter_count integer NOT NULL CHECK (parameter_count >= 0),
    warnings jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(warnings) = 'array'),
    UNIQUE (model_snapshot_id, projection_sequence)
);

CREATE INDEX dashboard_model_identity_idx
    ON dashboard_model_projection
       (model_snapshot_id, release_alias, projection_sequence DESC);

CREATE TABLE dashboard_track_projection (
    projection_sequence bigint PRIMARY KEY
        DEFAULT nextval('dashboard_projection_sequence'),
    track_id text NOT NULL CHECK (track_id <> ''),
    model_snapshot_id text NOT NULL CHECK (model_snapshot_id <> ''),
    radio_id text NOT NULL CHECK (radio_id <> ''),
    started_utc_ns bigint NOT NULL CHECK (started_utc_ns >= 0),
    finished_utc_ns bigint NOT NULL CHECK (finished_utc_ns > started_utc_ns),
    UNIQUE (track_id, projection_sequence)
);

CREATE INDEX dashboard_track_recent_idx
    ON dashboard_track_projection
       (started_utc_ns DESC, track_id DESC, projection_sequence);

CREATE TABLE dashboard_storage_health_projection (
    projection_sequence bigint PRIMARY KEY
        DEFAULT nextval('dashboard_projection_sequence'),
    available boolean NOT NULL,
    total_bytes bigint CHECK (total_bytes >= 0),
    free_bytes bigint CHECK (free_bytes >= 0),
    CHECK ((total_bytes IS NULL) = (free_bytes IS NULL)),
    CHECK (total_bytes IS NULL OR free_bytes <= total_bytes)
);

GRANT SELECT ON
    dashboard_recording_projection,
    dashboard_activity_projection,
    dashboard_feature_projection,
    dashboard_model_projection,
    dashboard_track_projection,
    dashboard_storage_health_projection
TO leo_dashboard;

GRANT SELECT, INSERT ON
    dashboard_recording_projection,
    dashboard_activity_projection
TO leo_capture;

GRANT SELECT, INSERT ON
    dashboard_recording_projection,
    dashboard_activity_projection,
    dashboard_feature_projection,
    dashboard_model_projection,
    dashboard_track_projection,
    dashboard_storage_health_projection
TO leo_analysis;

GRANT USAGE, SELECT ON SEQUENCE dashboard_projection_sequence
TO leo_capture, leo_analysis;

GRANT SELECT ON SEQUENCE dashboard_projection_sequence
TO leo_dashboard;

REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON
    dashboard_recording_projection,
    dashboard_activity_projection,
    dashboard_feature_projection,
    dashboard_model_projection,
    dashboard_track_projection,
    dashboard_storage_health_projection
FROM leo_dashboard;

REVOKE UPDATE, DELETE, TRUNCATE ON
    dashboard_recording_projection,
    dashboard_activity_projection
FROM leo_capture;

REVOKE UPDATE, DELETE, TRUNCATE ON
    dashboard_recording_projection,
    dashboard_activity_projection,
    dashboard_feature_projection,
    dashboard_model_projection,
    dashboard_track_projection,
    dashboard_storage_health_projection
FROM leo_analysis;

COMMIT;
