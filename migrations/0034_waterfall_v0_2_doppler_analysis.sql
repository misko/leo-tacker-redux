BEGIN;

CREATE TABLE public.recording_waterfall_v0_2 (
    product_id text PRIMARY KEY CHECK (product_id ~ '^waterfall_[0-9a-f]{32}$'),
    analysis_run_id text NOT NULL UNIQUE CHECK (analysis_run_id ~ '^arun_[0-9a-f]{32}$'),
    source_job_id text NOT NULL UNIQUE REFERENCES public.job(job_id),
    recording_id text NOT NULL REFERENCES public.recording(recording_id),
    input_recording_digest_algorithm text NOT NULL CHECK (input_recording_digest_algorithm='sha256'),
    input_recording_digest_value text NOT NULL CHECK (input_recording_digest_value~'^[0-9a-f]{64}$'),
    request_digest_algorithm text NOT NULL CHECK (request_digest_algorithm='sha256'),
    request_digest_value text NOT NULL CHECK (request_digest_value~'^[0-9a-f]{64}$'),
    bundle_digest_algorithm text NOT NULL CHECK (bundle_digest_algorithm='sha256'),
    bundle_digest_value text NOT NULL CHECK (bundle_digest_value~'^[0-9a-f]{64}$'),
    tile_count integer NOT NULL CHECK (tile_count BETWEEN 1 AND 16),
    pixel_count integer NOT NULL CHECK (pixel_count BETWEEN 1 AND 524288),
    idempotency_key text NOT NULL UNIQUE CHECK (idempotency_key<>''),
    published_at_utc timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE(recording_id,input_recording_digest_algorithm,input_recording_digest_value,
           request_digest_algorithm,request_digest_value),
    FOREIGN KEY(bundle_digest_algorithm,bundle_digest_value)
        REFERENCES public.object_blob(digest_algorithm,digest_value)
);

