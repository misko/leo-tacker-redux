BEGIN;

CREATE TABLE public.tracking_model_snapshot (
    model_run_id text PRIMARY KEY
        CHECK (model_run_id ~ '^mrun_[0-9a-f]{32}$'),
    model_snapshot_id text NOT NULL
        CHECK (model_snapshot_id ~ '^model_[0-9a-f]{32}$'),
    scientific_snapshot_digest_algorithm text NOT NULL
        CHECK (scientific_snapshot_digest_algorithm = 'sha256'),
    scientific_snapshot_digest_value text NOT NULL
        CHECK (scientific_snapshot_digest_value ~ '^[0-9a-f]{64}$'),
    run_digest_algorithm text NOT NULL CHECK (run_digest_algorithm = 'sha256'),
    run_digest_value text NOT NULL CHECK (run_digest_value ~ '^[0-9a-f]{64}$'),
    output_digest_algorithm text NOT NULL
        CHECK (output_digest_algorithm = 'sha256'),
    output_digest_value text NOT NULL
        CHECK (output_digest_value ~ '^[0-9a-f]{64}$'),
    evidence_digest_algorithm text NOT NULL
        CHECK (evidence_digest_algorithm = 'sha256'),
    evidence_digest_value text NOT NULL
        CHECK (evidence_digest_value ~ '^[0-9a-f]{64}$'),
    provenance_digest_algorithm text NOT NULL
        CHECK (provenance_digest_algorithm = 'sha256'),
    provenance_digest_value text NOT NULL
        CHECK (provenance_digest_value ~ '^[0-9a-f]{64}$'),
    tracking_input_snapshot_id text NOT NULL,
    tracking_input_snapshot_digest_algorithm text NOT NULL
        CHECK (tracking_input_snapshot_digest_algorithm = 'sha256'),
    tracking_input_snapshot_digest_value text NOT NULL
        CHECK (tracking_input_snapshot_digest_value ~ '^[0-9a-f]{64}$'),
    tracking_input_membership_digest_algorithm text NOT NULL
        CHECK (tracking_input_membership_digest_algorithm = 'sha256'),
    tracking_input_membership_digest_value text NOT NULL
        CHECK (tracking_input_membership_digest_value ~ '^[0-9a-f]{64}$'),
    tracking_input_bundle_digest_algorithm text NOT NULL
        CHECK (tracking_input_bundle_digest_algorithm = 'sha256'),
    tracking_input_bundle_digest_value text NOT NULL
        CHECK (tracking_input_bundle_digest_value ~ '^[0-9a-f]{64}$'),
    tracking_input_bundle_byte_count bigint NOT NULL
        CHECK (tracking_input_bundle_byte_count BETWEEN 1 AND 134217728),
    tracking_input_bundle_media_type text NOT NULL
        CHECK (tracking_input_bundle_media_type = 'application/json'),
    tracking_input_bundle_format_id text NOT NULL
        CHECK (tracking_input_bundle_format_id = 'tracking-input-snapshot-v0.1'),
    parameter_block_count integer NOT NULL
        CHECK (parameter_block_count BETWEEN 1 AND 512),
    accepted_association_count integer NOT NULL
        CHECK (accepted_association_count BETWEEN 0 AND 100000),
    rejected_association_count integer NOT NULL
        CHECK (rejected_association_count BETWEEN 0 AND 100000),
    warning_count integer NOT NULL CHECK (warning_count BETWEEN 1 AND 1024),
    bundle_digest_algorithm text NOT NULL
        CHECK (bundle_digest_algorithm = 'sha256'),
    bundle_digest_value text NOT NULL
        CHECK (bundle_digest_value ~ '^[0-9a-f]{64}$'),
    bundle_byte_count bigint NOT NULL
        CHECK (bundle_byte_count BETWEEN 1 AND 67108864),
    bundle_media_type text NOT NULL
        CHECK (bundle_media_type = 'application/json'),
    bundle_format_id text NOT NULL
        CHECK (bundle_format_id = 'tracking-model-snapshot-bundle-v0.1'),
    idempotency_key text NOT NULL UNIQUE
        CHECK (idempotency_key ~ '^[^[:space:]]+$'),
    published_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (output_digest_algorithm, output_digest_value),
    UNIQUE (run_digest_algorithm, run_digest_value),
    UNIQUE (bundle_digest_algorithm, bundle_digest_value),
    FOREIGN KEY (bundle_digest_algorithm, bundle_digest_value)
        REFERENCES public.object_blob (digest_algorithm, digest_value),
    FOREIGN KEY (
        tracking_input_snapshot_id,
        tracking_input_snapshot_digest_algorithm,
        tracking_input_snapshot_digest_value,
        tracking_input_membership_digest_algorithm,
        tracking_input_membership_digest_value,
        tracking_input_bundle_digest_algorithm,
        tracking_input_bundle_digest_value,
        tracking_input_bundle_byte_count,
        tracking_input_bundle_media_type,
        tracking_input_bundle_format_id
    ) REFERENCES public.tracking_input_snapshot (
        snapshot_id, snapshot_digest_algorithm, snapshot_digest_value,
        membership_digest_algorithm, membership_digest_value,
        bundle_digest_algorithm, bundle_digest_value, bundle_byte_count,
        bundle_media_type, bundle_format_id
    ),
    CHECK (accepted_association_count + rejected_association_count <= 100000),
    CHECK (
        model_snapshot_id =
        'model_' || pg_catalog.left(scientific_snapshot_digest_value, 32)
    ),
    CHECK (model_run_id = 'mrun_' || pg_catalog.left(run_digest_value, 32)),
    CHECK (
        (output_digest_algorithm, output_digest_value) =
        (bundle_digest_algorithm, bundle_digest_value)
    )
);

