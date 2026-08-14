BEGIN;

ALTER TABLE public.dataset_snapshot
    ADD CONSTRAINT dataset_snapshot_tracking_input_authority_key
    UNIQUE (
        snapshot_id,
        feature_membership_digest_algorithm,
        feature_membership_digest_value,
        snapshot_digest_algorithm,
        snapshot_digest_value
    );

ALTER TABLE public.dataset_member
    ADD CONSTRAINT dataset_member_tracking_input_authority_key
    UNIQUE (
        snapshot_id, feature_set_id, analysis_run_id,
        feature_digest_algorithm, feature_digest_value
    );

ALTER TABLE public.feature_set
    ADD CONSTRAINT feature_set_tracking_input_authority_key
    UNIQUE (
        feature_set_id, analysis_run_id,
        bundle_digest_algorithm, bundle_digest_value, recording_id
    );

ALTER TABLE public.recording_hardware_link
    ADD CONSTRAINT recording_hardware_link_tracking_input_authority_key
    UNIQUE (
        link_id, link_digest_algorithm, link_digest_value, recording_id,
        recording_identity_digest_algorithm, recording_identity_digest_value,
        hardware_snapshot_id, hardware_snapshot_digest_algorithm,
        hardware_snapshot_digest_value
    );

ALTER TABLE public.recording_ephemeris_link
    ADD CONSTRAINT recording_ephemeris_link_tracking_input_authority_key
    UNIQUE (
        link_id, link_digest_algorithm, link_digest_value, recording_id,
        recording_identity_digest_algorithm, recording_identity_digest_value
    );

CREATE TABLE public.tracking_input_snapshot (
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
    bundle_byte_count bigint NOT NULL
        CHECK (bundle_byte_count BETWEEN 1 AND 134217728),
    bundle_media_type text NOT NULL
        CHECK (bundle_media_type = 'application/json'),
    bundle_format_id text NOT NULL
        CHECK (bundle_format_id = 'tracking-input-snapshot-v0.1'),
    entry_count integer NOT NULL CHECK (entry_count > 0 AND entry_count <= 100000),
    idempotency_key text NOT NULL UNIQUE
        CHECK (idempotency_key ~ '^[^[:space:]]+$'),
    published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (snapshot_digest_algorithm, snapshot_digest_value),
    UNIQUE (bundle_digest_algorithm, bundle_digest_value),
    UNIQUE (
        snapshot_id, snapshot_digest_algorithm, snapshot_digest_value,
        membership_digest_algorithm, membership_digest_value,
        bundle_digest_algorithm, bundle_digest_value,
        bundle_byte_count, bundle_media_type, bundle_format_id
    ),
    FOREIGN KEY (bundle_digest_algorithm, bundle_digest_value)
        REFERENCES public.object_blob (digest_algorithm, digest_value),
    FOREIGN KEY (
        dataset_snapshot_id,
        dataset_membership_digest_algorithm,
        dataset_membership_digest_value,
        dataset_snapshot_digest_algorithm,
        dataset_snapshot_digest_value
    ) REFERENCES public.dataset_snapshot (
        snapshot_id,
        feature_membership_digest_algorithm,
        feature_membership_digest_value,
        snapshot_digest_algorithm,
        snapshot_digest_value
    ),
    CHECK (snapshot_id = 'trackinput_' || left(snapshot_digest_value, 32))
);

