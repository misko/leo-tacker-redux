BEGIN;

CREATE FUNCTION public.read_pending_dashboard_capture_qam_products_v0_1(integer)
RETURNS TABLE(source_kind text,recording_id text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp
AS $function$
WITH products AS (
  SELECT 'adaptive-v0.4'::text AS source_kind,analysis_id,recording_id,published_at_utc
    FROM public.recording_starlink_adaptive_qam_v0_4
  UNION ALL
  SELECT 'acquired-v0.3',analysis_id,recording_id,published_at_utc
    FROM public.recording_starlink_acquired_constellation_v0_3
), latest AS (
  SELECT DISTINCT ON (source_kind,recording_id) * FROM products
   ORDER BY source_kind,recording_id,published_at_utc DESC,analysis_id DESC
)
SELECT latest.source_kind,latest.recording_id
  FROM latest
 WHERE $1 BETWEEN 1 AND 100
   AND NOT EXISTS (
     SELECT 1 FROM public.dashboard_capture_qam_candidate_v0_1 summary
      WHERE summary.source_kind=latest.source_kind
        AND summary.analysis_id=latest.analysis_id
   )
 ORDER BY latest.published_at_utc DESC,latest.analysis_id DESC
 LIMIT $1;
$function$;

ALTER FUNCTION public.read_pending_dashboard_capture_qam_products_v0_1(integer)
  OWNER TO leo_routine_owner;
REVOKE ALL ON FUNCTION public.read_pending_dashboard_capture_qam_products_v0_1(integer)
  FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;
GRANT EXECUTE ON FUNCTION public.read_pending_dashboard_capture_qam_products_v0_1(integer)
  TO leo_analysis;

COMMIT;