CREATE INDEX tracking_model_snapshot_content_idx
    ON public.tracking_model_snapshot (model_snapshot_id, model_run_id);
CREATE INDEX tracking_model_snapshot_input_idx
    ON public.tracking_model_snapshot
       (tracking_input_snapshot_id, model_snapshot_id, model_run_id);

CREATE FUNCTION public.publish_tracking_model_snapshot(p_publication jsonb)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    inserted_model_run_id text;
    registered public.object_blob%ROWTYPE;
BEGIN
    IF p_publication IS NULL
       OR pg_catalog.jsonb_typeof(p_publication) <> 'object'
       OR pg_catalog.octet_length(p_publication::text) > 65536 THEN
        RAISE EXCEPTION 'invalid tracking model publication shape'
            USING ERRCODE = '22023';
    END IF;

    SELECT * INTO registered
      FROM public.object_blob
     WHERE digest_algorithm = p_publication ->> 'bundle_digest_algorithm'
       AND digest_value = p_publication ->> 'bundle_digest_value'
     FOR KEY SHARE;
    IF NOT FOUND
       OR registered.lifecycle_state <> 'live'
       OR registered.byte_count < 1
       OR registered.byte_count > 67108864
       OR registered.byte_count <>
          (p_publication ->> 'bundle_byte_count')::bigint
       OR registered.media_type <> 'application/json'
       OR registered.media_type <> p_publication ->> 'bundle_media_type'
       OR registered.format_id <> 'tracking-model-snapshot-bundle-v0.1'
       OR registered.format_id <> p_publication ->> 'bundle_format_id'
       OR registered.locator <> p_publication ->> 'bundle_locator'
       OR p_publication ->> 'output_digest_algorithm' <>
          p_publication ->> 'bundle_digest_algorithm'
       OR p_publication ->> 'output_digest_value' <>
          p_publication ->> 'bundle_digest_value' THEN
        RAISE EXCEPTION 'tracking model object metadata is not exact and live'
            USING ERRCODE = '23514';
    END IF;

    PERFORM 1
      FROM public.tracking_input_snapshot AS tis
     WHERE tis.snapshot_id = p_publication ->> 'tracking_input_snapshot_id'
       AND tis.snapshot_digest_algorithm =
           p_publication ->> 'tracking_input_snapshot_digest_algorithm'
       AND tis.snapshot_digest_value =
           p_publication ->> 'tracking_input_snapshot_digest_value'
       AND tis.membership_digest_algorithm =
           p_publication ->> 'tracking_input_membership_digest_algorithm'
       AND tis.membership_digest_value =
           p_publication ->> 'tracking_input_membership_digest_value'
       AND tis.bundle_digest_algorithm =
           p_publication ->> 'tracking_input_bundle_digest_algorithm'
       AND tis.bundle_digest_value =
           p_publication ->> 'tracking_input_bundle_digest_value'
       AND tis.bundle_byte_count =
           (p_publication ->> 'tracking_input_bundle_byte_count')::bigint
       AND tis.bundle_media_type =
           p_publication ->> 'tracking_input_bundle_media_type'
       AND tis.bundle_format_id =
           p_publication ->> 'tracking_input_bundle_format_id'
     FOR KEY SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'tracking model input is not an exact authoritative snapshot'
            USING ERRCODE = '23503';
    END IF;

    INSERT INTO public.tracking_model_snapshot (
        model_snapshot_id, model_run_id,
        scientific_snapshot_digest_algorithm,
        scientific_snapshot_digest_value,
        run_digest_algorithm, run_digest_value,
        output_digest_algorithm, output_digest_value,
        evidence_digest_algorithm, evidence_digest_value,
        provenance_digest_algorithm, provenance_digest_value,
        tracking_input_snapshot_id,
        tracking_input_snapshot_digest_algorithm,
        tracking_input_snapshot_digest_value,
        tracking_input_membership_digest_algorithm,
        tracking_input_membership_digest_value,
        tracking_input_bundle_digest_algorithm,
        tracking_input_bundle_digest_value,
        tracking_input_bundle_byte_count,
        tracking_input_bundle_media_type,
        tracking_input_bundle_format_id,
        parameter_block_count, accepted_association_count,
        rejected_association_count, warning_count,
        bundle_digest_algorithm, bundle_digest_value, bundle_byte_count,
        bundle_media_type, bundle_format_id, idempotency_key)
    VALUES (
        p_publication ->> 'model_snapshot_id',
        p_publication ->> 'model_run_id',
        p_publication ->> 'scientific_snapshot_digest_algorithm',
        p_publication ->> 'scientific_snapshot_digest_value',
        p_publication ->> 'run_digest_algorithm',
        p_publication ->> 'run_digest_value',
        p_publication ->> 'output_digest_algorithm',
        p_publication ->> 'output_digest_value',
        p_publication ->> 'evidence_digest_algorithm',
        p_publication ->> 'evidence_digest_value',
        p_publication ->> 'provenance_digest_algorithm',
        p_publication ->> 'provenance_digest_value',
        p_publication ->> 'tracking_input_snapshot_id',
        p_publication ->> 'tracking_input_snapshot_digest_algorithm',
        p_publication ->> 'tracking_input_snapshot_digest_value',
        p_publication ->> 'tracking_input_membership_digest_algorithm',
        p_publication ->> 'tracking_input_membership_digest_value',
        p_publication ->> 'tracking_input_bundle_digest_algorithm',
        p_publication ->> 'tracking_input_bundle_digest_value',
        (p_publication ->> 'tracking_input_bundle_byte_count')::bigint,
        p_publication ->> 'tracking_input_bundle_media_type',
        p_publication ->> 'tracking_input_bundle_format_id',
        (p_publication ->> 'parameter_block_count')::integer,
        (p_publication ->> 'accepted_association_count')::integer,
        (p_publication ->> 'rejected_association_count')::integer,
        (p_publication ->> 'warning_count')::integer,
        p_publication ->> 'bundle_digest_algorithm',
        p_publication ->> 'bundle_digest_value',
        (p_publication ->> 'bundle_byte_count')::bigint,
        p_publication ->> 'bundle_media_type',
        p_publication ->> 'bundle_format_id',
        p_publication ->> 'idempotency_key')
    ON CONFLICT DO NOTHING
    RETURNING model_run_id INTO inserted_model_run_id;
    RETURN inserted_model_run_id IS NOT NULL;
