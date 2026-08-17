BEGIN;

CREATE FUNCTION public.read_dashboard_surrogate_null_interval_v1(
    bigint, bigint, integer
) RETURNS TABLE(
    recording_id text,
    radio_id text,
    started_utc_ns bigint,
    analysis_id text,
    bundle_digest_algorithm text,
    bundle_digest_value text,
    bundle_byte_count bigint,
    bundle_media_type text,
    bundle_format_id text,
    bundle_locator text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF $1 < 0 OR $2 <= $1 OR $3 < 1 OR $3 > 513 THEN
        RAISE EXCEPTION 'invalid surrogate distribution interval or limit'
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
    ), latest_product AS (
        SELECT DISTINCT ON (product.recording_id)
               product.recording_id,
               product.analysis_id,
               product.bundle_digest_algorithm,
               product.bundle_digest_value,
               product.published_at_utc
          FROM public.recording_starlink_surrogate_null AS product
         WHERE product.result_state = 'candidates'
         ORDER BY product.recording_id,
                  product.published_at_utc DESC,
                  product.analysis_id DESC
    )
    SELECT recording.recording_id,
           recording.radio_id,
           recording.started_utc_ns,
           product.analysis_id,
           product.bundle_digest_algorithm,
           product.bundle_digest_value,
           object.byte_count,
           object.media_type,
           object.format_id,
           object.locator
      FROM latest_recording AS recording
      JOIN latest_product AS product USING (recording_id)
      JOIN public.object_blob AS object
        ON (object.digest_algorithm, object.digest_value) =
           (product.bundle_digest_algorithm, product.bundle_digest_value)
     WHERE object.lifecycle_state = 'live'
     ORDER BY recording.started_utc_ns DESC, recording.recording_id DESC
     LIMIT $3;
END
$function$;

ALTER FUNCTION public.read_dashboard_surrogate_null_interval_v1(
    bigint, bigint, integer
) OWNER TO leo_routine_owner;

REVOKE ALL ON FUNCTION public.read_dashboard_surrogate_null_interval_v1(
    bigint, bigint, integer
) FROM PUBLIC, leo_capture, leo_analysis, leo_maintenance;
GRANT EXECUTE ON FUNCTION public.read_dashboard_surrogate_null_interval_v1(
    bigint, bigint, integer
) TO leo_dashboard;

COMMIT;