CREATE TABLE public.tracking_input_entry (
    tracking_input_snapshot_id text NOT NULL
        REFERENCES public.tracking_input_snapshot (snapshot_id),
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
    hardware_snapshot_id text NOT NULL,
    hardware_snapshot_digest_algorithm text NOT NULL
        CHECK (hardware_snapshot_digest_algorithm = 'sha256'),
    hardware_snapshot_digest_value text NOT NULL
        CHECK (hardware_snapshot_digest_value ~ '^[0-9a-f]{64}$'),
    receiver_chain_valid_from_utc_ns bigint NOT NULL
        CHECK (receiver_chain_valid_from_utc_ns >= 0),
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
    ) REFERENCES public.dataset_member (
        snapshot_id, feature_set_id, analysis_run_id,
        feature_digest_algorithm, feature_digest_value
    ),
    FOREIGN KEY (
        feature_set_id, analysis_run_id,
        feature_bundle_digest_algorithm, feature_bundle_digest_value, recording_id
    ) REFERENCES public.feature_set (
        feature_set_id, analysis_run_id,
        bundle_digest_algorithm, bundle_digest_value, recording_id
    ),
    FOREIGN KEY (
        hardware_link_id,
        hardware_link_digest_algorithm, hardware_link_digest_value, recording_id,
        recording_identity_digest_algorithm, recording_identity_digest_value,
        hardware_snapshot_id, hardware_snapshot_digest_algorithm,
        hardware_snapshot_digest_value
    ) REFERENCES public.recording_hardware_link (
        link_id, link_digest_algorithm, link_digest_value, recording_id,
        recording_identity_digest_algorithm, recording_identity_digest_value,
        hardware_snapshot_id, hardware_snapshot_digest_algorithm,
        hardware_snapshot_digest_value
    ),
    FOREIGN KEY (
        ephemeris_link_id,
        ephemeris_link_digest_algorithm, ephemeris_link_digest_value, recording_id,
        recording_identity_digest_algorithm, recording_identity_digest_value
    ) REFERENCES public.recording_ephemeris_link (
        link_id, link_digest_algorithm, link_digest_value, recording_id,
        recording_identity_digest_algorithm, recording_identity_digest_value
    ),
    FOREIGN KEY (
        hardware_snapshot_id,
        hardware_snapshot_digest_algorithm, hardware_snapshot_digest_value
    ) REFERENCES public.hardware_snapshot (
        snapshot_id, snapshot_digest_algorithm, snapshot_digest_value
    ),
    FOREIGN KEY (
        hardware_snapshot_id, receiver_chain_id,
        receiver_chain_valid_from_utc_ns
    ) REFERENCES public.hardware_receiver_chain (
        snapshot_id, receiver_chain_id, valid_from_utc_ns
    )
);

CREATE INDEX tracking_input_dataset_idx
    ON public.tracking_input_snapshot (dataset_snapshot_id, snapshot_id);
CREATE INDEX tracking_input_entry_recording_idx
    ON public.tracking_input_entry (recording_id, midpoint_utc_ns);

CREATE OR REPLACE FUNCTION public.object_blob_assert_live_reference()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    referenced_algorithm text := pg_catalog.to_jsonb(NEW) ->> TG_ARGV[0];
    referenced_digest text := pg_catalog.to_jsonb(NEW) ->> TG_ARGV[1];
BEGIN
    PERFORM 1
      FROM public.object_blob
     WHERE digest_algorithm = referenced_algorithm
       AND digest_value = referenced_digest
       AND lifecycle_state = 'live'
     FOR KEY SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'object reference does not identify a live catalog object'
            USING ERRCODE = '23503';
    END IF;
    RETURN NEW;
END
$function$;

