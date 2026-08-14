BEGIN;

ALTER TABLE dataset_snapshot
    ADD CONSTRAINT dataset_snapshot_tracking_input_authority_key
    UNIQUE (
        snapshot_id,
        feature_membership_digest_algorithm,
        feature_membership_digest_value,
        snapshot_digest_algorithm,
        snapshot_digest_value
    );

ALTER TABLE dataset_member
    ADD CONSTRAINT dataset_member_tracking_input_authority_key
    UNIQUE (
        snapshot_id, feature_set_id, analysis_run_id,
        feature_digest_algorithm, feature_digest_value
    );

ALTER TABLE feature_set
    ADD CONSTRAINT feature_set_tracking_input_authority_key
    UNIQUE (
        feature_set_id, analysis_run_id,
        bundle_digest_algorithm, bundle_digest_value, recording_id
    );

ALTER TABLE recording_hardware_link
    ADD CONSTRAINT recording_hardware_link_tracking_input_authority_key
    UNIQUE (
        link_id, link_digest_algorithm, link_digest_value, recording_id,
        recording_identity_digest_algorithm, recording_identity_digest_value
    );

ALTER TABLE recording_ephemeris_link
    ADD CONSTRAINT recording_ephemeris_link_tracking_input_authority_key
    UNIQUE (
        link_id, link_digest_algorithm, link_digest_value, recording_id,
        recording_identity_digest_algorithm, recording_identity_digest_value
    );

