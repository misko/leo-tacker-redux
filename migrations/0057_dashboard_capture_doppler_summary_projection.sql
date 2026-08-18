BEGIN;

CREATE TABLE public.dashboard_capture_doppler_product_v0_1 (
    doppler_id text PRIMARY KEY
        REFERENCES public.recording_doppler_analysis(doppler_id),
    recording_id text NOT NULL REFERENCES public.recording(recording_id),
    waterfall_product_id text NOT NULL
        REFERENCES public.recording_waterfall_v0_2(product_id),
    segment_id text NOT NULL,
    receiver_chain_id text NOT NULL,
    algorithm_version text NOT NULL CHECK (algorithm_version <> ''),
    candidate_count integer NOT NULL CHECK (candidate_count BETWEEN 0 AND 32),
    published_at_utc timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (waterfall_product_id, segment_id, receiver_chain_id)
);

CREATE INDEX dashboard_capture_doppler_product_recording_idx
    ON public.dashboard_capture_doppler_product_v0_1(
        recording_id, published_at_utc DESC, waterfall_product_id DESC
    );

CREATE TABLE public.dashboard_capture_doppler_candidate_v0_1 (
    doppler_id text NOT NULL
        REFERENCES public.dashboard_capture_doppler_product_v0_1(doppler_id),
    candidate_rank integer NOT NULL CHECK (candidate_rank BETWEEN 1 AND 32),
    candidate_id text NOT NULL CHECK (candidate_id <> ''),
    model text NOT NULL CHECK (model IN ('constant', 'linear', 'quadratic')),
    drift_rate_hz_s double precision NOT NULL CHECK (
        drift_rate_hz_s <> 'NaN'::double precision
        AND drift_rate_hz_s > '-Infinity'::double precision
        AND drift_rate_hz_s < 'Infinity'::double precision
    ),
    ranking_score double precision NOT NULL CHECK (
        ranking_score <> 'NaN'::double precision
        AND ranking_score > '-Infinity'::double precision
        AND ranking_score < 'Infinity'::double precision
    ),
    PRIMARY KEY (doppler_id, candidate_rank),
    UNIQUE (doppler_id, candidate_id)
);

