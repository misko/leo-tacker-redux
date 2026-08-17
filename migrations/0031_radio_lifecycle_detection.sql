BEGIN;

CREATE TABLE public.capture_attempt_radio_lifecycle_fact (
    attempt_id text PRIMARY KEY
        CHECK (attempt_id ~ '^cattempt_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    batch_id text NOT NULL
        CHECK (batch_id ~ '^cbatch_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    radio_id text NOT NULL
        CHECK (radio_id ~ '^radio_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    schema_id text NOT NULL
        CHECK (schema_id='org.leo-flow.capture-attempt-radio-lifecycle'),
    schema_version text NOT NULL CHECK (schema_version='0.1'),
    terminal_observed_utc_ns bigint NOT NULL CHECK (terminal_observed_utc_ns>=0),
    semantic_sha256 text NOT NULL CHECK (semantic_sha256 ~ '^[0-9a-f]{64}$'),
    semantic_fact jsonb NOT NULL CHECK (jsonb_typeof(semantic_fact)='object'),
    recorded_at_utc timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);

CREATE INDEX capture_attempt_radio_lifecycle_latest_idx
ON public.capture_attempt_radio_lifecycle_fact
   (radio_id, terminal_observed_utc_ns DESC, attempt_id DESC);

CREATE TABLE public.radio_lifecycle_interval_fact (
    radio_id text NOT NULL
        CHECK (radio_id ~ '^radio_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    previous_attempt_id text NOT NULL
        CHECK (previous_attempt_id ~ '^cattempt_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    current_attempt_id text NOT NULL
        CHECK (current_attempt_id ~ '^cattempt_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    schema_id text NOT NULL
        CHECK (schema_id='org.leo-flow.radio-lifecycle-interval'),
    schema_version text NOT NULL CHECK (schema_version='0.1'),
    semantic_sha256 text NOT NULL CHECK (semantic_sha256 ~ '^[0-9a-f]{64}$'),
    semantic_fact jsonb NOT NULL CHECK (jsonb_typeof(semantic_fact)='object'),
    recorded_at_utc timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    PRIMARY KEY (radio_id, previous_attempt_id, current_attempt_id),
    CHECK (previous_attempt_id<>current_attempt_id)
);

ALTER TABLE public.capture_attempt_radio_lifecycle_fact
OWNER TO leo_routine_owner;
ALTER TABLE public.radio_lifecycle_interval_fact
OWNER TO leo_routine_owner;

CREATE FUNCTION public.publish_capture_attempt_radio_lifecycle_fact(
    p_fact jsonb, p_semantic_sha256 text)
RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp
AS $function$
DECLARE
    target_attempt text;
    existing public.capture_attempt_radio_lifecycle_fact%ROWTYPE;
BEGIN
    IF p_fact IS NULL OR jsonb_typeof(p_fact)<>'object'
       OR p_fact#>>'{schema,schema_id}'<>
          'org.leo-flow.capture-attempt-radio-lifecycle'
       OR p_fact#>>'{schema,version,major}'<>'0'
       OR p_fact#>>'{schema,version,minor}'<>'1'
       OR p_fact->>'attempt_id' !~ '^cattempt_[A-Za-z0-9][A-Za-z0-9._:-]*$'
       OR p_fact->>'batch_id' !~ '^cbatch_[A-Za-z0-9][A-Za-z0-9._:-]*$'
       OR p_fact->>'radio_id' !~ '^radio_[A-Za-z0-9][A-Za-z0-9._:-]*$'
       OR (p_fact#>>'{terminal,observed_utc_ns}')::bigint<0
       OR p_semantic_sha256 !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'invalid capture attempt lifecycle fact' USING ERRCODE='22023';
    END IF;
    target_attempt := p_fact->>'attempt_id';
    SELECT * INTO existing FROM public.capture_attempt_radio_lifecycle_fact
     WHERE attempt_id=target_attempt FOR UPDATE;
    IF FOUND THEN
        IF existing.semantic_sha256<>p_semantic_sha256
           OR existing.semantic_fact<>p_fact THEN
            RAISE EXCEPTION 'capture attempt lifecycle fact conflict'
                USING ERRCODE='23505';
        END IF;
        RETURN target_attempt;
    END IF;
    INSERT INTO public.capture_attempt_radio_lifecycle_fact(
        attempt_id,batch_id,radio_id,schema_id,schema_version,
        terminal_observed_utc_ns,semantic_sha256,semantic_fact)
    VALUES(target_attempt,p_fact->>'batch_id',p_fact->>'radio_id',
        p_fact#>>'{schema,schema_id}','0.1',
        (p_fact#>>'{terminal,observed_utc_ns}')::bigint,
        p_semantic_sha256,p_fact);
    RETURN target_attempt;
END $function$;

CREATE FUNCTION public.publish_radio_lifecycle_interval_fact(
    p_fact jsonb, p_semantic_sha256 text)
RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp
AS $function$
DECLARE
    r public.radio_lifecycle_interval_fact%ROWTYPE;
BEGIN
    IF p_fact IS NULL OR jsonb_typeof(p_fact)<>'object'
       OR p_fact#>>'{schema,schema_id}'<>'org.leo-flow.radio-lifecycle-interval'
       OR p_fact#>>'{schema,version,major}'<>'0'
       OR p_fact#>>'{schema,version,minor}'<>'1'
       OR p_fact->>'radio_id' !~ '^radio_[A-Za-z0-9][A-Za-z0-9._:-]*$'
       OR p_fact->>'previous_attempt_id' !~ '^cattempt_[A-Za-z0-9][A-Za-z0-9._:-]*$'
       OR p_fact->>'current_attempt_id' !~ '^cattempt_[A-Za-z0-9][A-Za-z0-9._:-]*$'
       OR p_semantic_sha256 !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'invalid radio lifecycle interval fact' USING ERRCODE='22023';
    END IF;
    SELECT * INTO r FROM public.radio_lifecycle_interval_fact
     WHERE radio_id=p_fact->>'radio_id'
       AND previous_attempt_id=p_fact->>'previous_attempt_id'
       AND current_attempt_id=p_fact->>'current_attempt_id' FOR UPDATE;
    IF FOUND THEN
        IF r.semantic_sha256<>p_semantic_sha256 OR r.semantic_fact<>p_fact THEN
            RAISE EXCEPTION 'radio lifecycle interval fact conflict'
                USING ERRCODE='23505';
        END IF;
        RETURN p_fact->>'current_attempt_id';
    END IF;
    INSERT INTO public.radio_lifecycle_interval_fact(
        radio_id,previous_attempt_id,current_attempt_id,schema_id,
        schema_version,semantic_sha256,semantic_fact)
    VALUES(p_fact->>'radio_id',p_fact->>'previous_attempt_id',
        p_fact->>'current_attempt_id',p_fact#>>'{schema,schema_id}',
        '0.1',p_semantic_sha256,p_fact);
    RETURN p_fact->>'current_attempt_id';
END $function$;

CREATE FUNCTION public.read_capture_attempt_radio_lifecycle_fact(p_attempt_id text)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp
AS $function$
 SELECT semantic_fact FROM public.capture_attempt_radio_lifecycle_fact
  WHERE attempt_id=p_attempt_id
$function$;

CREATE FUNCTION public.read_latest_radio_lifecycle_terminal(p_radio_id text)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp
AS $function$
 SELECT semantic_fact FROM public.capture_attempt_radio_lifecycle_fact
  WHERE radio_id=p_radio_id
  ORDER BY terminal_observed_utc_ns DESC,attempt_id DESC LIMIT 1
$function$;

ALTER FUNCTION public.publish_capture_attempt_radio_lifecycle_fact(jsonb,text)
OWNER TO leo_routine_owner;
ALTER FUNCTION public.publish_radio_lifecycle_interval_fact(jsonb,text)
OWNER TO leo_routine_owner;
ALTER FUNCTION public.read_capture_attempt_radio_lifecycle_fact(text)
OWNER TO leo_routine_owner;
ALTER FUNCTION public.read_latest_radio_lifecycle_terminal(text)
OWNER TO leo_routine_owner;

REVOKE ALL ON public.capture_attempt_radio_lifecycle_fact,
 public.radio_lifecycle_interval_fact FROM PUBLIC,leo_capture,leo_analysis,
 leo_dashboard,leo_maintenance;
REVOKE ALL ON FUNCTION
 public.publish_capture_attempt_radio_lifecycle_fact(jsonb,text),
 public.publish_radio_lifecycle_interval_fact(jsonb,text),
 public.read_capture_attempt_radio_lifecycle_fact(text),
 public.read_latest_radio_lifecycle_terminal(text)
FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;
GRANT EXECUTE ON FUNCTION
 public.publish_capture_attempt_radio_lifecycle_fact(jsonb,text),
 public.publish_radio_lifecycle_interval_fact(jsonb,text),
 public.read_latest_radio_lifecycle_terminal(text)
TO leo_capture;
GRANT EXECUTE ON FUNCTION public.read_capture_attempt_radio_lifecycle_fact(text)
TO leo_dashboard;

COMMIT;
