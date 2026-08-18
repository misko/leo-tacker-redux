BEGIN;

CREATE FUNCTION public.read_dashboard_capture_qam_snapshot_v0_1(
  bigint,bigint,integer,text[]
) RETURNS TABLE(
  recording_id text,radio_id text,analysis_state text,summary_state text,
  assignment_count bigint,source_kind text,analysis_id text,
  receipt_candidate_count integer,lnb_id text,receiver_chain_id text,
  segment_id text,edge text,qam_goodness double precision,
  hard_symbol_accuracy double precision,rms_evm double precision,
  window_count integer
) LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp
AS $function$
WITH requested AS (
  SELECT value AS recording_id,position
    FROM unnest($4) WITH ORDINALITY AS input(value,position)
   WHERE $1 >= 0 AND $2 > $1 AND $3 BETWEEN 1 AND 100
     AND cardinality($4) <= $3
     AND cardinality($4) = (
       SELECT count(DISTINCT duplicate.value)
         FROM unnest($4) AS duplicate(value)
     )
), latest_batches AS (
  SELECT DISTINCT ON (batch.batch_id)
         batch.projection_sequence,batch.batch_id,batch.requested_start_utc_ns
    FROM public.dashboard_capture_batch_projection batch
   WHERE batch.requested_start_utc_ns >= $1
     AND batch.requested_start_utc_ns < $2
   ORDER BY batch.batch_id,batch.projection_sequence DESC
), successful AS (
  SELECT DISTINCT ON (attempt.recording_id)
         requested.position,attempt.recording_id,attempt.radio_id,
         attempt.analysis_state,attempt.observed_start_utc_ns
    FROM requested
    JOIN public.dashboard_capture_attempt_projection attempt
      ON attempt.recording_id=requested.recording_id
    JOIN latest_batches batch
      ON batch.projection_sequence=attempt.projection_sequence
   WHERE attempt.capture_state='succeeded'
   ORDER BY attempt.recording_id,batch.requested_start_utc_ns DESC,
            batch.projection_sequence DESC
), assignments AS (
  SELECT successful.*,chain.lnb_id,chain.receiver_chain_id
    FROM successful
    LEFT JOIN public.recording_hardware_link link
      ON link.recording_id=successful.recording_id
    LEFT JOIN public.hardware_receiver_chain chain
      ON chain.snapshot_id=link.hardware_snapshot_id
     AND chain.radio_id=successful.radio_id
     AND chain.valid_from_utc_ns<=successful.observed_start_utc_ns
     AND (chain.valid_until_utc_ns IS NULL
          OR successful.observed_start_utc_ns<chain.valid_until_utc_ns)
), assignment_counts AS (
  SELECT recording_id,count(receiver_chain_id) AS assignment_count
    FROM assignments GROUP BY recording_id
), products AS (
  SELECT 'adaptive-v0.4'::text AS source_kind,analysis_id,recording_id,
         published_at_utc,1 AS preference
    FROM public.recording_starlink_adaptive_qam_v0_4
  UNION ALL
  SELECT 'acquired-v0.3',analysis_id,recording_id,published_at_utc,0
    FROM public.recording_starlink_acquired_constellation_v0_3
), selected_product AS (
  SELECT DISTINCT ON (product.recording_id)
         product.recording_id,product.source_kind,product.analysis_id
    FROM products product JOIN successful USING(recording_id)
   ORDER BY product.recording_id,product.preference DESC,
            product.published_at_utc DESC,product.analysis_id DESC
), outcomes AS (
  SELECT successful.position,successful.recording_id,successful.radio_id,
         successful.analysis_state,counts.assignment_count,
         product.source_kind,product.analysis_id,receipt.candidate_count,
         CASE
           WHEN receipt.terminal_outcome='complete' THEN 'complete'
           WHEN receipt.terminal_outcome='no-candidate' THEN 'no_candidate'
           WHEN product.analysis_id IS NOT NULL THEN 'pending'
           WHEN successful.analysis_state IN ('pending','running') THEN 'pending'
           WHEN successful.analysis_state='failed' THEN 'failed'
           ELSE 'not_analyzed'
         END AS summary_state
    FROM successful
    JOIN assignment_counts counts USING(recording_id)
    LEFT JOIN selected_product product USING(recording_id)
    LEFT JOIN public.dashboard_capture_qam_summary_receipt_v0_2 receipt
      ON receipt.source_kind=product.source_kind
     AND receipt.analysis_id=product.analysis_id
), rows AS (
  SELECT outcome.*,candidate.lnb_id,candidate.receiver_chain_id,
         candidate.segment_id,candidate.edge,candidate.qam_goodness,
         candidate.hard_symbol_accuracy,candidate.rms_evm,
         candidate.window_count
    FROM outcomes outcome
    LEFT JOIN assignments assignment
      ON assignment.recording_id=outcome.recording_id
    LEFT JOIN public.dashboard_capture_qam_candidate_v0_1 candidate
      ON outcome.summary_state='complete'
     AND candidate.recording_id=outcome.recording_id
     AND candidate.source_kind=outcome.source_kind
     AND candidate.analysis_id=outcome.analysis_id
     AND candidate.radio_id=outcome.radio_id
     AND candidate.lnb_id=assignment.lnb_id
     AND candidate.receiver_chain_id=assignment.receiver_chain_id
)
SELECT recording_id,radio_id,analysis_state,summary_state,assignment_count,
       source_kind,analysis_id,candidate_count,lnb_id,receiver_chain_id,
       segment_id,edge,qam_goodness,hard_symbol_accuracy,rms_evm,window_count
  FROM rows
 ORDER BY position,lnb_id NULLS LAST,receiver_chain_id NULLS LAST;
$function$;

ALTER FUNCTION public.read_dashboard_capture_qam_snapshot_v0_1(
  bigint,bigint,integer,text[]
) OWNER TO leo_routine_owner;
REVOKE ALL ON FUNCTION public.read_dashboard_capture_qam_snapshot_v0_1(
  bigint,bigint,integer,text[]
) FROM PUBLIC,leo_capture,leo_analysis,leo_dashboard,leo_maintenance;
GRANT EXECUTE ON FUNCTION public.read_dashboard_capture_qam_snapshot_v0_1(
  bigint,bigint,integer,text[]
) TO leo_dashboard;

COMMIT;
