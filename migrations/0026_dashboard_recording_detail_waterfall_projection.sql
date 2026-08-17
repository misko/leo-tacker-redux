BEGIN;

CREATE TABLE public.dashboard_recording_detail_projection (
    projection_sequence bigint PRIMARY KEY
        DEFAULT nextval('public.dashboard_projection_sequence'),
    recording_id text NOT NULL UNIQUE
        CHECK (recording_id ~ '^rec_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    semantic_view jsonb NOT NULL CHECK (jsonb_typeof(semantic_view) = 'object'),
    projected_at_utc timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);

CREATE TABLE public.dashboard_recording_waterfall_projection (
    projection_sequence bigint PRIMARY KEY
        DEFAULT nextval('public.dashboard_projection_sequence'),
    recording_id text NOT NULL
        CHECK (recording_id ~ '^rec_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    recording_identity_digest_value text NOT NULL
        CHECK (recording_identity_digest_value ~ '^[0-9a-f]{64}$'),
    analysis_run_id text
        CHECK (analysis_run_id ~ '^arun_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    state text NOT NULL
        CHECK (state IN ('unavailable', 'pending', 'complete', 'failed')),
    reason_code text
        CHECK (reason_code ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    tile_count integer NOT NULL CHECK (tile_count BETWEEN 0 AND 64),
    cell_count integer NOT NULL CHECK (cell_count BETWEEN 0 AND 262144),
    semantic_view jsonb NOT NULL CHECK (jsonb_typeof(semantic_view) = 'object'),
    projected_at_utc timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (recording_id, projection_sequence),
    CHECK (
        (state = 'unavailable' AND analysis_run_id IS NULL
         AND reason_code IS NULL AND tile_count = 0 AND cell_count = 0)
        OR
        (state = 'pending' AND analysis_run_id IS NOT NULL
         AND reason_code IS NULL AND tile_count = 0 AND cell_count = 0)
        OR
        (state = 'complete' AND analysis_run_id IS NOT NULL
         AND reason_code IS NULL AND tile_count > 0 AND cell_count > 0)
        OR
        (state = 'failed' AND analysis_run_id IS NOT NULL
         AND reason_code IS NOT NULL AND tile_count = 0 AND cell_count = 0)
    )
);

CREATE INDEX dashboard_recording_waterfall_identity_idx
    ON public.dashboard_recording_waterfall_projection
       (recording_id, projection_sequence DESC);

CREATE FUNCTION public.publish_dashboard_recording_detail(p_view jsonb)
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    target_recording_id text;
    target_radio_id text;
    segment jsonb;
    receiver_chain jsonb;
    segment_count integer;
    previous_segment_start bigint;
    existing_sequence bigint;
    existing_view jsonb;
    recording_row record;
    inserted_sequence bigint;
BEGIN
    IF p_view IS NULL OR pg_catalog.jsonb_typeof(p_view) <> 'object'
       OR pg_catalog.octet_length(p_view::text) > 1048576
       OR (SELECT pg_catalog.count(*)
             FROM pg_catalog.jsonb_object_keys(p_view)) <> 17
       OR NOT p_view ?& ARRAY[
            'schema', 'recording_id', 'plan_id', 'station_id', 'radio_id',
            'radio_serial', 'hardware_snapshot_id', 'producer', 'clock_status',
            'capture_started_utc_ns', 'capture_finished_utc_ns',
            'analysis_state', 'recording_object_available', 'manifest_digest',
            'sample_dtype', 'sample_layout', 'segments'] THEN
        RAISE EXCEPTION 'invalid dashboard recording detail projection'
            USING ERRCODE = '22023';
    END IF;
    IF pg_catalog.jsonb_typeof(p_view->'schema') <> 'object'
       OR (SELECT pg_catalog.count(*)
             FROM pg_catalog.jsonb_object_keys(p_view->'schema')) <> 2
       OR p_view #>> '{schema,schema_id}' <>
          'org.leo-flow.dashboard.recording-capture-detail'
       OR pg_catalog.jsonb_typeof(p_view #> '{schema,version}') <> 'object'
       OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(
               p_view #> '{schema,version}')) <> 2
       OR p_view #>> '{schema,version,major}' <> '0'
       OR p_view #>> '{schema,version,minor}' <> '1' THEN
        RAISE EXCEPTION 'unsupported dashboard recording detail schema'
            USING ERRCODE = '22023';
    END IF;

    target_recording_id := p_view->>'recording_id';
    target_radio_id := p_view->>'radio_id';
    IF target_recording_id !~ '^rec_[A-Za-z0-9][A-Za-z0-9._:-]*$'
       OR p_view->>'plan_id' !~ '^plan_[A-Za-z0-9][A-Za-z0-9._:-]*$'
       OR p_view->>'station_id' !~ '^station_[A-Za-z0-9][A-Za-z0-9._:-]*$'
       OR target_radio_id !~ '^radio_[A-Za-z0-9][A-Za-z0-9._:-]*$'
       OR p_view->>'hardware_snapshot_id' !~
          '^hw_[A-Za-z0-9][A-Za-z0-9._:-]*$'
       OR p_view->>'radio_serial' !~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
       OR p_view->>'producer' !~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
       OR p_view->>'clock_status' !~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
       OR p_view->>'analysis_state' !~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
       OR (p_view->>'capture_started_utc_ns')::bigint < 0
       OR (p_view->>'capture_finished_utc_ns')::bigint <=
          (p_view->>'capture_started_utc_ns')::bigint
       OR pg_catalog.jsonb_typeof(p_view->'recording_object_available') <>
          'boolean'
       OR p_view #>> '{manifest_digest,algorithm}' <> 'sha256'
       OR p_view #>> '{manifest_digest,value}' !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_view->'manifest_digest') <> 'object'
       OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(
               p_view->'manifest_digest')) <> 2
       OR p_view->>'sample_dtype' <> '<i2'
       OR p_view->'sample_layout' <>
          '["sample", "receiver", "component"]'::jsonb
       OR pg_catalog.jsonb_typeof(p_view->'segments') <> 'array'
       OR pg_catalog.jsonb_array_length(p_view->'segments') NOT BETWEEN 1 AND 4096
    THEN
        RAISE EXCEPTION 'invalid dashboard recording detail values'
            USING ERRCODE = '22023';
    END IF;

    segment_count := pg_catalog.jsonb_array_length(p_view->'segments');
    IF (SELECT pg_catalog.count(DISTINCT item.value->>'segment_id')
          FROM pg_catalog.jsonb_array_elements(
               p_view->'segments') AS item(value)) <> segment_count THEN
        RAISE EXCEPTION 'duplicate dashboard recording segment identity'
            USING ERRCODE = '22023';
    END IF;
    previous_segment_start := NULL;
    FOR segment IN
        SELECT item.value
          FROM pg_catalog.jsonb_array_elements(p_view->'segments') AS item(value)
    LOOP
        IF pg_catalog.jsonb_typeof(segment) <> 'object'
           OR (SELECT pg_catalog.count(*)
                 FROM pg_catalog.jsonb_object_keys(segment)) <> 12
           OR NOT segment ?& ARRAY[
                'segment_id', 'activity_id', 'activity_kind',
                'receiver_chain_ids', 'started_utc_ns', 'finished_utc_ns',
                'center_frequency_hz', 'sample_rate_hz', 'bandwidth_hz',
                'gain_mode', 'gain_db', 'sample_count']
           OR segment->>'segment_id' !~
              '^seg_[A-Za-z0-9][A-Za-z0-9._:-]*$'
           OR segment->>'activity_id' !~
              '^act_[A-Za-z0-9][A-Za-z0-9._:-]*$'
           OR segment->>'activity_kind' NOT IN
              ('scan', 'dwell', 'calibration', 'test')
           OR pg_catalog.jsonb_typeof(segment->'receiver_chain_ids') <> 'array'
           OR pg_catalog.jsonb_array_length(segment->'receiver_chain_ids') = 0
           OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_array_elements(
                   segment->'receiver_chain_ids')) <>
              (SELECT pg_catalog.count(DISTINCT item.value)
                 FROM pg_catalog.jsonb_array_elements(
                      segment->'receiver_chain_ids') AS item(value))
           OR (segment->>'started_utc_ns')::bigint <
              (p_view->>'capture_started_utc_ns')::bigint
           OR (previous_segment_start IS NOT NULL AND
               (segment->>'started_utc_ns')::bigint < previous_segment_start)
           OR (segment->>'finished_utc_ns')::bigint <=
              (segment->>'started_utc_ns')::bigint
           OR (segment->>'finished_utc_ns')::bigint >
              (p_view->>'capture_finished_utc_ns')::bigint
           OR (segment->>'center_frequency_hz')::double precision <= 0
           OR (segment->>'sample_rate_hz')::double precision <= 0
           OR (segment->>'bandwidth_hz')::double precision <= 0
           OR (segment->>'bandwidth_hz')::double precision >
              (segment->>'sample_rate_hz')::double precision
           OR (segment->>'center_frequency_hz')::double precision IN
              ('NaN'::double precision, 'Infinity'::double precision,
               '-Infinity'::double precision)
           OR (segment->>'sample_rate_hz')::double precision IN
              ('NaN'::double precision, 'Infinity'::double precision,
               '-Infinity'::double precision)
           OR (segment->>'bandwidth_hz')::double precision IN
              ('NaN'::double precision, 'Infinity'::double precision,
               '-Infinity'::double precision)
           OR segment->>'gain_mode' NOT IN ('manual', 'agc')
           OR (segment->>'sample_count')::bigint <= 0
           OR (segment->>'gain_mode' = 'manual' AND segment->'gain_db' = 'null'::jsonb)
           OR (segment->>'gain_mode' = 'agc' AND segment->'gain_db' <> 'null'::jsonb)
           OR (segment->>'gain_mode' = 'manual' AND
               (segment->>'gain_db')::double precision IN
               ('NaN'::double precision, 'Infinity'::double precision,
                '-Infinity'::double precision))
        THEN
            RAISE EXCEPTION 'invalid dashboard recording segment projection'
                USING ERRCODE = '22023';
        END IF;
        previous_segment_start := (segment->>'started_utc_ns')::bigint;
        FOR receiver_chain IN
            SELECT item.value FROM pg_catalog.jsonb_array_elements(
                segment->'receiver_chain_ids') AS item(value)
        LOOP
            IF pg_catalog.jsonb_typeof(receiver_chain) <> 'string'
               OR receiver_chain #>> '{}' !~
                  '^rx_[A-Za-z0-9][A-Za-z0-9._:-]*$' THEN
                RAISE EXCEPTION 'invalid dashboard recording receiver chain'
                    USING ERRCODE = '22023';
            END IF;
        END LOOP;
    END LOOP;

    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('dashboard-recording-detail:' ||
                                    target_recording_id, 0));
    SELECT detail.projection_sequence, detail.semantic_view
      INTO existing_sequence, existing_view
      FROM public.dashboard_recording_detail_projection AS detail
     WHERE detail.recording_id = target_recording_id;
    IF FOUND THEN
        IF (existing_view - ARRAY['analysis_state',
                                  'recording_object_available']) =
           (p_view - ARRAY['analysis_state',
                           'recording_object_available']) THEN
            RETURN existing_sequence;
        END IF;
        RAISE EXCEPTION 'immutable recording detail projection conflict'
            USING ERRCODE = '23505';
    END IF;

    SELECT recording.radio_id, recording.started_utc_ns,
           recording.finished_utc_ns, recording.segment_count
      INTO recording_row
      FROM public.dashboard_recording_projection AS recording
     WHERE recording.recording_id = target_recording_id
     ORDER BY recording.projection_sequence DESC
     LIMIT 1;
    IF NOT FOUND
       OR recording_row.radio_id <> target_radio_id
       OR recording_row.started_utc_ns <>
          (p_view->>'capture_started_utc_ns')::bigint
       OR recording_row.finished_utc_ns <>
          (p_view->>'capture_finished_utc_ns')::bigint
       OR recording_row.segment_count <> segment_count THEN
        RAISE EXCEPTION 'recording detail has no matching recording projection'
            USING ERRCODE = '23503';
    END IF;

    INSERT INTO public.dashboard_recording_detail_projection(
        recording_id, semantic_view)
    VALUES (target_recording_id, p_view)
    RETURNING projection_sequence INTO inserted_sequence;
    RETURN inserted_sequence;
END
$function$;

CREATE FUNCTION public.publish_dashboard_recording_waterfall(p_view jsonb)
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    target_recording_id text;
    target_digest text;
    target_run_id text;
    target_state text;
    target_reason text;
    tile jsonb;
    power_row jsonb;
    power_value jsonb;
    axis_index integer;
    axis_start bigint;
    axis_stop bigint;
    axis_midpoint bigint;
    previous_axis_stop bigint;
    offset_value double precision;
    previous_offset double precision;
    tile_start bigint;
    tile_samples bigint;
    tile_rate double precision;
    tile_fft integer;
    tile_count integer;
    time_count integer;
    frequency_count integer;
    cell_count integer := 0;
    previous_tile_key text;
    tile_key text;
    latest record;
    inserted_sequence bigint;
BEGIN
    IF p_view IS NULL OR pg_catalog.jsonb_typeof(p_view) <> 'object'
       OR pg_catalog.octet_length(p_view::text) > 4194304
       OR (SELECT pg_catalog.count(*)
             FROM pg_catalog.jsonb_object_keys(p_view)) <> 7
       OR NOT p_view ?& ARRAY[
            'schema', 'recording_id', 'recording_identity_digest',
            'analysis_run_id', 'state', 'reason_code', 'tiles'] THEN
        RAISE EXCEPTION 'invalid dashboard recording waterfall projection'
            USING ERRCODE = '22023';
    END IF;
    IF pg_catalog.jsonb_typeof(p_view->'schema') <> 'object'
       OR (SELECT pg_catalog.count(*)
             FROM pg_catalog.jsonb_object_keys(p_view->'schema')) <> 2
       OR p_view #>> '{schema,schema_id}' <>
          'org.leo-flow.dashboard.recording-waterfall'
       OR pg_catalog.jsonb_typeof(p_view #> '{schema,version}') <> 'object'
       OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(
               p_view #> '{schema,version}')) <> 2
       OR p_view #>> '{schema,version,major}' <> '0'
       OR p_view #>> '{schema,version,minor}' <> '1' THEN
        RAISE EXCEPTION 'unsupported dashboard recording waterfall schema'
            USING ERRCODE = '22023';
    END IF;

    target_recording_id := p_view->>'recording_id';
    target_digest := p_view #>> '{recording_identity_digest,value}';
    target_run_id := p_view->>'analysis_run_id';
    target_state := p_view->>'state';
    target_reason := p_view->>'reason_code';
    IF target_recording_id !~ '^rec_[A-Za-z0-9][A-Za-z0-9._:-]*$'
       OR p_view #>> '{recording_identity_digest,algorithm}' <> 'sha256'
       OR target_digest !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_view->'recording_identity_digest') <>
          'object'
       OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(
               p_view->'recording_identity_digest')) <> 2
       OR target_state NOT IN ('unavailable', 'pending', 'complete', 'failed')
       OR (target_run_id IS NOT NULL AND target_run_id !~
           '^arun_[A-Za-z0-9][A-Za-z0-9._:-]*$')
       OR (target_reason IS NOT NULL AND target_reason !~
           '^[A-Za-z0-9][A-Za-z0-9._:-]*$')
       OR pg_catalog.jsonb_typeof(p_view->'tiles') <> 'array'
       OR pg_catalog.jsonb_array_length(p_view->'tiles') > 64 THEN
        RAISE EXCEPTION 'invalid dashboard recording waterfall values'
            USING ERRCODE = '22023';
    END IF;

    tile_count := pg_catalog.jsonb_array_length(p_view->'tiles');
    IF (target_state = 'unavailable' AND
        (target_run_id IS NOT NULL OR target_reason IS NOT NULL OR tile_count <> 0))
       OR (target_state = 'pending' AND
           (target_run_id IS NULL OR target_reason IS NOT NULL OR tile_count <> 0))
       OR (target_state = 'failed' AND
           (target_run_id IS NULL OR target_reason IS NULL OR tile_count <> 0))
       OR (target_state = 'complete' AND
           (target_run_id IS NULL OR target_reason IS NOT NULL OR tile_count = 0))
    THEN
        RAISE EXCEPTION 'inconsistent dashboard waterfall state'
            USING ERRCODE = '22023';
    END IF;

    FOR tile IN
        SELECT item.value
          FROM pg_catalog.jsonb_array_elements(p_view->'tiles') AS item(value)
    LOOP
        IF pg_catalog.jsonb_typeof(tile) <> 'object'
           OR (SELECT pg_catalog.count(*)
                 FROM pg_catalog.jsonb_object_keys(tile)) <> 15
           OR NOT tile ?& ARRAY[
                'segment_id', 'receiver_chain_id', 'segment_start_utc_ns',
                'segment_sample_count', 'center_frequency_hz', 'sample_rate_hz',
                'fft_window_samples', 'time_bin_start_samples',
                'time_bin_stop_samples', 'time_bin_midpoint_utc_ns',
                'frequency_bin_offsets_hz', 'power_db', 'power_reference',
                'floor_db', 'ceiling_db']
           OR tile->>'segment_id' !~ '^seg_[A-Za-z0-9][A-Za-z0-9._:-]*$'
           OR tile->>'receiver_chain_id' !~
              '^rx_[A-Za-z0-9][A-Za-z0-9._:-]*$'
           OR (tile->>'segment_start_utc_ns')::bigint < 0
           OR (tile->>'segment_sample_count')::bigint <= 0
           OR (tile->>'center_frequency_hz')::double precision <= 0
           OR (tile->>'sample_rate_hz')::double precision <= 0
           OR (tile->>'fft_window_samples')::integer < 8
           OR (tile->>'fft_window_samples')::integer >
              (tile->>'segment_sample_count')::bigint
           OR ((tile->>'fft_window_samples')::integer &
               ((tile->>'fft_window_samples')::integer - 1)) <> 0
           OR tile->>'power_reference' <> 'counts-squared-per-bin'
           OR (tile->>'ceiling_db')::double precision <=
              (tile->>'floor_db')::double precision
           OR (tile->>'center_frequency_hz')::double precision IN
              ('NaN'::double precision, 'Infinity'::double precision,
               '-Infinity'::double precision)
           OR (tile->>'sample_rate_hz')::double precision IN
              ('NaN'::double precision, 'Infinity'::double precision,
               '-Infinity'::double precision)
           OR (tile->>'floor_db')::double precision IN
              ('NaN'::double precision, 'Infinity'::double precision,
               '-Infinity'::double precision)
           OR (tile->>'ceiling_db')::double precision IN
              ('NaN'::double precision, 'Infinity'::double precision,
               '-Infinity'::double precision)
           OR pg_catalog.jsonb_typeof(tile->'time_bin_start_samples') <> 'array'
           OR pg_catalog.jsonb_typeof(tile->'time_bin_stop_samples') <> 'array'
           OR pg_catalog.jsonb_typeof(tile->'time_bin_midpoint_utc_ns') <> 'array'
           OR pg_catalog.jsonb_typeof(tile->'frequency_bin_offsets_hz') <> 'array'
           OR pg_catalog.jsonb_typeof(tile->'power_db') <> 'array'
        THEN
            RAISE EXCEPTION 'invalid dashboard waterfall tile'
                USING ERRCODE = '22023';
        END IF;
        tile_start := (tile->>'segment_start_utc_ns')::bigint;
        tile_samples := (tile->>'segment_sample_count')::bigint;
        tile_rate := (tile->>'sample_rate_hz')::double precision;
        tile_fft := (tile->>'fft_window_samples')::integer;
        tile_key := (tile->>'segment_id') || ':' ||
                    (tile->>'receiver_chain_id');
        IF previous_tile_key IS NOT NULL AND previous_tile_key >= tile_key THEN
            RAISE EXCEPTION 'dashboard waterfall tiles are not canonical'
                USING ERRCODE = '22023';
        END IF;
        previous_tile_key := tile_key;
        time_count := pg_catalog.jsonb_array_length(tile->'power_db');
        frequency_count := pg_catalog.jsonb_array_length(
            tile->'frequency_bin_offsets_hz');
        IF time_count NOT BETWEEN 1 AND 128
           OR frequency_count NOT BETWEEN 1 AND 128
           OR pg_catalog.jsonb_array_length(tile->'time_bin_start_samples') <>
              time_count
           OR pg_catalog.jsonb_array_length(tile->'time_bin_stop_samples') <>
              time_count
           OR pg_catalog.jsonb_array_length(tile->'time_bin_midpoint_utc_ns') <>
              time_count THEN
            RAISE EXCEPTION 'invalid dashboard waterfall axes'
                USING ERRCODE = '22023';
        END IF;
        previous_axis_stop := -1;
        FOR axis_index IN 0..(time_count - 1)
        LOOP
            axis_start := (tile->'time_bin_start_samples'->>axis_index)::bigint;
            axis_stop := (tile->'time_bin_stop_samples'->>axis_index)::bigint;
            axis_midpoint :=
                (tile->'time_bin_midpoint_utc_ns'->>axis_index)::bigint;
            IF axis_start < 0
               OR axis_stop <= axis_start
               OR axis_stop - axis_start <> tile_fft
               OR axis_stop > tile_samples
               OR axis_start < previous_axis_stop
               OR axis_midpoint < 0
               OR axis_midpoint <> tile_start + pg_catalog.round(
                    (axis_start + axis_stop) * 500000000.0 / tile_rate
                  )::bigint THEN
                RAISE EXCEPTION 'invalid dashboard waterfall time axis'
                    USING ERRCODE = '22023';
            END IF;
            previous_axis_stop := axis_stop;
        END LOOP;
        previous_offset := NULL;
        FOR axis_index IN 0..(frequency_count - 1)
        LOOP
            offset_value :=
                (tile->'frequency_bin_offsets_hz'->>axis_index)::double precision;
            IF offset_value IN
               ('NaN'::double precision, 'Infinity'::double precision,
                '-Infinity'::double precision)
               OR offset_value < -tile_rate / 2
               OR offset_value >= tile_rate / 2
               OR (previous_offset IS NOT NULL AND
                   offset_value <= previous_offset) THEN
                RAISE EXCEPTION 'invalid dashboard waterfall frequency axis'
                    USING ERRCODE = '22023';
            END IF;
            previous_offset := offset_value;
        END LOOP;
        FOR power_row IN
            SELECT item.value
              FROM pg_catalog.jsonb_array_elements(tile->'power_db') AS item(value)
        LOOP
            IF pg_catalog.jsonb_typeof(power_row) <> 'array'
               OR pg_catalog.jsonb_array_length(power_row) <> frequency_count THEN
                RAISE EXCEPTION 'invalid dashboard waterfall power row'
                    USING ERRCODE = '22023';
            END IF;
            FOR power_value IN
                SELECT item.value
                  FROM pg_catalog.jsonb_array_elements(power_row) AS item(value)
            LOOP
                IF pg_catalog.jsonb_typeof(power_value) <> 'number'
                   OR (power_value #>> '{}')::double precision IN
                      ('NaN'::double precision, 'Infinity'::double precision,
                       '-Infinity'::double precision) THEN
                    RAISE EXCEPTION 'invalid dashboard waterfall power value'
                        USING ERRCODE = '22023';
                END IF;
            END LOOP;
        END LOOP;
        cell_count := cell_count + time_count * frequency_count;
        IF cell_count > 262144 THEN
            RAISE EXCEPTION 'dashboard waterfall exceeds its cell bound'
                USING ERRCODE = '22023';
        END IF;
    END LOOP;

    PERFORM 1
      FROM public.dashboard_recording_projection AS recording
     WHERE recording.recording_id = target_recording_id
     LIMIT 1;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'waterfall has no recording projection'
            USING ERRCODE = '23503';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('dashboard-recording-waterfall:' ||
                                    target_recording_id, 0));
    SELECT waterfall.projection_sequence,
           waterfall.recording_identity_digest_value,
           waterfall.analysis_run_id, waterfall.state,
           waterfall.semantic_view
      INTO latest
      FROM public.dashboard_recording_waterfall_projection AS waterfall
     WHERE waterfall.recording_id = target_recording_id
     ORDER BY waterfall.projection_sequence DESC
     LIMIT 1;
    IF FOUND THEN
        IF latest.semantic_view = p_view THEN
            RETURN latest.projection_sequence;
        END IF;
        IF latest.recording_identity_digest_value <> target_digest THEN
            RAISE EXCEPTION 'waterfall recording identity cannot change'
                USING ERRCODE = '23505';
        END IF;
        IF latest.state = 'complete'
           OR target_state = 'unavailable'
           OR (latest.state = 'pending' AND target_state = 'pending'
               AND latest.analysis_run_id <> target_run_id)
           OR (latest.state = 'pending' AND target_state IN ('complete', 'failed')
               AND latest.analysis_run_id <> target_run_id)
           OR (latest.state = 'failed' AND
               (target_state <> 'pending'
                OR latest.analysis_run_id = target_run_id)) THEN
            RAISE EXCEPTION 'invalid waterfall projection replay or regression'
                USING ERRCODE = '23505';
        END IF;
    END IF;

    INSERT INTO public.dashboard_recording_waterfall_projection(
        recording_id, recording_identity_digest_value, analysis_run_id, state,
        reason_code, tile_count, cell_count, semantic_view)
    VALUES (target_recording_id, target_digest, target_run_id, target_state,
            target_reason, tile_count, cell_count, p_view)
    RETURNING projection_sequence INTO inserted_sequence;
    RETURN inserted_sequence;
END
$function$;

GRANT SELECT, INSERT ON public.dashboard_recording_detail_projection,
    public.dashboard_recording_waterfall_projection TO leo_routine_owner;
GRANT SELECT ON public.dashboard_recording_projection TO leo_routine_owner;
GRANT USAGE, SELECT ON SEQUENCE public.dashboard_projection_sequence
TO leo_routine_owner;

ALTER FUNCTION public.publish_dashboard_recording_detail(jsonb)
    OWNER TO leo_routine_owner;
ALTER FUNCTION public.publish_dashboard_recording_waterfall(jsonb)
    OWNER TO leo_routine_owner;

REVOKE ALL ON public.dashboard_recording_detail_projection,
    public.dashboard_recording_waterfall_projection
FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard, leo_maintenance;
REVOKE ALL ON FUNCTION public.publish_dashboard_recording_detail(jsonb)
FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard, leo_maintenance;
REVOKE ALL ON FUNCTION public.publish_dashboard_recording_waterfall(jsonb)
FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard, leo_maintenance;

GRANT SELECT ON public.dashboard_recording_detail_projection,
    public.dashboard_recording_waterfall_projection TO leo_dashboard;
GRANT EXECUTE ON FUNCTION public.publish_dashboard_recording_detail(jsonb)
TO leo_capture;
GRANT EXECUTE ON FUNCTION public.publish_dashboard_recording_waterfall(jsonb)
TO leo_analysis;

COMMIT;