CREATE FUNCTION public.publish_dashboard_capture_doppler_product_v0_1(
    text, text, jsonb
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    requested_doppler_id alias for $1;
    requested_algorithm_version alias for $2;
    requested_candidates alias for $3;
    source public.recording_doppler_analysis%ROWTYPE;
    item jsonb;
    expected_count integer;
BEGIN
    IF requested_algorithm_version = ''
       OR pg_catalog.jsonb_typeof(requested_candidates) <> 'array'
       OR pg_catalog.jsonb_array_length(requested_candidates) > 32 THEN
        RAISE EXCEPTION 'invalid Doppler summary projection'
            USING ERRCODE = '22023';
    END IF;

    SELECT * INTO source
      FROM public.recording_doppler_analysis
     WHERE doppler_id = requested_doppler_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Doppler summary source is not cataloged'
            USING ERRCODE = '23503';
    END IF;

    expected_count := pg_catalog.jsonb_array_length(requested_candidates);
    IF expected_count <> source.candidate_count THEN
        RAISE EXCEPTION 'Doppler summary candidate count differs from catalog'
            USING ERRCODE = '22023';
    END IF;

    INSERT INTO public.dashboard_capture_doppler_product_v0_1(
        doppler_id, recording_id, waterfall_product_id, segment_id,
        receiver_chain_id, algorithm_version, candidate_count
    ) VALUES (
        source.doppler_id, source.recording_id, source.waterfall_product_id,
        source.segment_id, source.receiver_chain_id,
        requested_algorithm_version, expected_count
    ) ON CONFLICT DO NOTHING;

    PERFORM 1
      FROM public.dashboard_capture_doppler_product_v0_1 product
     WHERE product.doppler_id = source.doppler_id
       AND product.recording_id = source.recording_id
       AND product.waterfall_product_id = source.waterfall_product_id
       AND product.segment_id = source.segment_id
       AND product.receiver_chain_id = source.receiver_chain_id
       AND product.algorithm_version = requested_algorithm_version
       AND product.candidate_count = expected_count;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Doppler summary product conflicts'
            USING ERRCODE = '23505';
    END IF;

    FOR item IN SELECT value FROM pg_catalog.jsonb_array_elements(requested_candidates)
    LOOP
        IF pg_catalog.jsonb_typeof(item) <> 'object'
           OR (item->>'candidate_rank')::integer NOT BETWEEN 1 AND 32
           OR item->>'candidate_id' = ''
           OR item->>'model' NOT IN ('constant', 'linear', 'quadratic')
           OR (item->>'drift_rate_hz_s')::double precision = 'NaN'::double precision
           OR (item->>'drift_rate_hz_s')::double precision <= '-Infinity'::double precision
           OR (item->>'drift_rate_hz_s')::double precision >= 'Infinity'::double precision
           OR (item->>'ranking_score')::double precision = 'NaN'::double precision
           OR (item->>'ranking_score')::double precision <= '-Infinity'::double precision
           OR (item->>'ranking_score')::double precision >= 'Infinity'::double precision THEN
            RAISE EXCEPTION 'invalid Doppler summary candidate'
                USING ERRCODE = '22023';
        END IF;

        INSERT INTO public.dashboard_capture_doppler_candidate_v0_1(
            doppler_id, candidate_rank, candidate_id, model,
            drift_rate_hz_s, ranking_score
        ) VALUES (
            source.doppler_id, (item->>'candidate_rank')::integer,
            item->>'candidate_id', item->>'model',
            (item->>'drift_rate_hz_s')::double precision,
            (item->>'ranking_score')::double precision
        ) ON CONFLICT DO NOTHING;

        PERFORM 1
          FROM public.dashboard_capture_doppler_candidate_v0_1 candidate
         WHERE candidate.doppler_id = source.doppler_id
           AND candidate.candidate_rank = (item->>'candidate_rank')::integer
           AND candidate.candidate_id = item->>'candidate_id'
           AND candidate.model = item->>'model'
           AND candidate.drift_rate_hz_s =
               (item->>'drift_rate_hz_s')::double precision
           AND candidate.ranking_score =
               (item->>'ranking_score')::double precision;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Doppler summary candidate conflicts'
                USING ERRCODE = '23505';
        END IF;
    END LOOP;

    IF (SELECT count(*)
          FROM public.dashboard_capture_doppler_candidate_v0_1 candidate
         WHERE candidate.doppler_id = source.doppler_id) <> expected_count THEN
        RAISE EXCEPTION 'Doppler summary candidate closure conflicts'
            USING ERRCODE = '23505';
    END IF;
    RETURN true;
END
$function$;

CREATE FUNCTION public.read_dashboard_capture_doppler_summaries_v0_1(
    bigint, bigint, integer
) RETURNS TABLE(
    recording_id text,
    radio_id text,
    analysis_state text,
    summary_state text,
    assignment_count bigint,
    lnb_id text,
    receiver_chain_id text,
    segment_id text,
    candidate_id text,
    model text,
    drift_rate_hz_s double precision,
    ranking_score double precision,
    doppler_id text,
    algorithm_version text,
    original_recording_count bigint
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF $1 < 0 OR $2 <= $1 OR $3 NOT BETWEEN 1 AND 100 THEN
        RAISE EXCEPTION 'invalid capture Doppler summary interval or bound'
            USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    WITH latest_batches AS (
        SELECT DISTINCT ON (batch.batch_id)
               batch.projection_sequence, batch.batch_id,
               batch.requested_start_utc_ns
          FROM public.dashboard_capture_batch_projection batch
         WHERE batch.requested_start_utc_ns >= $1
           AND batch.requested_start_utc_ns < $2
         ORDER BY batch.batch_id, batch.projection_sequence DESC
    ), successful AS (
        SELECT DISTINCT ON (attempt.recording_id)
               attempt.recording_id, attempt.radio_id, attempt.analysis_state,
               attempt.observed_start_utc_ns, batch.requested_start_utc_ns
          FROM latest_batches batch
          JOIN public.dashboard_capture_attempt_projection attempt
            ON attempt.projection_sequence = batch.projection_sequence
         WHERE attempt.capture_state = 'succeeded'
           AND attempt.recording_id IS NOT NULL
         ORDER BY attempt.recording_id,
                  batch.requested_start_utc_ns DESC, batch.batch_id DESC
    ), bounded AS (
        SELECT successful.*,
               count(*) OVER () AS original_recording_count
          FROM successful
         ORDER BY requested_start_utc_ns DESC, recording_id
         LIMIT $3
    ), assignments AS (
        SELECT bounded.*,
               chain.receiver_chain_id, chain.lnb_id,
               count(chain.receiver_chain_id) OVER (
                   PARTITION BY bounded.recording_id
               ) AS assignment_count
          FROM bounded
          LEFT JOIN public.recording_hardware_link link
            ON link.recording_id = bounded.recording_id
          LEFT JOIN public.hardware_receiver_chain chain
            ON chain.snapshot_id = link.hardware_snapshot_id
           AND chain.radio_id = bounded.radio_id
           AND chain.valid_from_utc_ns <= bounded.observed_start_utc_ns
           AND (chain.valid_until_utc_ns IS NULL
                OR bounded.observed_start_utc_ns < chain.valid_until_utc_ns)
    ), receipt_closure AS (
        SELECT product.recording_id, product.waterfall_product_id,
               max(product.published_at_utc) AS published_at_utc,
               sum(product.candidate_count)::bigint AS candidate_count
          FROM public.dashboard_capture_doppler_product_v0_1 product
          JOIN public.recording_waterfall_v0_2 waterfall
            ON waterfall.product_id = product.waterfall_product_id
          LEFT JOIN public.dashboard_capture_doppler_candidate_v0_1 candidate
            ON candidate.doppler_id = product.doppler_id
         GROUP BY product.recording_id, product.waterfall_product_id,
                  waterfall.tile_count
        HAVING count(DISTINCT product.doppler_id) = waterfall.tile_count
           AND count(candidate.candidate_rank) = sum(product.candidate_count)
    ), selected_product AS (
        SELECT DISTINCT ON (closure.recording_id) closure.*
          FROM receipt_closure closure
         ORDER BY closure.recording_id, closure.published_at_utc DESC,
                  closure.waterfall_product_id DESC
    ), ranked_candidate AS (
        SELECT product.recording_id, product.receiver_chain_id,
               product.segment_id, candidate.candidate_id, candidate.model,
               candidate.drift_rate_hz_s, candidate.ranking_score,
               product.doppler_id, product.algorithm_version,
               row_number() OVER (
                   PARTITION BY product.recording_id, product.receiver_chain_id
                   ORDER BY candidate.ranking_score DESC,
                            candidate.candidate_id DESC
               ) AS selection_rank
          FROM selected_product selected
          JOIN public.dashboard_capture_doppler_product_v0_1 product
            ON product.recording_id = selected.recording_id
           AND product.waterfall_product_id = selected.waterfall_product_id
          JOIN public.dashboard_capture_doppler_candidate_v0_1 candidate
            ON candidate.doppler_id = product.doppler_id
    )
    SELECT assignment.recording_id, assignment.radio_id,
           assignment.analysis_state,
           CASE
             WHEN candidate.candidate_id IS NOT NULL THEN 'complete'
             WHEN selected.candidate_count = 0 THEN 'no_candidate'
             WHEN assignment.analysis_state IN ('pending', 'running') THEN 'pending'
             WHEN assignment.analysis_state IN ('failed', 'error') THEN 'failed'
             ELSE 'not_analyzed'
           END AS summary_state,
           assignment.assignment_count,
           CASE WHEN candidate.candidate_id IS NULL THEN NULL
                ELSE assignment.lnb_id END,
           candidate.receiver_chain_id, candidate.segment_id,
           candidate.candidate_id, candidate.model,
           candidate.drift_rate_hz_s, candidate.ranking_score,
           candidate.doppler_id, candidate.algorithm_version,
           assignment.original_recording_count
      FROM assignments assignment
      LEFT JOIN selected_product selected
        ON selected.recording_id = assignment.recording_id
      LEFT JOIN ranked_candidate candidate
        ON candidate.recording_id = assignment.recording_id
       AND candidate.receiver_chain_id = assignment.receiver_chain_id
       AND candidate.selection_rank = 1
     ORDER BY assignment.recording_id, assignment.lnb_id,
              assignment.receiver_chain_id;
END
$function$;

ALTER TABLE public.dashboard_capture_doppler_product_v0_1
    OWNER TO leo_routine_owner;
ALTER TABLE public.dashboard_capture_doppler_candidate_v0_1
    OWNER TO leo_routine_owner;
GRANT SELECT, INSERT ON
    public.dashboard_capture_doppler_product_v0_1,
    public.dashboard_capture_doppler_candidate_v0_1
TO leo_routine_owner;
REVOKE ALL ON
    public.dashboard_capture_doppler_product_v0_1,
    public.dashboard_capture_doppler_candidate_v0_1
FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard, leo_maintenance;

ALTER FUNCTION public.publish_dashboard_capture_doppler_product_v0_1(
    text, text, jsonb
) OWNER TO leo_routine_owner;
ALTER FUNCTION public.read_dashboard_capture_doppler_summaries_v0_1(
    bigint, bigint, integer
) OWNER TO leo_routine_owner;
REVOKE ALL ON FUNCTION
    public.publish_dashboard_capture_doppler_product_v0_1(text, text, jsonb),
    public.read_dashboard_capture_doppler_summaries_v0_1(bigint, bigint, integer)
FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard, leo_maintenance;
GRANT EXECUTE ON FUNCTION
    public.publish_dashboard_capture_doppler_product_v0_1(text, text, jsonb)
TO leo_analysis;
GRANT EXECUTE ON FUNCTION
    public.read_dashboard_capture_doppler_summaries_v0_1(bigint, bigint, integer)
TO leo_dashboard;

COMMIT;
