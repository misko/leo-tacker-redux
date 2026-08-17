BEGIN;

CREATE TABLE public.recording_starlink_pilot_constellation (
    analysis_id text PRIMARY KEY CHECK (analysis_id ~ '^slqamrec_[0-9a-f]{32}$'),
    recording_id text NOT NULL REFERENCES public.recording(recording_id),
    input_recording_digest_algorithm text NOT NULL CHECK (input_recording_digest_algorithm='sha256'),
    input_recording_digest_value text NOT NULL CHECK (input_recording_digest_value ~ '^[0-9a-f]{64}$'),
    source_suite_analysis_id text NOT NULL REFERENCES public.recording_starlink_detector_suite(analysis_id),
    source_suite_bundle_digest_algorithm text NOT NULL CHECK (source_suite_bundle_digest_algorithm='sha256'),
    source_suite_bundle_digest_value text NOT NULL CHECK (source_suite_bundle_digest_value ~ '^[0-9a-f]{64}$'),
    source_suite_schema_id text NOT NULL CHECK (source_suite_schema_id='org.leo-flow.starlink-detector-suite-recording-bundle'),
    source_suite_schema_major integer NOT NULL CHECK (source_suite_schema_major=0),
    source_suite_schema_minor integer NOT NULL CHECK (source_suite_schema_minor=2),
    source_suite_request_digest_algorithm text NOT NULL CHECK (source_suite_request_digest_algorithm='sha256'),
    source_suite_request_digest_value text NOT NULL CHECK (source_suite_request_digest_value ~ '^[0-9a-f]{64}$'),
    request_digest_algorithm text NOT NULL CHECK (request_digest_algorithm='sha256'),
    request_digest_value text NOT NULL CHECK (request_digest_value ~ '^[0-9a-f]{64}$'),
    bundle_digest_algorithm text NOT NULL CHECK (bundle_digest_algorithm='sha256'),
    bundle_digest_value text NOT NULL CHECK (bundle_digest_value ~ '^[0-9a-f]{64}$'),
    stream_count integer NOT NULL CHECK (stream_count BETWEEN 1 AND 64),
    point_count integer NOT NULL CHECK (point_count=stream_count*2400),
    idempotency_key text NOT NULL UNIQUE CHECK (idempotency_key<>''),
    published_at_utc timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE(recording_id,input_recording_digest_algorithm,input_recording_digest_value,source_suite_analysis_id,source_suite_bundle_digest_algorithm,source_suite_bundle_digest_value,source_suite_request_digest_algorithm,source_suite_request_digest_value,request_digest_algorithm,request_digest_value),
    FOREIGN KEY(bundle_digest_algorithm,bundle_digest_value) REFERENCES public.object_blob(digest_algorithm,digest_value)
);

CREATE INDEX recording_starlink_pilot_constellation_latest_idx ON public.recording_starlink_pilot_constellation(recording_id,published_at_utc DESC,analysis_id DESC);
CREATE TRIGGER recording_starlink_pilot_constellation_bundle_must_be_live BEFORE INSERT OR UPDATE OF bundle_digest_algorithm,bundle_digest_value ON public.recording_starlink_pilot_constellation FOR EACH ROW EXECUTE FUNCTION public.object_blob_assert_live_reference('bundle_digest_algorithm','bundle_digest_value');

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
UNION ALL SELECT advanced_bundle_digest_algorithm,advanced_bundle_digest_value,'recording_doppler_analysis.advanced',doppler_id::text FROM public.recording_doppler_analysis
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'recording_starlink_surrogate_null.bundle',analysis_id::text FROM public.recording_starlink_surrogate_null
UNION ALL SELECT bundle_digest_algorithm,bundle_digest_value,'recording_starlink_pilot_constellation.bundle',analysis_id::text FROM public.recording_starlink_pilot_constellation;

CREATE FUNCTION public.publish_recording_starlink_pilot_constellation(text,text,text,text,text,text,text,text,integer,integer,text,text,text,text,text,text,integer,integer,text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
DECLARE inserted text;
BEGIN
  IF $8<>'org.leo-flow.starlink-detector-suite-recording-bundle' OR $9<>0 OR $10<>2 THEN RAISE EXCEPTION 'source detector-suite schema is not v0.2' USING ERRCODE='22023'; END IF;
  PERFORM 1 FROM public.recording_starlink_detector_suite s WHERE s.analysis_id=$5 AND s.recording_id=$2 AND s.input_recording_digest_algorithm=$3 AND s.input_recording_digest_value=$4 AND s.bundle_digest_algorithm=$6 AND s.bundle_digest_value=$7 AND s.request_digest_algorithm=$11 AND s.request_digest_value=$12;
  IF NOT FOUND THEN RAISE EXCEPTION 'source detector-suite identity is not authoritative' USING ERRCODE='23503'; END IF;
  INSERT INTO public.recording_starlink_pilot_constellation VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,DEFAULT) ON CONFLICT DO NOTHING RETURNING analysis_id INTO inserted;
  IF inserted IS NOT NULL THEN RETURN true; END IF;
  IF EXISTS(SELECT 1 FROM public.recording_starlink_pilot_constellation e WHERE e.analysis_id=$1 AND e.recording_id=$2 AND e.input_recording_digest_algorithm=$3 AND e.input_recording_digest_value=$4 AND e.source_suite_analysis_id=$5 AND e.source_suite_bundle_digest_algorithm=$6 AND e.source_suite_bundle_digest_value=$7 AND e.source_suite_schema_id=$8 AND e.source_suite_schema_major=$9 AND e.source_suite_schema_minor=$10 AND e.source_suite_request_digest_algorithm=$11 AND e.source_suite_request_digest_value=$12 AND e.request_digest_algorithm=$13 AND e.request_digest_value=$14 AND e.bundle_digest_algorithm=$15 AND e.bundle_digest_value=$16 AND e.stream_count=$17 AND e.point_count=$18 AND e.idempotency_key=$19) THEN RETURN true; END IF;
  RAISE EXCEPTION 'pilot constellation catalog identity conflict' USING ERRCODE='23505';
