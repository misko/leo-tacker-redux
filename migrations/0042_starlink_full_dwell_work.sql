BEGIN;

CREATE TABLE public.starlink_full_dwell_work_v0_1 (
  source_suite_analysis_id text PRIMARY KEY REFERENCES public.recording_starlink_detector_suite(analysis_id),
  recording_id text NOT NULL REFERENCES public.recording(recording_id),
  source_suite_request_digest_value text NOT NULL CHECK(source_suite_request_digest_value~'^[0-9a-f]{64}$'),
  state text NOT NULL DEFAULT 'ready' CHECK(state IN ('ready','leased','failed','succeeded','parked')),
  available_at_utc timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
  attempt integer NOT NULL DEFAULT 0 CHECK(attempt>=0),
  lease_token text,
  lease_generation bigint NOT NULL DEFAULT 0 CHECK(lease_generation>=0),
  lease_expires_utc timestamptz,
  last_error text,
  park_reason text,
  parked_at_utc timestamptz,
  result_analysis_id text REFERENCES public.recording_starlink_full_dwell_v0_1(analysis_id),
  completed_at_utc timestamptz,
  CHECK((state='leased' AND lease_token IS NOT NULL AND lease_expires_utc IS NOT NULL) OR (state<>'leased' AND lease_token IS NULL AND lease_expires_utc IS NULL)),
  CHECK((state='parked' AND park_reason~'^[a-z0-9][a-z0-9._:-]{0,127}$' AND parked_at_utc IS NOT NULL) OR (state<>'parked' AND park_reason IS NULL AND parked_at_utc IS NULL)),
  CHECK((state='succeeded' AND result_analysis_id IS NOT NULL AND completed_at_utc IS NOT NULL) OR (state<>'succeeded' AND result_analysis_id IS NULL AND completed_at_utc IS NULL))
);
CREATE INDEX starlink_full_dwell_work_claim_v0_1_idx ON public.starlink_full_dwell_work_v0_1(available_at_utc,source_suite_analysis_id) WHERE state IN ('ready','failed','leased');

CREATE FUNCTION public.admit_starlink_full_dwell_work_v0_1(integer,integer)
RETURNS TABLE(admitted integer,active_backlog integer,saturated boolean)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
DECLARE capacity integer; inserted integer;
BEGIN
 IF $1 NOT BETWEEN 1 AND 64 OR $2 NOT BETWEEN 1 AND 256 OR $1>$2 THEN RAISE EXCEPTION 'invalid full-dwell admission bounds' USING ERRCODE='22023'; END IF;
 PERFORM pg_catalog.pg_advisory_xact_lock(1186462801);
 SELECT count(*)::integer INTO active_backlog FROM public.starlink_full_dwell_work_v0_1 WHERE state IN ('ready','leased','failed');
 capacity:=greatest(0,least($1,$2-active_backlog));
 WITH candidates AS (
   SELECT s.analysis_id,s.recording_id,s.request_digest_value
   FROM public.recording_starlink_detector_suite s
   JOIN public.object_blob o ON (o.digest_algorithm,o.digest_value)=(s.bundle_digest_algorithm,s.bundle_digest_value)
   WHERE s.result_state='candidates' AND o.lifecycle_state='live'
     AND NOT EXISTS(SELECT 1 FROM public.recording_starlink_full_dwell_v0_1 f WHERE f.source_suite_analysis_id=s.analysis_id)
     AND NOT EXISTS(SELECT 1 FROM public.starlink_full_dwell_work_v0_1 w WHERE w.source_suite_analysis_id=s.analysis_id)
   ORDER BY s.published_at_utc DESC,s.analysis_id DESC LIMIT capacity
 ), added AS (
   INSERT INTO public.starlink_full_dwell_work_v0_1(source_suite_analysis_id,recording_id,source_suite_request_digest_value)
   SELECT analysis_id,recording_id,request_digest_value FROM candidates ON CONFLICT DO NOTHING RETURNING 1
 ) SELECT count(*)::integer INTO inserted FROM added;
 admitted:=inserted; active_backlog:=active_backlog+inserted;
 saturated:=active_backlog >= $2 AND EXISTS(
   SELECT 1 FROM public.recording_starlink_detector_suite s
   JOIN public.object_blob o ON (o.digest_algorithm,o.digest_value)=(s.bundle_digest_algorithm,s.bundle_digest_value)
   WHERE s.result_state='candidates' AND o.lifecycle_state='live'
     AND NOT EXISTS(SELECT 1 FROM public.recording_starlink_full_dwell_v0_1 f WHERE f.source_suite_analysis_id=s.analysis_id)
     AND NOT EXISTS(SELECT 1 FROM public.starlink_full_dwell_work_v0_1 w WHERE w.source_suite_analysis_id=s.analysis_id));
 RETURN NEXT;
END $function$;