-- Harden the shared publisher used immediately below. Migration 0014 pinned
-- pg_catalog but left its public relations unqualified, so a caller-created
-- temporary relation could deny publication by shadowing object_blob.
CREATE OR REPLACE FUNCTION public.register_live_object_blob(
    p_digest_algorithm text,
    p_digest_value text,
    p_byte_count bigint,
    p_media_type text,
    p_format_id text,
    p_locator text
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_registered_at timestamptz := pg_catalog.clock_timestamp();
    registered public.object_blob%ROWTYPE;
BEGIN
    PERFORM public.object_digest_fence(p_digest_algorithm, p_digest_value);
    IF EXISTS (
        SELECT 1 FROM public.object_orphan_observation
         WHERE digest_algorithm = p_digest_algorithm
           AND digest_value = p_digest_value
           AND state = 'claimed'
    ) THEN
        RAISE EXCEPTION 'object has an active orphan-deletion claim'
            USING ERRCODE = '40001';
    END IF;

    INSERT INTO public.object_blob
        (digest_algorithm, digest_value, byte_count, media_type, format_id, locator)
    VALUES
        (p_digest_algorithm, p_digest_value, p_byte_count,
         p_media_type, p_format_id, p_locator)
    ON CONFLICT DO NOTHING;

    UPDATE public.object_blob
       SET lifecycle_state = 'live',
           gc_deleted_at = NULL,
           gc_last_error = NULL,
           verified_at = NULL,
           registered_at = v_registered_at
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value
       AND lifecycle_state = 'gc_deleted'
       AND byte_count = p_byte_count
       AND media_type = p_media_type
       AND format_id = p_format_id
       AND locator = p_locator;

    SELECT * INTO registered
      FROM public.object_blob
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value
     FOR KEY SHARE;
    IF NOT FOUND
       OR registered.lifecycle_state <> 'live'
       OR registered.byte_count <> p_byte_count
       OR registered.media_type <> p_media_type
       OR registered.format_id <> p_format_id
       OR registered.locator <> p_locator THEN
        RETURN;
    END IF;

    UPDATE public.object_orphan_observation
       SET state = 'registered', claim_token = NULL, claimed_at = NULL,
           deleted_at = NULL, registered_at = v_registered_at
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value
       AND state <> 'registered';
    IF FOUND THEN
        INSERT INTO public.object_orphan_event
            (digest_algorithm, digest_value, event, event_at)
        VALUES (p_digest_algorithm, p_digest_value, 'registered', v_registered_at);
    END IF;
END
$function$;

CREATE FUNCTION public.publish_tracking_input_snapshot(p_publication jsonb)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    inserted_snapshot_id text;
    entries jsonb := p_publication -> 'entries';
    expected_entry_count integer := (p_publication ->> 'entry_count')::integer;
    registered public.object_blob%ROWTYPE;
BEGIN
    IF pg_catalog.octet_length(p_publication::text) > 134217728
       OR pg_catalog.jsonb_typeof(p_publication) <> 'object'
       OR pg_catalog.jsonb_typeof(entries) <> 'array'
       OR pg_catalog.jsonb_array_length(entries) <> expected_entry_count THEN
        RAISE EXCEPTION 'invalid tracking input publication shape'
            USING ERRCODE = '22023';
    END IF;

    SELECT * INTO registered
      FROM public.object_blob
     WHERE digest_algorithm = p_publication ->> 'bundle_digest_algorithm'
       AND digest_value = p_publication ->> 'bundle_digest_value'
     FOR KEY SHARE;
    IF NOT FOUND
       OR registered.lifecycle_state <> 'live'
       OR registered.byte_count <> (p_publication ->> 'bundle_byte_count')::bigint
       OR registered.media_type <> p_publication ->> 'bundle_media_type'
       OR registered.format_id <> p_publication ->> 'bundle_format_id'
       OR registered.locator <> p_publication ->> 'bundle_locator' THEN
        RAISE EXCEPTION 'tracking input object metadata is not exact and live'
            USING ERRCODE = '23514';
    END IF;

    INSERT INTO public.tracking_input_snapshot (
        snapshot_id, snapshot_digest_algorithm, snapshot_digest_value,
        membership_digest_algorithm, membership_digest_value,
        dataset_snapshot_id, dataset_membership_digest_algorithm,
        dataset_membership_digest_value, dataset_snapshot_digest_algorithm,
        dataset_snapshot_digest_value, builder_artifact_id,
        builder_digest_algorithm, builder_digest_value, builder_schema_id,
        builder_schema_version, selector_artifact_id, selector_digest_algorithm,
        selector_digest_value, selector_schema_id, selector_schema_version,
        provenance_digest_algorithm, provenance_digest_value,
        bundle_digest_algorithm, bundle_digest_value, bundle_byte_count,
        bundle_media_type, bundle_format_id, entry_count, idempotency_key)
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
        (p_publication ->> 'bundle_byte_count')::bigint,
        p_publication ->> 'bundle_media_type',
        p_publication ->> 'bundle_format_id',
        expected_entry_count,
        p_publication ->> 'idempotency_key')
    ON CONFLICT DO NOTHING
    RETURNING snapshot_id INTO inserted_snapshot_id;

    IF inserted_snapshot_id IS NULL THEN
        RETURN false;
    END IF;

    INSERT INTO public.tracking_input_entry (
        tracking_input_snapshot_id, entry_index, dataset_snapshot_id,
        feature_set_id, analysis_run_id, feature_bundle_digest_algorithm,
        feature_bundle_digest_value, feature_id, recording_id,
        recording_identity_digest_algorithm, recording_identity_digest_value,
        receiver_chain_id, midpoint_utc_ns, hardware_link_id,
        hardware_link_digest_algorithm, hardware_link_digest_value,
        hardware_snapshot_id, hardware_snapshot_digest_algorithm,
        hardware_snapshot_digest_value, receiver_chain_valid_from_utc_ns,
        ephemeris_link_id, ephemeris_link_digest_algorithm,
        ephemeris_link_digest_value, calibration_artifact_id,
        calibration_digest_algorithm, calibration_digest_value,
        calibration_schema_id, calibration_schema_version,
        prediction_policy_artifact_id, prediction_policy_digest_algorithm,
        prediction_policy_digest_value, prediction_policy_schema_id,
        prediction_policy_schema_version)
    SELECT
        inserted_snapshot_id, entry.entry_index,
        p_publication ->> 'dataset_snapshot_id', entry.feature_set_id,
        entry.analysis_run_id, entry.feature_bundle_digest_algorithm,
        entry.feature_bundle_digest_value, entry.feature_id,
        entry.recording_id, entry.recording_identity_digest_algorithm,
        entry.recording_identity_digest_value, entry.receiver_chain_id,
        entry.midpoint_utc_ns, entry.hardware_link_id,
        entry.hardware_link_digest_algorithm,
        entry.hardware_link_digest_value, hardware_link.hardware_snapshot_id,
        hardware_link.hardware_snapshot_digest_algorithm,
        hardware_link.hardware_snapshot_digest_value,
        receiver_chain.valid_from_utc_ns, entry.ephemeris_link_id,
        entry.ephemeris_link_digest_algorithm,
        entry.ephemeris_link_digest_value, entry.calibration_artifact_id,
        entry.calibration_digest_algorithm, entry.calibration_digest_value,
        entry.calibration_schema_id, entry.calibration_schema_version,
        entry.prediction_policy_artifact_id,
        entry.prediction_policy_digest_algorithm,
        entry.prediction_policy_digest_value,
        entry.prediction_policy_schema_id,
        entry.prediction_policy_schema_version
    FROM pg_catalog.jsonb_to_recordset(entries) AS entry (
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
        prediction_policy_schema_version text)
    JOIN public.recording_hardware_link AS hardware_link
      ON hardware_link.link_id = entry.hardware_link_id
     AND hardware_link.link_digest_algorithm =
         entry.hardware_link_digest_algorithm
     AND hardware_link.link_digest_value = entry.hardware_link_digest_value
     AND hardware_link.recording_id = entry.recording_id
     AND hardware_link.recording_identity_digest_algorithm =
         entry.recording_identity_digest_algorithm
     AND hardware_link.recording_identity_digest_value =
         entry.recording_identity_digest_value
    JOIN public.hardware_receiver_chain AS receiver_chain
      ON receiver_chain.snapshot_id = hardware_link.hardware_snapshot_id
     AND receiver_chain.receiver_chain_id = entry.receiver_chain_id
     AND receiver_chain.valid_from_utc_ns <= entry.midpoint_utc_ns
     AND (receiver_chain.valid_until_utc_ns IS NULL
          OR entry.midpoint_utc_ns < receiver_chain.valid_until_utc_ns);

    IF NOT EXISTS (
        SELECT 1 FROM public.tracking_input_entry
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

CREATE OR REPLACE VIEW public.object_blob_live_reference AS
    SELECT data_digest_algorithm AS digest_algorithm,
           data_digest_value AS digest_value,
           'recording.data'::text AS reference_kind,
           recording_id::text AS owner_id
    FROM public.recording
UNION ALL
    SELECT metadata_digest_algorithm, metadata_digest_value,
           'recording.metadata', recording_id::text FROM public.recording
UNION ALL
    SELECT raw_digest_algorithm, raw_digest_value,
           'ephemeris_snapshot.raw', snapshot_id::text
    FROM public.ephemeris_snapshot
UNION ALL
    SELECT normalized_digest_algorithm, normalized_digest_value,
           'ephemeris_snapshot.normalized', snapshot_id::text
    FROM public.ephemeris_snapshot
UNION ALL
    SELECT provenance_digest_algorithm, provenance_digest_value,
           'ephemeris_snapshot.provenance', snapshot_id::text
    FROM public.ephemeris_snapshot
UNION ALL
    SELECT bundle_digest_algorithm, bundle_digest_value,
           'dataset_snapshot.bundle', snapshot_id::text
    FROM public.dataset_snapshot
UNION ALL
    SELECT bundle_digest_algorithm, bundle_digest_value,
           'feature_set.bundle', feature_set_id::text FROM public.feature_set
UNION ALL
    SELECT bundle_digest_algorithm, bundle_digest_value,
           'model_snapshot.bundle', model_snapshot_id::text
    FROM public.model_snapshot
UNION ALL
    SELECT bundle_digest_algorithm, bundle_digest_value,
           'hardware_snapshot.bundle', snapshot_id::text
    FROM public.hardware_snapshot
UNION ALL
    SELECT report_digest_algorithm, report_digest_value,
           'detector_evaluation_report.report', evaluation_id::text
    FROM public.detector_evaluation_report
UNION ALL
    SELECT bundle_digest_algorithm, bundle_digest_value,
           'tracking_input_snapshot.bundle', snapshot_id::text
    FROM public.tracking_input_snapshot;

CREATE TRIGGER tracking_input_bundle_object_must_be_live
BEFORE INSERT OR UPDATE OF bundle_digest_algorithm, bundle_digest_value
ON public.tracking_input_snapshot
FOR EACH ROW EXECUTE FUNCTION public.object_blob_assert_live_reference(
    'bundle_digest_algorithm', 'bundle_digest_value');

REVOKE ALL ON public.tracking_input_snapshot, public.tracking_input_entry
FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard;
GRANT SELECT ON public.tracking_input_snapshot, public.tracking_input_entry
TO leo_analysis, leo_dashboard;
REVOKE ALL ON FUNCTION public.publish_tracking_input_snapshot(jsonb)
FROM PUBLIC, leo_capture, leo_dashboard;
GRANT EXECUTE ON FUNCTION public.publish_tracking_input_snapshot(jsonb)
TO leo_analysis;

COMMIT;
