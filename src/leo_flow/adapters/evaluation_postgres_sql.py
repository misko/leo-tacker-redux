"""PostgreSQL statements for detector evaluation publication."""

REGISTER_OBJECT_SQL = """
SELECT register_live_object_blob(
    %(report_digest_algorithm)s, %(report_digest_value)s, %(report_byte_count)s,
     %(report_media_type)s, %(report_format_id)s, %(report_locator)s)
"""

VERIFY_OBJECT_SQL = """
SELECT byte_count, media_type, format_id, locator FROM object_blob
WHERE digest_algorithm = %(report_digest_algorithm)s
  AND digest_value = %(report_digest_value)s
  AND lifecycle_state = 'live'
"""

VERIFY_DATASET_SQL = """
SELECT snapshot_id FROM dataset_snapshot
WHERE snapshot_id = %(dataset_snapshot_id)s
  AND snapshot_digest_algorithm = %(dataset_snapshot_digest_algorithm)s
  AND snapshot_digest_value = %(dataset_snapshot_digest_value)s
  AND feature_membership_digest_algorithm = %(membership_digest_algorithm)s
  AND feature_membership_digest_value = %(membership_digest_value)s
"""

PUBLISH_REPORT_SQL = """
INSERT INTO detector_evaluation_report
    (evaluation_id, run_id, dataset_snapshot_id,
     dataset_snapshot_digest_algorithm, dataset_snapshot_digest_value,
     feature_membership_digest_algorithm, feature_membership_digest_value,
     threshold_rule_id, threshold_rule_digest_algorithm, threshold_rule_digest_value,
     calibration_dataset_id, calibration_split,
     report_digest_algorithm, report_digest_value,
     method_count, union_window_count, warnings, idempotency_key)
VALUES
    (%(evaluation_id)s, %(run_id)s, %(dataset_snapshot_id)s,
     %(dataset_snapshot_digest_algorithm)s, %(dataset_snapshot_digest_value)s,
     %(membership_digest_algorithm)s, %(membership_digest_value)s,
     %(threshold_rule_id)s, %(threshold_rule_digest_algorithm)s, %(threshold_rule_digest_value)s,
     %(calibration_dataset_id)s, %(calibration_split)s,
     %(report_digest_algorithm)s, %(report_digest_value)s,
     %(method_count)s, %(union_window_count)s, %(warnings)s::jsonb, %(idempotency_key)s)
ON CONFLICT DO NOTHING RETURNING evaluation_id
"""

PUBLISH_METHOD_SQL = """
INSERT INTO detector_evaluation_method_summary
    (evaluation_id, method_id, split, threshold, score_semantics,
     feature_set_count, feature_set_present_count, union_window_count,
     present_window_count, missing_window_count, firing_count,
     true_positive, false_positive, true_negative, false_negative,
     scored_prediction_count, missing_prediction_count)
VALUES
    (%(evaluation_id)s, %(method_id)s, %(split)s, %(threshold)s, %(score_semantics)s,
     %(feature_set_count)s, %(feature_set_present_count)s, %(split_union_window_count)s,
     %(present_window_count)s, %(missing_window_count)s, %(firing_count)s,
     %(true_positive)s, %(false_positive)s, %(true_negative)s, %(false_negative)s,
     %(scored_prediction_count)s, %(missing_prediction_count)s)
"""

REPORT_SELECT = """
SELECT er.*, ob.byte_count AS report_byte_count,
       ob.media_type AS report_media_type, ob.format_id AS report_format_id,
       ob.locator AS report_locator
FROM detector_evaluation_report er
JOIN object_blob ob ON (ob.digest_algorithm, ob.digest_value) =
    (er.report_digest_algorithm, er.report_digest_value)
"""

GET_CONFLICTS_SQL = (
    REPORT_SELECT
    + """
WHERE er.evaluation_id = %(evaluation_id)s OR er.run_id = %(run_id)s
   OR (er.report_digest_algorithm, er.report_digest_value) =
      (%(report_digest_algorithm)s, %(report_digest_value)s)
   OR er.idempotency_key = %(idempotency_key)s
"""
)

GET_EXACT_SQL = (
    REPORT_SELECT
    + """
WHERE er.evaluation_id = %(evaluation_id)s AND er.run_id = %(run_id)s
  AND er.report_digest_algorithm = %(report_digest_algorithm)s
  AND er.report_digest_value = %(report_digest_value)s
"""
)

GET_METHODS_SQL = """
SELECT * FROM detector_evaluation_method_summary
WHERE evaluation_id = %(evaluation_id)s
ORDER BY method_id, CASE split WHEN 'train' THEN 0 WHEN 'validation' THEN 1 ELSE 2 END
"""
