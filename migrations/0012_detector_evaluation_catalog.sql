BEGIN;

ALTER TABLE dataset_snapshot
    ADD CONSTRAINT dataset_snapshot_evaluation_authority_key
    UNIQUE (snapshot_id, snapshot_digest_algorithm, snapshot_digest_value);

CREATE TABLE detector_evaluation_report (
    evaluation_id text PRIMARY KEY CHECK (evaluation_id ~ '^eval_[0-9a-f]{64}$'),
    run_id text NOT NULL UNIQUE CHECK (run_id ~ '^erun_[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    dataset_snapshot_id text NOT NULL,
    dataset_snapshot_digest_algorithm text NOT NULL CHECK (dataset_snapshot_digest_algorithm = 'sha256'),
    dataset_snapshot_digest_value text NOT NULL CHECK (dataset_snapshot_digest_value ~ '^[0-9a-f]{64}$'),
    feature_membership_digest_algorithm text NOT NULL CHECK (feature_membership_digest_algorithm = 'sha256'),
    feature_membership_digest_value text NOT NULL CHECK (feature_membership_digest_value ~ '^[0-9a-f]{64}$'),
    threshold_rule_id text NOT NULL CHECK (threshold_rule_id <> ''),
    threshold_rule_digest_algorithm text NOT NULL CHECK (threshold_rule_digest_algorithm = 'sha256'),
    threshold_rule_digest_value text NOT NULL CHECK (threshold_rule_digest_value ~ '^[0-9a-f]{64}$'),
    calibration_dataset_id text NOT NULL CHECK (calibration_dataset_id <> ''),
    calibration_split text NOT NULL CHECK (calibration_split = 'train'),
    report_digest_algorithm text NOT NULL CHECK (report_digest_algorithm = 'sha256'),
    report_digest_value text NOT NULL CHECK (report_digest_value ~ '^[0-9a-f]{64}$'),
    method_count integer NOT NULL CHECK (method_count >= 0),
    union_window_count integer NOT NULL CHECK (union_window_count >= 0),
    warnings jsonb NOT NULL CHECK (jsonb_typeof(warnings) = 'array'),
    idempotency_key text NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (report_digest_algorithm, report_digest_value),
    FOREIGN KEY (report_digest_algorithm, report_digest_value)
        REFERENCES object_blob (digest_algorithm, digest_value),
    FOREIGN KEY (dataset_snapshot_id, dataset_snapshot_digest_algorithm, dataset_snapshot_digest_value)
        REFERENCES dataset_snapshot (snapshot_id, snapshot_digest_algorithm, snapshot_digest_value),
    FOREIGN KEY (dataset_snapshot_id, feature_membership_digest_algorithm, feature_membership_digest_value)
        REFERENCES dataset_snapshot (snapshot_id, feature_membership_digest_algorithm, feature_membership_digest_value)
);

CREATE TABLE detector_evaluation_method_summary (
    evaluation_id text NOT NULL REFERENCES detector_evaluation_report (evaluation_id),
    method_id text NOT NULL CHECK (method_id <> ''),
    split text NOT NULL CHECK (split IN ('train', 'validation', 'locked_test')),
    threshold double precision NOT NULL CHECK (
        threshold NOT IN ('NaN'::double precision,
                          'Infinity'::double precision,
                          '-Infinity'::double precision)
    ),
    score_semantics text,
    feature_set_count integer NOT NULL CHECK (feature_set_count >= 0),
    feature_set_present_count integer NOT NULL CHECK (feature_set_present_count >= 0),
    union_window_count integer NOT NULL CHECK (union_window_count >= 0),
    present_window_count integer NOT NULL CHECK (present_window_count >= 0),
    missing_window_count integer NOT NULL CHECK (missing_window_count >= 0),
    firing_count integer NOT NULL CHECK (firing_count >= 0),
    true_positive integer NOT NULL CHECK (true_positive >= 0),
    false_positive integer NOT NULL CHECK (false_positive >= 0),
    true_negative integer NOT NULL CHECK (true_negative >= 0),
    false_negative integer NOT NULL CHECK (false_negative >= 0),
    scored_prediction_count integer NOT NULL CHECK (scored_prediction_count >= 0),
    missing_prediction_count integer NOT NULL CHECK (missing_prediction_count >= 0),
    PRIMARY KEY (evaluation_id, method_id, split)
);

CREATE INDEX detector_evaluation_dataset_idx
    ON detector_evaluation_report (dataset_snapshot_id, published_at DESC);

CREATE FUNCTION validate_detector_evaluation_summary()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    target_evaluation_id text := COALESCE(NEW.evaluation_id, OLD.evaluation_id);
    expected_method_count integer;
    actual_row_count bigint;
    actual_method_count bigint;
    complete_method_count bigint;
BEGIN
    SELECT method_count INTO expected_method_count
    FROM detector_evaluation_report
    WHERE evaluation_id = target_evaluation_id;

    IF expected_method_count IS NULL THEN
        RETURN COALESCE(NEW, OLD);
    END IF;

    SELECT count(*), count(DISTINCT method_id)
    INTO actual_row_count, actual_method_count
    FROM detector_evaluation_method_summary
    WHERE evaluation_id = target_evaluation_id;

    SELECT count(*) INTO complete_method_count
    FROM (
        SELECT method_id
        FROM detector_evaluation_method_summary
        WHERE evaluation_id = target_evaluation_id
        GROUP BY method_id
        HAVING array_agg(split ORDER BY split) =
               ARRAY['locked_test', 'train', 'validation']::text[]
    ) AS complete;

    IF actual_row_count <> expected_method_count * 3
       OR actual_method_count <> expected_method_count
       OR complete_method_count <> expected_method_count THEN
        RAISE EXCEPTION 'detector evaluation % has incomplete method summaries',
            target_evaluation_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN COALESCE(NEW, OLD);
END;
$$;

CREATE CONSTRAINT TRIGGER detector_evaluation_report_complete
AFTER INSERT OR UPDATE ON detector_evaluation_report
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION validate_detector_evaluation_summary();

CREATE CONSTRAINT TRIGGER detector_evaluation_method_complete
AFTER INSERT OR UPDATE OR DELETE ON detector_evaluation_method_summary
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION validate_detector_evaluation_summary();

GRANT SELECT, INSERT ON detector_evaluation_report, detector_evaluation_method_summary TO leo_analysis;
GRANT SELECT ON detector_evaluation_report, detector_evaluation_method_summary TO leo_dashboard;
REVOKE ALL ON detector_evaluation_report, detector_evaluation_method_summary FROM leo_capture;
REVOKE UPDATE, DELETE, TRUNCATE ON detector_evaluation_report, detector_evaluation_method_summary FROM leo_analysis, leo_dashboard;
REVOKE INSERT ON detector_evaluation_report, detector_evaluation_method_summary FROM leo_dashboard;
REVOKE ALL ON FUNCTION validate_detector_evaluation_summary() FROM PUBLIC;

COMMIT;