END $function$;

CREATE FUNCTION public.read_recording_starlink_pilot_constellation(text,text,text,text) RETURNS TABLE(analysis_id text,recording_id text,input_recording_digest_algorithm text,input_recording_digest_value text,source_suite_analysis_id text,source_suite_bundle_digest_algorithm text,source_suite_bundle_digest_value text,source_suite_schema_id text,source_suite_schema_major integer,source_suite_schema_minor integer,source_suite_request_digest_algorithm text,source_suite_request_digest_value text,request_digest_algorithm text,request_digest_value text,stream_count integer,point_count integer,bundle_digest_algorithm text,bundle_digest_value text,bundle_byte_count bigint,bundle_media_type text,bundle_format_id text,bundle_locator text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
SELECT e.analysis_id,e.recording_id,e.input_recording_digest_algorithm,e.input_recording_digest_value,e.source_suite_analysis_id,e.source_suite_bundle_digest_algorithm,e.source_suite_bundle_digest_value,e.source_suite_schema_id,e.source_suite_schema_major,e.source_suite_schema_minor,e.source_suite_request_digest_algorithm,e.source_suite_request_digest_value,e.request_digest_algorithm,e.request_digest_value,e.stream_count,e.point_count,e.bundle_digest_algorithm,e.bundle_digest_value,o.byte_count,o.media_type,o.format_id,o.locator FROM public.recording_starlink_pilot_constellation e JOIN public.object_blob o ON (o.digest_algorithm,o.digest_value)=(e.bundle_digest_algorithm,e.bundle_digest_value) WHERE e.analysis_id=$1 AND e.recording_id=$2 AND e.bundle_digest_algorithm=$3 AND e.bundle_digest_value=$4 AND o.lifecycle_state='live'; $function$;

CREATE FUNCTION public.read_latest_recording_starlink_pilot_constellation(text) RETURNS TABLE(analysis_id text,recording_id text,stream_count integer,point_count integer,bundle_digest_algorithm text,bundle_digest_value text,published_at_utc timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
SELECT e.analysis_id,e.recording_id,e.stream_count,e.point_count,e.bundle_digest_algorithm,e.bundle_digest_value,e.published_at_utc FROM public.recording_starlink_pilot_constellation e JOIN public.object_blob o ON (o.digest_algorithm,o.digest_value)=(e.bundle_digest_algorithm,e.bundle_digest_value) WHERE e.recording_id=$1 AND o.lifecycle_state='live' ORDER BY e.published_at_utc DESC,e.analysis_id DESC LIMIT 1; $function$;

ALTER TABLE public.recording_starlink_pilot_constellation OWNER TO leo_routine_owner;
GRANT SELECT,INSERT ON public.recording_starlink_pilot_constellation TO leo_routine_owner;
REVOKE ALL ON public.recording_starlink_pilot_constellation FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;
ALTER FUNCTION public.publish_recording_starlink_pilot_constellation(text,text,text,text,text,text,text,text,integer,integer,text,text,text,text,text,text,integer,integer,text) OWNER TO leo_routine_owner;
ALTER FUNCTION public.read_recording_starlink_pilot_constellation(text,text,text,text) OWNER TO leo_routine_owner;
ALTER FUNCTION public.read_latest_recording_starlink_pilot_constellation(text) OWNER TO leo_routine_owner;
REVOKE ALL ON FUNCTION public.publish_recording_starlink_pilot_constellation(text,text,text,text,text,text,text,text,integer,integer,text,text,text,text,text,text,integer,integer,text),public.read_recording_starlink_pilot_constellation(text,text,text,text),public.read_latest_recording_starlink_pilot_constellation(text) FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;
GRANT EXECUTE ON FUNCTION public.publish_recording_starlink_pilot_constellation(text,text,text,text,text,text,text,text,integer,integer,text,text,text,text,text,text,integer,integer,text),public.read_recording_starlink_pilot_constellation(text,text,text,text),public.read_latest_recording_starlink_pilot_constellation(text) TO leo_analysis;
GRANT EXECUTE ON FUNCTION
    public.read_latest_recording_starlink_pilot_constellation(text),
    public.read_recording_starlink_pilot_constellation(text,text,text,text)
TO leo_dashboard;

COMMIT;