CREATE TABLE tracking_input_snapshot (
    snapshot_id text PRIMARY KEY
        CHECK (snapshot_id ~ '^trackinput_[0-9a-f]{32}$'),
    snapshot_digest_algorithm text NOT NULL
        CHECK (snapshot_digest_algorithm = 'sha256'),
    snapshot_digest_value text NOT NULL
        CHECK (snapshot_digest_value ~ '^[0-9a-f]{64}$'),
    membership_digest_algorithm text NOT NULL
        CHECK (membership_digest_algorithm = 'sha256'),
    membership_digest_value text NOT NULL
        CHECK (membership_digest_value ~ '^[0-9a-f]{64}$'),
    dataset_snapshot_id text NOT NULL,
    dataset_membership_digest_algorithm text NOT NULL
        CHECK (dataset_membership_digest_algorithm = 'sha256'),
    dataset_membership_digest_value text NOT NULL
        CHECK (dataset_membership_digest_value ~ '^[0-9a-f]{64}$'),
    dataset_snapshot_digest_algorithm text NOT NULL
        CHECK (dataset_snapshot_digest_algorithm = 'sha256'),
    dataset_snapshot_digest_value text NOT NULL
        CHECK (dataset_snapshot_digest_value ~ '^[0-9a-f]{64}$'),
    builder_artifact_id text NOT NULL
        CHECK (builder_artifact_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    builder_digest_algorithm text NOT NULL
        CHECK (builder_digest_algorithm = 'sha256'),
    builder_digest_value text NOT NULL
        CHECK (builder_digest_value ~ '^[0-9a-f]{64}$'),
    builder_schema_id text NOT NULL
        CHECK (builder_schema_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    builder_schema_version text NOT NULL
        CHECK (builder_schema_version ~ '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'),
    selector_artifact_id text NOT NULL
        CHECK (selector_artifact_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    selector_digest_algorithm text NOT NULL
        CHECK (selector_digest_algorithm = 'sha256'),
    selector_digest_value text NOT NULL
        CHECK (selector_digest_value ~ '^[0-9a-f]{64}$'),
    selector_schema_id text NOT NULL
        CHECK (selector_schema_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    selector_schema_version text NOT NULL
        CHECK (selector_schema_version ~ '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'),
    provenance_digest_algorithm text NOT NULL
        CHECK (provenance_digest_algorithm = 'sha256'),
    provenance_digest_value text NOT NULL
        CHECK (provenance_digest_value ~ '^[0-9a-f]{64}$'),
    bundle_digest_algorithm text NOT NULL
        CHECK (bundle_digest_algorithm = 'sha256'),
    bundle_digest_value text NOT NULL
        CHECK (bundle_digest_value ~ '^[0-9a-f]{64}$'),
    entry_count integer NOT NULL CHECK (entry_count > 0 AND entry_count <= 100000),
    idempotency_key text NOT NULL UNIQUE
        CHECK (idempotency_key ~ '^[^[:space:]]+$'),
    published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (snapshot_digest_algorithm, snapshot_digest_value),
    UNIQUE (bundle_digest_algorithm, bundle_digest_value),
    UNIQUE (
        snapshot_id, snapshot_digest_algorithm, snapshot_digest_value,
        membership_digest_algorithm, membership_digest_value,
        bundle_digest_algorithm, bundle_digest_value
    ),
    FOREIGN KEY (bundle_digest_algorithm, bundle_digest_value)
        REFERENCES object_blob (digest_algorithm, digest_value),
    FOREIGN KEY (
        dataset_snapshot_id,
        dataset_membership_digest_algorithm,
        dataset_membership_digest_value,
        dataset_snapshot_digest_algorithm,
        dataset_snapshot_digest_value
    ) REFERENCES dataset_snapshot (
        snapshot_id,
        feature_membership_digest_algorithm,
        feature_membership_digest_value,
        snapshot_digest_algorithm,
        snapshot_digest_value
    ),
    CHECK (snapshot_id = 'trackinput_' || left(snapshot_digest_value, 32))
);

CREATE TABLE tracking_input_entry (
    tracking_input_snapshot_id text NOT NULL
        REFERENCES tracking_input_snapshot (snapshot_id),
    entry_index integer NOT NULL CHECK (entry_index >= 0),
    dataset_snapshot_id text NOT NULL,
    feature_set_id text NOT NULL,
    analysis_run_id text NOT NULL,
    feature_bundle_digest_algorithm text NOT NULL
        CHECK (feature_bundle_digest_algorithm = 'sha256'),
    feature_bundle_digest_value text NOT NULL
        CHECK (feature_bundle_digest_value ~ '^[0-9a-f]{64}$'),
    feature_id text NOT NULL
        CHECK (feature_id ~ '^feature_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    recording_id text NOT NULL,
    recording_identity_digest_algorithm text NOT NULL
        CHECK (recording_identity_digest_algorithm = 'sha256'),
    recording_identity_digest_value text NOT NULL
        CHECK (recording_identity_digest_value ~ '^[0-9a-f]{64}$'),
    receiver_chain_id text NOT NULL
        CHECK (receiver_chain_id ~ '^rx_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    midpoint_utc_ns bigint NOT NULL CHECK (midpoint_utc_ns >= 0),
    hardware_link_id text NOT NULL,
    hardware_link_digest_algorithm text NOT NULL
        CHECK (hardware_link_digest_algorithm = 'sha256'),
    hardware_link_digest_value text NOT NULL
        CHECK (hardware_link_digest_value ~ '^[0-9a-f]{64}$'),
    ephemeris_link_id text NOT NULL,
    ephemeris_link_digest_algorithm text NOT NULL
        CHECK (ephemeris_link_digest_algorithm = 'sha256'),
    ephemeris_link_digest_value text NOT NULL
        CHECK (ephemeris_link_digest_value ~ '^[0-9a-f]{64}$'),
    calibration_artifact_id text NOT NULL
        CHECK (calibration_artifact_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    calibration_digest_algorithm text NOT NULL
        CHECK (calibration_digest_algorithm = 'sha256'),
    calibration_digest_value text NOT NULL
        CHECK (calibration_digest_value ~ '^[0-9a-f]{64}$'),
    calibration_schema_id text NOT NULL
        CHECK (calibration_schema_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    calibration_schema_version text NOT NULL
        CHECK (calibration_schema_version ~ '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'),
    prediction_policy_artifact_id text NOT NULL
        CHECK (prediction_policy_artifact_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    prediction_policy_digest_algorithm text NOT NULL
        CHECK (prediction_policy_digest_algorithm = 'sha256'),
    prediction_policy_digest_value text NOT NULL
        CHECK (prediction_policy_digest_value ~ '^[0-9a-f]{64}$'),
    prediction_policy_schema_id text NOT NULL
        CHECK (prediction_policy_schema_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    prediction_policy_schema_version text NOT NULL
        CHECK (prediction_policy_schema_version ~ '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'),
    PRIMARY KEY (tracking_input_snapshot_id, entry_index),
    UNIQUE (tracking_input_snapshot_id, feature_set_id, feature_id),
    FOREIGN KEY (
        dataset_snapshot_id, feature_set_id, analysis_run_id,
        feature_bundle_digest_algorithm, feature_bundle_digest_value
    ) REFERENCES dataset_member (
        snapshot_id, feature_set_id, analysis_run_id,
        feature_digest_algorithm, feature_digest_value
    ),
    FOREIGN KEY (
        feature_set_id, analysis_run_id,
        feature_bundle_digest_algorithm, feature_bundle_digest_value, recording_id
    ) REFERENCES feature_set (
        feature_set_id, analysis_run_id,
        bundle_digest_algorithm, bundle_digest_value, recording_id
    ),
    FOREIGN KEY (
        hardware_link_id,
        hardware_link_digest_algorithm, hardware_link_digest_value, recording_id,
        recording_identity_digest_algorithm, recording_identity_digest_value
    ) REFERENCES recording_hardware_link (
        link_id, link_digest_algorithm, link_digest_value, recording_id,
        recording_identity_digest_algorithm, recording_identity_digest_value
    ),
    FOREIGN KEY (
        ephemeris_link_id,
        ephemeris_link_digest_algorithm, ephemeris_link_digest_value, recording_id,
        recording_identity_digest_algorithm, recording_identity_digest_value
    ) REFERENCES recording_ephemeris_link (
        link_id, link_digest_algorithm, link_digest_value, recording_id,
        recording_identity_digest_algorithm, recording_identity_digest_value
    )
);

CREATE INDEX tracking_input_dataset_idx
    ON tracking_input_snapshot (dataset_snapshot_id, snapshot_id);
CREATE INDEX tracking_input_entry_recording_idx
    ON tracking_input_entry (recording_id, midpoint_utc_ns);

CREATE FUNCTION publish_tracking_input_snapshot(p_publication jsonb) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    inserted_snapshot_id text;
    entries jsonb := p_publication -> 'entries';
    expected_entry_count integer := (p_publication ->> 'entry_count')::integer;
BEGIN
    IF jsonb_typeof(p_publication) <> 'object'
       OR jsonb_typeof(entries) <> 'array'
       OR jsonb_array_length(entries) <> expected_entry_count THEN
        RAISE EXCEPTION 'invalid tracking input publication shape'
            USING ERRCODE = '22023';
    END IF;

    INSERT INTO tracking_input_snapshot (
        snapshot_id, snapshot_digest_algorithm, snapshot_digest_value,
        membership_digest_algorithm, membership_digest_value,
        dataset_snapshot_id, dataset_membership_digest_algorithm,
        dataset_membership_digest_value, dataset_snapshot_digest_algorithm,
        dataset_snapshot_digest_value, builder_artifact_id,
        builder_digest_algorithm, builder_digest_value, builder_schema_id,
        builder_schema_version, selector_artifact_id, selector_digest_algorithm,
        selector_digest_value, selector_schema_id, selector_schema_version,
        provenance_digest_algorithm, provenance_digest_value,
        bundle_digest_algorithm, bundle_digest_value, entry_count, idempotency_key)
    VALUES (
        p_publication ->> 'snapshot_id',
        p_publication ->> 'snapshot_digest_algorithm',
        p_publication ->> 'snapshot_digest_value',
        p_publication ->> 'membership_digest_algorithm',
        p_publication ->> 'membership_digest_value',
        p_publication ->> 'dataset_snapshot_id',
        p_publication ->> 'dataset_membership_digest_algorithm',
        p_publication ->> 'dataset_membership_digest_value',
        p_publication ->> 'dataset_snapshot_digest_algorithm',
        p_publication ->> 'dataset_snapshot_digest_value',
        p_publication ->> 'builder_artifact_id',
        p_publication ->> 'builder_digest_algorithm',
        p_publication ->> 'builder_digest_value',
        p_publication ->> 'builder_schema_id',
        p_publication ->> 'builder_schema_version',
        p_publication ->> 'selector_artifact_id',
        p_publication ->> 'selector_digest_algorithm',
        p_publication ->> 'selector_digest_value',
        p_publication ->> 'selector_schema_id',
        p_publication ->> 'selector_schema_version',
        p_publication ->> 'provenance_digest_algorithm',
        p_publication ->> 'provenance_digest_value',
        p_publication ->> 'bundle_digest_algorithm',
        p_publication ->> 'bundle_digest_value',
        expected_entry_count,
        p_publication ->> 'idempotency_key')
    ON CONFLICT DO NOTHING
    RETURNING snapshot_id INTO inserted_snapshot_id;

    IF inserted_snapshot_id IS NULL THEN
        RETURN false;
    END IF;

    INSERT INTO tracking_input_entry (
        tracking_input_snapshot_id, entry_index, dataset_snapshot_id,
        feature_set_id, analysis_run_id, feature_bundle_digest_algorithm,
        feature_bundle_digest_value, feature_id, recording_id,
        recording_identity_digest_algorithm, recording_identity_digest_value,
        receiver_chain_id, midpoint_utc_ns, hardware_link_id,
        hardware_link_digest_algorithm, hardware_link_digest_value,
        ephemeris_link_id, ephemeris_link_digest_algorithm,
        ephemeris_link_digest_value, calibration_artifact_id,
        calibration_digest_algorithm, calibration_digest_value,
        calibration_schema_id, calibration_schema_version,
        prediction_policy_artifact_id, prediction_policy_digest_algorithm,
        prediction_policy_digest_value, prediction_policy_schema_id,
        prediction_policy_schema_version)
    SELECT
        inserted_snapshot_id, entry_index,
        p_publication ->> 'dataset_snapshot_id', feature_set_id, analysis_run_id,
        feature_bundle_digest_algorithm, feature_bundle_digest_value, feature_id,
        recording_id, recording_identity_digest_algorithm,
        recording_identity_digest_value, receiver_chain_id, midpoint_utc_ns,
        hardware_link_id, hardware_link_digest_algorithm,
        hardware_link_digest_value, ephemeris_link_id,
        ephemeris_link_digest_algorithm, ephemeris_link_digest_value,
        calibration_artifact_id, calibration_digest_algorithm,
        calibration_digest_value, calibration_schema_id,
        calibration_schema_version, prediction_policy_artifact_id,
        prediction_policy_digest_algorithm, prediction_policy_digest_value,
        prediction_policy_schema_id, prediction_policy_schema_version
    FROM jsonb_to_recordset(entries) AS entry (
        entry_index integer, feature_set_id text, analysis_run_id text,
        feature_bundle_digest_algorithm text, feature_bundle_digest_value text,
        feature_id text, recording_id text,
        recording_identity_digest_algorithm text,
        recording_identity_digest_value text, receiver_chain_id text,
        midpoint_utc_ns bigint, hardware_link_id text,
        hardware_link_digest_algorithm text, hardware_link_digest_value text,
        ephemeris_link_id text, ephemeris_link_digest_algorithm text,
        ephemeris_link_digest_value text, calibration_artifact_id text,
        calibration_digest_algorithm text, calibration_digest_value text,
        calibration_schema_id text, calibration_schema_version text,
        prediction_policy_artifact_id text,
        prediction_policy_digest_algorithm text,
        prediction_policy_digest_value text, prediction_policy_schema_id text,
        prediction_policy_schema_version text);

    IF NOT EXISTS (
        SELECT 1 FROM tracking_input_entry
         WHERE tracking_input_snapshot_id = inserted_snapshot_id
         HAVING count(*) = expected_entry_count
            AND min(entry_index) = 0
            AND max(entry_index) = expected_entry_count - 1
    ) THEN
        RAISE EXCEPTION 'tracking input entries are not contiguous'
            USING ERRCODE = '22023';
    END IF;
    RETURN true;
END
$function$;

CREATE OR REPLACE VIEW object_blob_live_reference AS
    SELECT data_digest_algorithm AS digest_algorithm,
           data_digest_value AS digest_value,
           'recording.data'::text AS reference_kind,
           recording_id::text AS owner_id
    FROM recording
UNION ALL
    SELECT metadata_digest_algorithm, metadata_digest_value,
           'recording.metadata', recording_id::text FROM recording
UNION ALL
    SELECT raw_digest_algorithm, raw_digest_value,
           'ephemeris_snapshot.raw', snapshot_id::text FROM ephemeris_snapshot
UNION ALL
    SELECT normalized_digest_algorithm, normalized_digest_value,
           'ephemeris_snapshot.normalized', snapshot_id::text FROM ephemeris_snapshot
UNION ALL
    SELECT provenance_digest_algorithm, provenance_digest_value,
           'ephemeris_snapshot.provenance', snapshot_id::text FROM ephemeris_snapshot
UNION ALL
    SELECT bundle_digest_algorithm, bundle_digest_value,
           'dataset_snapshot.bundle', snapshot_id::text FROM dataset_snapshot
UNION ALL
    SELECT bundle_digest_algorithm, bundle_digest_value,
           'feature_set.bundle', feature_set_id::text FROM feature_set
UNION ALL
    SELECT bundle_digest_algorithm, bundle_digest_value,
           'model_snapshot.bundle', model_snapshot_id::text FROM model_snapshot
UNION ALL
    SELECT bundle_digest_algorithm, bundle_digest_value,
           'hardware_snapshot.bundle', snapshot_id::text FROM hardware_snapshot
UNION ALL
    SELECT report_digest_algorithm, report_digest_value,
           'detector_evaluation_report.report', evaluation_id::text
    FROM detector_evaluation_report
UNION ALL
    SELECT bundle_digest_algorithm, bundle_digest_value,
           'tracking_input_snapshot.bundle', snapshot_id::text
    FROM tracking_input_snapshot;

CREATE TRIGGER tracking_input_bundle_object_must_be_live
BEFORE INSERT OR UPDATE OF bundle_digest_algorithm, bundle_digest_value
ON tracking_input_snapshot
FOR EACH ROW EXECUTE FUNCTION object_blob_assert_live_reference(
    'bundle_digest_algorithm', 'bundle_digest_value');

REVOKE ALL ON tracking_input_snapshot, tracking_input_entry
FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard;
GRANT SELECT ON tracking_input_snapshot, tracking_input_entry
TO leo_analysis, leo_dashboard;
REVOKE ALL ON FUNCTION publish_tracking_input_snapshot(jsonb)
FROM PUBLIC, leo_capture, leo_dashboard;
GRANT EXECUTE ON FUNCTION publish_tracking_input_snapshot(jsonb) TO leo_analysis;

COMMIT;