END
$function$;

ALTER FUNCTION public.publish_tracking_model_snapshot(jsonb)
    OWNER TO leo_routine_owner;
GRANT SELECT, INSERT ON public.tracking_model_snapshot TO leo_routine_owner;
-- PostgreSQL requires UPDATE privilege for SELECT ... FOR KEY SHARE. The role
-- is NOLOGIN and can exercise it only through the fixed definer routines.
GRANT UPDATE ON public.tracking_input_snapshot TO leo_routine_owner;

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
    FROM public.tracking_input_snapshot
UNION ALL
    SELECT bundle_digest_algorithm, bundle_digest_value,
           'tracking_model_snapshot.bundle', model_run_id::text
    FROM public.tracking_model_snapshot;

CREATE TRIGGER tracking_model_bundle_object_must_be_live
BEFORE INSERT OR UPDATE OF bundle_digest_algorithm, bundle_digest_value
ON public.tracking_model_snapshot
FOR EACH ROW EXECUTE FUNCTION public.object_blob_assert_live_reference(
    'bundle_digest_algorithm', 'bundle_digest_value');

REVOKE ALL ON public.tracking_model_snapshot
FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard, leo_maintenance;
GRANT SELECT ON public.tracking_model_snapshot TO leo_analysis, leo_dashboard;
REVOKE ALL ON FUNCTION public.publish_tracking_model_snapshot(jsonb)
FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard, leo_maintenance;
GRANT EXECUTE ON FUNCTION public.publish_tracking_model_snapshot(jsonb)
TO leo_analysis;

COMMIT;
