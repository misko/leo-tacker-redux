BEGIN;

CREATE FUNCTION public.read_dashboard_doppler_aggregate_interval_count_v1(
    bigint, bigint
) RETURNS TABLE(interval_recording_count bigint, available_recording_count bigint)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF $1 < 0 OR $2 <= $1 THEN
        RAISE EXCEPTION 'invalid Doppler aggregate count interval'
            USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    WITH latest_recording AS (
        SELECT DISTINCT ON (projection.recording_id) projection.recording_id
          FROM public.dashboard_recording_projection AS projection
         WHERE projection.started_utc_ns >= $1
           AND projection.started_utc_ns < $2
         ORDER BY projection.recording_id, projection.projection_sequence DESC
    )
    SELECT count(DISTINCT recording.recording_id),
           count(DISTINCT doppler.recording_id) FILTER (
               WHERE basic.lifecycle_state = 'live'
                 AND advanced.lifecycle_state = 'live'
           )
      FROM latest_recording AS recording
      LEFT JOIN public.recording_doppler_analysis AS doppler
        ON doppler.recording_id = recording.recording_id
      LEFT JOIN public.object_blob AS basic
        ON (basic.digest_algorithm, basic.digest_value) =
           (doppler.basic_bundle_digest_algorithm, doppler.basic_bundle_digest_value)
      LEFT JOIN public.object_blob AS advanced
        ON (advanced.digest_algorithm, advanced.digest_value) =
           (doppler.advanced_bundle_digest_algorithm, doppler.advanced_bundle_digest_value);
END
$function$;

CREATE FUNCTION public.read_dashboard_doppler_aggregate_interval_v1(
    bigint, bigint, integer
) RETURNS TABLE(
    recording_id text,
    radio_id text,
    started_utc_ns bigint,
    doppler_id text,
    waterfall_product_id text,
    segment_id text,
    receiver_chain_id text,
    spectrogram_digest_algorithm text,
    spectrogram_digest_value text,
    basic_config_digest_algorithm text,
    basic_config_digest_value text,
    advanced_config_digest_algorithm text,
    advanced_config_digest_value text,
    basic_bundle_digest_algorithm text,
    basic_bundle_digest_value text,
    basic_bundle_byte_count bigint,
    basic_bundle_media_type text,
    basic_bundle_format_id text,
    basic_bundle_locator text,
    advanced_bundle_digest_algorithm text,
    advanced_bundle_digest_value text,
    advanced_bundle_byte_count bigint,
    advanced_bundle_media_type text,
    advanced_bundle_format_id text,
    advanced_bundle_locator text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF $1 < 0 OR $2 <= $1 OR $3 < 1 OR $3 > 513 THEN
        RAISE EXCEPTION 'invalid Doppler aggregate interval or limit'
            USING ERRCODE = '22023';
    END IF;

    RETURN QUERY
    WITH latest_recording AS (
        SELECT DISTINCT ON (projection.recording_id)
               projection.recording_id,
               projection.radio_id,
               projection.started_utc_ns
          FROM public.dashboard_recording_projection AS projection
         WHERE projection.started_utc_ns >= $1
           AND projection.started_utc_ns < $2
         ORDER BY projection.recording_id, projection.projection_sequence DESC
    )
    SELECT recording.recording_id,
           recording.radio_id,
           recording.started_utc_ns,
           doppler.doppler_id,
           doppler.waterfall_product_id,
           doppler.segment_id,
           doppler.receiver_chain_id,
           doppler.spectrogram_digest_algorithm,
           doppler.spectrogram_digest_value,
           doppler.basic_config_digest_algorithm,
           doppler.basic_config_digest_value,
           doppler.advanced_config_digest_algorithm,
           doppler.advanced_config_digest_value,
           doppler.basic_bundle_digest_algorithm,
           doppler.basic_bundle_digest_value,
           basic.byte_count,
           basic.media_type,
           basic.format_id,
           basic.locator,
           doppler.advanced_bundle_digest_algorithm,
           doppler.advanced_bundle_digest_value,
           advanced.byte_count,
           advanced.media_type,
           advanced.format_id,
           advanced.locator
      FROM latest_recording AS recording
      JOIN public.recording_doppler_analysis AS doppler
        ON doppler.recording_id = recording.recording_id
      JOIN public.object_blob AS basic
        ON (basic.digest_algorithm, basic.digest_value) =
           (doppler.basic_bundle_digest_algorithm, doppler.basic_bundle_digest_value)
      JOIN public.object_blob AS advanced
        ON (advanced.digest_algorithm, advanced.digest_value) =
           (doppler.advanced_bundle_digest_algorithm, doppler.advanced_bundle_digest_value)
     WHERE basic.lifecycle_state = 'live'
       AND advanced.lifecycle_state = 'live'
     ORDER BY recording.started_utc_ns DESC,
              recording.recording_id DESC,
              doppler.segment_id,
              doppler.receiver_chain_id,
              doppler.doppler_id
     LIMIT $3;
END
$function$;

ALTER FUNCTION public.read_dashboard_doppler_aggregate_interval_v1(
    bigint, bigint, integer
) OWNER TO leo_routine_owner;
ALTER FUNCTION public.read_dashboard_doppler_aggregate_interval_count_v1(
    bigint, bigint
) OWNER TO leo_routine_owner;

REVOKE ALL ON FUNCTION public.read_dashboard_doppler_aggregate_interval_v1(
    bigint, bigint, integer
) FROM PUBLIC, leo_capture, leo_analysis, leo_maintenance;
REVOKE ALL ON FUNCTION public.read_dashboard_doppler_aggregate_interval_count_v1(
    bigint, bigint
) FROM PUBLIC, leo_capture, leo_analysis, leo_maintenance;
GRANT EXECUTE ON FUNCTION public.read_dashboard_doppler_aggregate_interval_v1(
    bigint, bigint, integer
) TO leo_dashboard;
GRANT EXECUTE ON FUNCTION public.read_dashboard_doppler_aggregate_interval_count_v1(
    bigint, bigint
) TO leo_dashboard;

COMMIT;