CREATE FUNCTION public.claim_starlink_full_dwell_work_v0_1(text,interval)
RETURNS TABLE(source_suite_analysis_id text,recording_id text,source_suite_request_digest_value text,lease_token text,lease_generation bigint,attempt integer,bundle_digest_algorithm text,bundle_digest_value text,bundle_byte_count bigint,bundle_media_type text,bundle_format_id text,bundle_locator text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
BEGIN
 IF $1='' OR $2<=interval '0' OR $2>interval '8 hours' THEN RAISE EXCEPTION 'invalid full-dwell claim bounds' USING ERRCODE='22023'; END IF;
 RETURN QUERY WITH candidate AS (
   SELECT w.source_suite_analysis_id FROM public.starlink_full_dwell_work_v0_1 w
   WHERE w.available_at_utc<=clock_timestamp() AND (w.state IN ('ready','failed') OR (w.state='leased' AND w.lease_expires_utc<=clock_timestamp()))
   ORDER BY w.available_at_utc,w.source_suite_analysis_id FOR UPDATE SKIP LOCKED LIMIT 1
 ), claimed AS (
   UPDATE public.starlink_full_dwell_work_v0_1 w SET state='leased',attempt=w.attempt+1,lease_generation=w.lease_generation+1,lease_token=$1,lease_expires_utc=clock_timestamp()+$2,last_error=NULL
   FROM candidate c WHERE w.source_suite_analysis_id=c.source_suite_analysis_id RETURNING w.*
 ) SELECT c.source_suite_analysis_id,c.recording_id,c.source_suite_request_digest_value,c.lease_token,c.lease_generation,c.attempt,s.bundle_digest_algorithm,s.bundle_digest_value,o.byte_count,o.media_type,o.format_id,o.locator
 FROM claimed c JOIN public.recording_starlink_detector_suite s ON s.analysis_id=c.source_suite_analysis_id JOIN public.object_blob o ON (o.digest_algorithm,o.digest_value)=(s.bundle_digest_algorithm,s.bundle_digest_value) WHERE o.lifecycle_state='live';
END $function$;

CREATE FUNCTION public.complete_starlink_full_dwell_work_v0_1(text,text,bigint,text) RETURNS boolean
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
WITH changed AS (
 UPDATE public.starlink_full_dwell_work_v0_1 w SET state='succeeded',result_analysis_id=$4,completed_at_utc=clock_timestamp(),lease_token=NULL,lease_expires_utc=NULL,last_error=NULL
 WHERE w.source_suite_analysis_id=$1 AND w.state='leased' AND w.lease_token=$2 AND w.lease_generation=$3 AND w.lease_expires_utc>clock_timestamp()
   AND EXISTS(SELECT 1 FROM public.recording_starlink_full_dwell_v0_1 f WHERE f.analysis_id=$4 AND f.recording_id=w.recording_id AND f.source_suite_analysis_id=w.source_suite_analysis_id)
 RETURNING 1) SELECT count(*)=1 FROM changed;$function$;

CREATE FUNCTION public.retry_starlink_full_dwell_work_v0_1(text,text,bigint,text,timestamptz) RETURNS boolean
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
WITH changed AS (UPDATE public.starlink_full_dwell_work_v0_1 SET state='failed',last_error=$4,available_at_utc=$5,lease_token=NULL,lease_expires_utc=NULL WHERE source_suite_analysis_id=$1 AND state='leased' AND lease_token=$2 AND lease_generation=$3 AND lease_expires_utc>clock_timestamp() AND $4~'^[a-z0-9][a-z0-9._:-]{0,127}$' AND $5>clock_timestamp() RETURNING 1) SELECT count(*)=1 FROM changed;$function$;

CREATE FUNCTION public.park_starlink_full_dwell_work_v0_1(text,text,bigint,text) RETURNS boolean
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $function$
WITH changed AS (UPDATE public.starlink_full_dwell_work_v0_1 SET state='parked',park_reason=$4,parked_at_utc=clock_timestamp(),lease_token=NULL,lease_expires_utc=NULL,last_error=NULL WHERE source_suite_analysis_id=$1 AND state='leased' AND lease_token=$2 AND lease_generation=$3 AND lease_expires_utc>clock_timestamp() AND $4~'^[a-z0-9][a-z0-9._:-]{0,127}$' RETURNING 1) SELECT count(*)=1 FROM changed;$function$;

ALTER TABLE public.starlink_full_dwell_work_v0_1 OWNER TO leo_routine_owner;
GRANT SELECT,INSERT,UPDATE ON public.starlink_full_dwell_work_v0_1 TO leo_routine_owner;
REVOKE ALL ON public.starlink_full_dwell_work_v0_1 FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;
ALTER FUNCTION public.admit_starlink_full_dwell_work_v0_1(integer,integer) OWNER TO leo_routine_owner;
ALTER FUNCTION public.claim_starlink_full_dwell_work_v0_1(text,interval) OWNER TO leo_routine_owner;
ALTER FUNCTION public.complete_starlink_full_dwell_work_v0_1(text,text,bigint,text) OWNER TO leo_routine_owner;
ALTER FUNCTION public.retry_starlink_full_dwell_work_v0_1(text,text,bigint,text,timestamptz) OWNER TO leo_routine_owner;
ALTER FUNCTION public.park_starlink_full_dwell_work_v0_1(text,text,bigint,text) OWNER TO leo_routine_owner;
REVOKE ALL ON FUNCTION public.admit_starlink_full_dwell_work_v0_1(integer,integer),public.claim_starlink_full_dwell_work_v0_1(text,interval),public.complete_starlink_full_dwell_work_v0_1(text,text,bigint,text),public.retry_starlink_full_dwell_work_v0_1(text,text,bigint,text,timestamptz),public.park_starlink_full_dwell_work_v0_1(text,text,bigint,text) FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;
GRANT EXECUTE ON FUNCTION public.admit_starlink_full_dwell_work_v0_1(integer,integer),public.claim_starlink_full_dwell_work_v0_1(text,interval),public.complete_starlink_full_dwell_work_v0_1(text,text,bigint,text),public.retry_starlink_full_dwell_work_v0_1(text,text,bigint,text,timestamptz),public.park_starlink_full_dwell_work_v0_1(text,text,bigint,text) TO leo_analysis;

COMMIT;