CREATE TABLE public.recording_doppler_analysis (
    doppler_id text PRIMARY KEY CHECK (doppler_id~'^doppler_[0-9a-f]{32}$'),
    source_job_id text NOT NULL REFERENCES public.job(job_id),
    recording_id text NOT NULL REFERENCES public.recording(recording_id),
    waterfall_product_id text NOT NULL REFERENCES public.recording_waterfall_v0_2(product_id),
    waterfall_bundle_digest_algorithm text NOT NULL CHECK (waterfall_bundle_digest_algorithm='sha256'),
    waterfall_bundle_digest_value text NOT NULL CHECK (waterfall_bundle_digest_value~'^[0-9a-f]{64}$'),
    segment_id text NOT NULL CHECK (segment_id~'^seg_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    receiver_chain_id text NOT NULL CHECK (receiver_chain_id~'^rx_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    spectrogram_digest_algorithm text NOT NULL CHECK (spectrogram_digest_algorithm='sha256'),
    spectrogram_digest_value text NOT NULL CHECK (spectrogram_digest_value~'^[0-9a-f]{64}$'),
    basic_config_digest_algorithm text NOT NULL CHECK (basic_config_digest_algorithm='sha256'),
    basic_config_digest_value text NOT NULL CHECK (basic_config_digest_value~'^[0-9a-f]{64}$'),
    advanced_config_digest_algorithm text NOT NULL CHECK (advanced_config_digest_algorithm='sha256'),
    advanced_config_digest_value text NOT NULL CHECK (advanced_config_digest_value~'^[0-9a-f]{64}$'),
    basic_bundle_digest_algorithm text NOT NULL CHECK (basic_bundle_digest_algorithm='sha256'),
    basic_bundle_digest_value text NOT NULL CHECK (basic_bundle_digest_value~'^[0-9a-f]{64}$'),
    advanced_bundle_digest_algorithm text NOT NULL CHECK (advanced_bundle_digest_algorithm='sha256'),
    advanced_bundle_digest_value text NOT NULL CHECK (advanced_bundle_digest_value~'^[0-9a-f]{64}$'),
    candidate_count integer NOT NULL CHECK (candidate_count BETWEEN 0 AND 32),
    moving_candidate_count integer NOT NULL CHECK (moving_candidate_count BETWEEN 0 AND candidate_count),
    strongest_candidate_score double precision,
    idempotency_key text NOT NULL UNIQUE CHECK (idempotency_key<>''),
    published_at_utc timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    CHECK ((candidate_count=0 AND strongest_candidate_score IS NULL) OR
           (candidate_count>0 AND strongest_candidate_score IS NOT NULL AND
            strongest_candidate_score=strongest_candidate_score)),
    UNIQUE(waterfall_product_id,segment_id,receiver_chain_id,spectrogram_digest_algorithm,
           spectrogram_digest_value,basic_config_digest_algorithm,basic_config_digest_value,
           advanced_config_digest_algorithm,advanced_config_digest_value),
    FOREIGN KEY(waterfall_bundle_digest_algorithm,waterfall_bundle_digest_value)
        REFERENCES public.object_blob(digest_algorithm,digest_value),
    FOREIGN KEY(basic_bundle_digest_algorithm,basic_bundle_digest_value)
        REFERENCES public.object_blob(digest_algorithm,digest_value),
    FOREIGN KEY(advanced_bundle_digest_algorithm,advanced_bundle_digest_value)
        REFERENCES public.object_blob(digest_algorithm,digest_value)
);

CREATE TRIGGER recording_waterfall_v0_2_bundle_must_be_live
BEFORE INSERT OR UPDATE OF bundle_digest_algorithm,bundle_digest_value
ON public.recording_waterfall_v0_2 FOR EACH ROW
EXECUTE FUNCTION public.object_blob_assert_live_reference('bundle_digest_algorithm','bundle_digest_value');

CREATE TRIGGER recording_doppler_basic_bundle_must_be_live
BEFORE INSERT OR UPDATE OF basic_bundle_digest_algorithm,basic_bundle_digest_value
ON public.recording_doppler_analysis FOR EACH ROW
EXECUTE FUNCTION public.object_blob_assert_live_reference('basic_bundle_digest_algorithm','basic_bundle_digest_value');

CREATE TRIGGER recording_doppler_advanced_bundle_must_be_live
BEFORE INSERT OR UPDATE OF advanced_bundle_digest_algorithm,advanced_bundle_digest_value
ON public.recording_doppler_analysis FOR EACH ROW
EXECUTE FUNCTION public.object_blob_assert_live_reference('advanced_bundle_digest_algorithm','advanced_bundle_digest_value');

CREATE OR REPLACE VIEW public.object_blob_live_reference AS
    SELECT data_digest_algorithm AS digest_algorithm,data_digest_value AS digest_value,'recording.data'::text AS reference_kind,recording_id::text AS owner_id FROM public.recording
UNION ALL SELECT metadata_digest_algorithm,metadata_digest_value,'recording.metadata',recording_id::text FROM public.recording
UNION ALL SELECT raw_digest_algorithm,raw_digest_value,'ephemeris_snapshot.raw',snapshot_id::text FROM public.ephemeris_snapshot
UNION ALL SELECT normalized_digest_algorithm,normalized_digest_value,'ephemeris_snapshot.normalized',snapshot_id::text FROM public.ephemeris_snapshot
UNION ALL SELECT provenance_digest_algorithm,provenance_digest_value,'ephemeris_snapshot.provenance',snapshot_id::text FROM public.ephemeris_snapshot
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'dataset_snapshot.bundle',snapshot_id::text FROM public.dataset_snapshot
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'feature_set.bundle',feature_set_id::text FROM public.feature_set
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'model_snapshot.bundle',model_snapshot_id::text FROM public.model_snapshot
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'hardware_snapshot.bundle',snapshot_id::text FROM public.hardware_snapshot
UNION ALL SELECT report_digest_algorithm,report_digest_value,'detector_evaluation_report.report',evaluation_id::text FROM public.detector_evaluation_report
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'tracking_input_snapshot.bundle',snapshot_id::text FROM public.tracking_input_snapshot
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'tracking_model_snapshot.bundle',model_run_id::text FROM public.tracking_model_snapshot
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'recording_waterfall.bundle',product_id::text FROM public.recording_waterfall
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'recording_starlink_candidate.bundle',analysis_id::text FROM public.recording_starlink_candidate
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'recording_starlink_detector_suite.bundle',analysis_id::text FROM public.recording_starlink_detector_suite
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'recording_waterfall_v0_2.bundle',product_id::text FROM public.recording_waterfall_v0_2
UNION ALL SELECT basic_bundle_digest_algorithm,basic_bundle_digest_value,'recording_doppler_analysis.basic',doppler_id::text FROM public.recording_doppler_analysis
UNION ALL SELECT advanced_bundle_digest_algorithm,advanced_bundle_digest_value,'recording_doppler_analysis.advanced',doppler_id::text FROM public.recording_doppler_analysis;

CREATE FUNCTION public.publish_recording_waterfall_v0_2(
    text,text,text,text,text,text,text,text,text,text,text,integer,integer,text)
RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $f$
WITH candidate AS (
    SELECT $1 AS product_id,$2 AS analysis_run_id,$3 AS source_job_id,$4 AS recording_id,
           $5 AS input_algorithm,$6 AS input_digest,$7 AS request_algorithm,$8 AS request_digest,
           $9 AS bundle_algorithm,$10 AS bundle_digest,$11 AS legacy_product_id,
           $12 AS tile_count,$13 AS pixel_count,$14 AS idempotency_key
      FROM public.job j JOIN public.waterfall_projection_work legacy_work
        ON legacy_work.source_job_id=$3 AND legacy_work.product_id=$11
      JOIN public.recording_waterfall legacy ON legacy.product_id=legacy_work.product_id
     WHERE j.job_id=$3 AND j.job_type='waterfall_analysis' AND j.state='leased'
       AND legacy.recording_id=$4 AND legacy.input_recording_digest_algorithm=$5
       AND legacy.input_recording_digest_value=$6
), inserted AS (
    INSERT INTO public.recording_waterfall_v0_2(
        product_id,analysis_run_id,source_job_id,recording_id,
        input_recording_digest_algorithm,input_recording_digest_value,
        request_digest_algorithm,request_digest_value,bundle_digest_algorithm,bundle_digest_value,
        tile_count,pixel_count,idempotency_key)
    SELECT product_id,analysis_run_id,source_job_id,recording_id,input_algorithm,input_digest,
           request_algorithm,request_digest,bundle_algorithm,bundle_digest,tile_count,pixel_count,idempotency_key
      FROM candidate ON CONFLICT DO NOTHING RETURNING product_id)
SELECT EXISTS(SELECT 1 FROM inserted);
$f$;

CREATE FUNCTION public.publish_recording_doppler_analysis(
    text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,
    integer,integer,double precision,text)
RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $f$
WITH candidate AS (
    SELECT $1 AS doppler_id,$2 AS source_job_id,$3 AS recording_id,$4 AS waterfall_product_id,
           $5 AS waterfall_algorithm,$6 AS waterfall_digest,$7 AS segment_id,$8 AS receiver_chain_id,
           $9 AS spectrogram_algorithm,$10 AS spectrogram_digest,
           $11 AS basic_config_algorithm,$12 AS basic_config_digest,
           $13 AS advanced_config_algorithm,$14 AS advanced_config_digest,
           $15 AS basic_bundle_algorithm,$16 AS basic_bundle_digest,
           $17 AS advanced_bundle_algorithm,$18 AS advanced_bundle_digest,
           $19 AS candidate_count,$20 AS moving_count,$21 AS strongest_score,$22 AS idempotency_key
      FROM public.job j JOIN public.recording_waterfall_v0_2 w ON w.product_id=$4
     WHERE j.job_id=$2 AND j.job_type='waterfall_analysis' AND j.state='leased'
       AND w.source_job_id=$2 AND w.recording_id=$3
       AND w.bundle_digest_algorithm=$5 AND w.bundle_digest_value=$6
), inserted AS (
    INSERT INTO public.recording_doppler_analysis(
        doppler_id,source_job_id,recording_id,waterfall_product_id,
        waterfall_bundle_digest_algorithm,waterfall_bundle_digest_value,
        segment_id,receiver_chain_id,spectrogram_digest_algorithm,spectrogram_digest_value,
        basic_config_digest_algorithm,basic_config_digest_value,
        advanced_config_digest_algorithm,advanced_config_digest_value,
        basic_bundle_digest_algorithm,basic_bundle_digest_value,
        advanced_bundle_digest_algorithm,advanced_bundle_digest_value,
        candidate_count,moving_candidate_count,strongest_candidate_score,idempotency_key)
    SELECT doppler_id,source_job_id,recording_id,waterfall_product_id,waterfall_algorithm,
           waterfall_digest,segment_id,receiver_chain_id,spectrogram_algorithm,spectrogram_digest,
           basic_config_algorithm,basic_config_digest,advanced_config_algorithm,advanced_config_digest,
           basic_bundle_algorithm,basic_bundle_digest,advanced_bundle_algorithm,advanced_bundle_digest,
           candidate_count,moving_count,strongest_score,idempotency_key FROM candidate
    ON CONFLICT DO NOTHING RETURNING doppler_id)
SELECT EXISTS(SELECT 1 FROM inserted);
$f$;

CREATE FUNCTION public.read_recording_doppler_analysis(text)
RETURNS TABLE(
    doppler_id text,recording_id text,waterfall_product_id text,
    waterfall_digest_algorithm text,waterfall_digest_value text,
    segment_id text,receiver_chain_id text,
    spectrogram_digest_algorithm text,spectrogram_digest_value text,
    basic_config_digest_algorithm text,basic_config_digest_value text,
    advanced_config_digest_algorithm text,advanced_config_digest_value text,
    basic_bundle_digest_algorithm text,basic_bundle_digest_value text,
    basic_bundle_byte_count bigint,basic_bundle_media_type text,basic_bundle_format_id text,basic_bundle_locator text,
    advanced_bundle_digest_algorithm text,advanced_bundle_digest_value text,
    advanced_bundle_byte_count bigint,advanced_bundle_media_type text,advanced_bundle_format_id text,advanced_bundle_locator text,
    candidate_count integer,moving_candidate_count integer,strongest_candidate_score double precision)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $f$
SELECT d.doppler_id,d.recording_id,d.waterfall_product_id,
       d.waterfall_bundle_digest_algorithm,d.waterfall_bundle_digest_value,
       d.segment_id,d.receiver_chain_id,d.spectrogram_digest_algorithm,d.spectrogram_digest_value,
       d.basic_config_digest_algorithm,d.basic_config_digest_value,
       d.advanced_config_digest_algorithm,d.advanced_config_digest_value,
       d.basic_bundle_digest_algorithm,d.basic_bundle_digest_value,
       b.byte_count,b.media_type,b.format_id,b.locator,
       d.advanced_bundle_digest_algorithm,d.advanced_bundle_digest_value,
       a.byte_count,a.media_type,a.format_id,a.locator,
       d.candidate_count,d.moving_candidate_count,d.strongest_candidate_score
  FROM public.recording_doppler_analysis d
  JOIN public.object_blob b ON (b.digest_algorithm,b.digest_value)=
       (d.basic_bundle_digest_algorithm,d.basic_bundle_digest_value)
  JOIN public.object_blob a ON (a.digest_algorithm,a.digest_value)=
       (d.advanced_bundle_digest_algorithm,d.advanced_bundle_digest_value)
 WHERE d.recording_id=$1 AND b.lifecycle_state='live' AND a.lifecycle_state='live'
 ORDER BY d.segment_id,d.receiver_chain_id,d.doppler_id;
$f$;

CREATE FUNCTION public.read_recording_waterfall_v0_2(text)
RETURNS TABLE(
    product_id text,analysis_run_id text,recording_id text,
    bundle_digest_algorithm text,bundle_digest_value text,bundle_byte_count bigint,
    bundle_media_type text,bundle_format_id text,bundle_locator text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $f$
SELECT w.product_id,w.analysis_run_id,w.recording_id,w.bundle_digest_algorithm,
       w.bundle_digest_value,o.byte_count,o.media_type,o.format_id,o.locator
  FROM public.recording_waterfall_v0_2 w JOIN public.object_blob o
    ON (o.digest_algorithm,o.digest_value)=(w.bundle_digest_algorithm,w.bundle_digest_value)
 WHERE w.product_id=$1 AND o.lifecycle_state='live';
$f$;

ALTER TABLE public.recording_waterfall_v0_2 OWNER TO leo_routine_owner;
ALTER TABLE public.recording_doppler_analysis OWNER TO leo_routine_owner;

GRANT SELECT,INSERT ON public.recording_waterfall_v0_2,public.recording_doppler_analysis TO leo_routine_owner;
GRANT SELECT ON public.recording_waterfall_v0_2,public.recording_doppler_analysis TO leo_analysis;
REVOKE INSERT,UPDATE,DELETE,TRUNCATE ON public.recording_waterfall_v0_2,public.recording_doppler_analysis
FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;

ALTER FUNCTION public.publish_recording_waterfall_v0_2(text,text,text,text,text,text,text,text,text,text,text,integer,integer,text) OWNER TO leo_routine_owner;
ALTER FUNCTION public.publish_recording_doppler_analysis(text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,integer,integer,double precision,text) OWNER TO leo_routine_owner;
ALTER FUNCTION public.read_recording_doppler_analysis(text) OWNER TO leo_routine_owner;
ALTER FUNCTION public.read_recording_waterfall_v0_2(text) OWNER TO leo_routine_owner;

REVOKE ALL ON FUNCTION public.publish_recording_waterfall_v0_2(text,text,text,text,text,text,text,text,text,text,text,integer,integer,text),
    public.publish_recording_doppler_analysis(text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,integer,integer,double precision,text),
    public.read_recording_doppler_analysis(text),public.read_recording_waterfall_v0_2(text)
FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;
GRANT EXECUTE ON FUNCTION public.publish_recording_waterfall_v0_2(text,text,text,text,text,text,text,text,text,text,text,integer,integer,text),
    public.publish_recording_doppler_analysis(text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,integer,integer,double precision,text)
TO leo_analysis;
GRANT EXECUTE ON FUNCTION public.read_recording_doppler_analysis(text),public.read_recording_waterfall_v0_2(text)
TO leo_analysis,leo_dashboard;

COMMIT;
