"""SQL for immutable hardware metadata publication."""

REGISTER_OBJECT_SQL = """
INSERT INTO object_blob
    (digest_algorithm, digest_value, byte_count, media_type, format_id, locator)
VALUES
    (%(digest_algorithm)s, %(digest_value)s, %(byte_count)s,
     %(media_type)s, %(format_id)s, %(locator)s)
ON CONFLICT DO NOTHING
"""

VERIFY_OBJECT_SQL = """
SELECT byte_count, media_type, format_id, locator FROM object_blob
WHERE digest_algorithm = %(digest_algorithm)s AND digest_value = %(digest_value)s
"""

PUBLISH_SNAPSHOT_SQL = """
INSERT INTO hardware_snapshot
    (snapshot_id, snapshot_digest_algorithm, snapshot_digest_value,
     bundle_digest_algorithm, bundle_digest_value, station_id,
     radio_count, chain_count, idempotency_key)
VALUES
    (%(snapshot_id)s, %(snapshot_digest_algorithm)s, %(snapshot_digest_value)s,
     %(bundle_digest_algorithm)s, %(bundle_digest_value)s, %(station_id)s,
     %(radio_count)s, %(chain_count)s, %(idempotency_key)s)
ON CONFLICT DO NOTHING
RETURNING snapshot_id
"""

PUBLISH_RADIO_SQL = """
INSERT INTO hardware_radio (snapshot_id, radio_index, radio_id)
VALUES (%(snapshot_id)s, %(radio_index)s, %(radio_id)s)
"""

PUBLISH_CHAIN_SQL = """
INSERT INTO hardware_receiver_chain
    (snapshot_id, chain_index, receiver_chain_id, radio_id, radio_channel,
     lnb_id, polarization, cable_id, valid_from_utc_ns, valid_until_utc_ns)
VALUES
    (%(snapshot_id)s, %(chain_index)s, %(receiver_chain_id)s, %(radio_id)s,
     %(radio_channel)s, %(lnb_id)s, %(polarization)s, %(cable_id)s,
     %(valid_from_utc_ns)s, %(valid_until_utc_ns)s)
"""

SNAPSHOT_SELECT = """
SELECT h.*, b.byte_count AS bundle_byte_count,
       b.media_type AS bundle_media_type, b.format_id AS bundle_format_id,
       b.locator AS bundle_locator
FROM hardware_snapshot h
JOIN object_blob b ON (b.digest_algorithm, b.digest_value) =
    (h.bundle_digest_algorithm, h.bundle_digest_value)
"""

GET_EXACT_SQL = (
    SNAPSHOT_SELECT
    + """
WHERE h.snapshot_id = %(snapshot_id)s
  AND h.snapshot_digest_algorithm = %(snapshot_digest_algorithm)s
  AND h.snapshot_digest_value = %(snapshot_digest_value)s
"""
)

GET_CONFLICTS_SQL = (
    SNAPSHOT_SELECT
    + """
WHERE h.snapshot_id = %(snapshot_id)s
   OR h.idempotency_key = %(idempotency_key)s
   OR (h.snapshot_digest_algorithm, h.snapshot_digest_value) =
      (%(snapshot_digest_algorithm)s, %(snapshot_digest_value)s)
FOR UPDATE OF h
"""
)

GET_CHAINS_SQL = """
SELECT * FROM hardware_receiver_chain
WHERE snapshot_id = %(snapshot_id)s ORDER BY chain_index
"""

GET_RADIOS_SQL = """
SELECT radio_id FROM hardware_radio
WHERE snapshot_id = %(snapshot_id)s ORDER BY radio_index
"""
