BEGIN;

CREATE TABLE object_orphan_observation (
    digest_algorithm text NOT NULL,
    digest_value text NOT NULL,
    byte_count bigint NOT NULL CHECK (byte_count >= 0),
    locator text NOT NULL,
    filesystem_device bigint NOT NULL,
    filesystem_inode bigint NOT NULL,
    filesystem_parent_device bigint NOT NULL,
    filesystem_parent_inode bigint NOT NULL,
    filesystem_mtime_ns bigint NOT NULL CHECK (filesystem_mtime_ns >= 0),
    first_observed_at timestamptz NOT NULL,
    last_observed_at timestamptz NOT NULL,
    state text NOT NULL CHECK (
        state IN ('observed', 'claimed', 'deleted', 'registered')
    ),
    claim_token text,
    claimed_at timestamptz,
    deleted_at timestamptz,
    registered_at timestamptz,
    PRIMARY KEY (digest_algorithm, digest_value),
    CONSTRAINT object_orphan_claim_consistent CHECK (
        (state = 'claimed') = (claim_token IS NOT NULL AND claimed_at IS NOT NULL)
    ),
    CONSTRAINT object_orphan_deleted_consistent CHECK (
        (state = 'deleted') = (deleted_at IS NOT NULL)
    ),
    CONSTRAINT object_orphan_registered_consistent CHECK (
        (state = 'registered') = (registered_at IS NOT NULL)
    )
);

CREATE TABLE object_orphan_event (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    digest_algorithm text NOT NULL,
    digest_value text NOT NULL,
    event text NOT NULL CHECK (
        event IN ('observed', 'evidence_changed', 'claimed',
                  'delete_failed', 'deleted', 'registered')
    ),
    event_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    claim_token text,
    detail text,
    UNIQUE (digest_algorithm, digest_value, event, claim_token)
);

-- NULL claim tokens intentionally allow multiple evidence_changed/registered
-- events across distinct physical incarnations. Claim-scoped events carry a
-- non-null stable token and are idempotent under the UNIQUE constraint.

-- The advisory key is only a serialization mechanism. A hash collision merely
-- serializes unrelated objects; it cannot conflate their catalog identities.
CREATE FUNCTION object_digest_fence(p_digest_algorithm text, p_digest_value text)
RETURNS void
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
    SELECT pg_advisory_xact_lock(
        hashtextextended(p_digest_algorithm || ':' || p_digest_value, 0)
    );
$function$;

CREATE FUNCTION observe_unregistered_object(
    p_digest_algorithm text,
    p_digest_value text,
    p_byte_count bigint,
    p_locator text,
    p_filesystem_device bigint,
    p_filesystem_inode bigint,
    p_filesystem_parent_device bigint,
    p_filesystem_parent_inode bigint,
    p_filesystem_mtime_ns bigint
) RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    blob_state text;
    existing object_orphan_observation%ROWTYPE;
    observed_at timestamptz := clock_timestamp();
    evidence_matches boolean;
BEGIN
    IF p_digest_algorithm <> 'sha256'
       OR p_digest_value !~ '^[0-9a-f]{64}$'
       OR p_byte_count < 0
       OR p_locator <> 'cas:sha256:' || p_digest_value
       OR p_filesystem_device < 0
       OR p_filesystem_inode < 0
       OR p_filesystem_parent_device < 0
       OR p_filesystem_parent_inode < 0
       OR p_filesystem_mtime_ns < 0 THEN
        RAISE EXCEPTION 'invalid orphan inventory evidence';
    END IF;
    PERFORM object_digest_fence(p_digest_algorithm, p_digest_value);

    SELECT lifecycle_state INTO blob_state
      FROM object_blob
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value;
    IF FOUND THEN
        RETURN CASE blob_state
            WHEN 'live' THEN 'live'
            WHEN 'gc_deleted' THEN 'tombstone'
            WHEN 'gc_claimed' THEN 'in_flight'
            ELSE 'registered'
        END;
    END IF;

    SELECT * INTO existing
      FROM object_orphan_observation
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value
     FOR UPDATE;
    IF NOT FOUND THEN
        INSERT INTO object_orphan_observation
            (digest_algorithm, digest_value, byte_count, locator,
             filesystem_device, filesystem_inode,
             filesystem_parent_device, filesystem_parent_inode,
             filesystem_mtime_ns,
             first_observed_at, last_observed_at, state)
        VALUES
            (p_digest_algorithm, p_digest_value, p_byte_count, p_locator,
             p_filesystem_device, p_filesystem_inode,
             p_filesystem_parent_device, p_filesystem_parent_inode,
             p_filesystem_mtime_ns,
             observed_at, observed_at, 'observed');
        INSERT INTO object_orphan_event
            (digest_algorithm, digest_value, event, event_at)
        VALUES (p_digest_algorithm, p_digest_value, 'observed', observed_at);
        RETURN 'unregistered';
    END IF;

    IF existing.state = 'claimed' THEN
        RETURN 'in_flight';
    END IF;
    evidence_matches := existing.byte_count = p_byte_count
        AND existing.locator = p_locator
        AND existing.filesystem_device = p_filesystem_device
        AND existing.filesystem_inode = p_filesystem_inode
        AND existing.filesystem_parent_device = p_filesystem_parent_device
        AND existing.filesystem_parent_inode = p_filesystem_parent_inode
        AND existing.filesystem_mtime_ns = p_filesystem_mtime_ns;
    IF evidence_matches AND existing.state = 'observed' THEN
        UPDATE object_orphan_observation
           SET last_observed_at = observed_at
         WHERE digest_algorithm = p_digest_algorithm
           AND digest_value = p_digest_value;
        RETURN 'unregistered';
    END IF;

    UPDATE object_orphan_observation
       SET byte_count = p_byte_count,
           locator = p_locator,
           filesystem_device = p_filesystem_device,
           filesystem_inode = p_filesystem_inode,
           filesystem_parent_device = p_filesystem_parent_device,
           filesystem_parent_inode = p_filesystem_parent_inode,
           filesystem_mtime_ns = p_filesystem_mtime_ns,
           first_observed_at = observed_at,
           last_observed_at = observed_at,
           state = 'observed',
           claim_token = NULL,
           claimed_at = NULL,
           deleted_at = NULL,
           registered_at = NULL
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value;
    INSERT INTO object_orphan_event
        (digest_algorithm, digest_value, event, event_at)
    VALUES (p_digest_algorithm, p_digest_value, 'evidence_changed', observed_at);
    RETURN 'unregistered';
END
$function$;

CREATE FUNCTION claim_unregistered_object(
    p_digest_algorithm text,
    p_digest_value text,
    p_byte_count bigint,
    p_locator text,
    p_filesystem_device bigint,
    p_filesystem_inode bigint,
    p_filesystem_parent_device bigint,
    p_filesystem_parent_inode bigint,
    p_filesystem_mtime_ns bigint,
    p_claim_token text,
    p_minimum_age_seconds bigint
) RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    existing object_orphan_observation%ROWTYPE;
    v_claimed_at timestamptz := clock_timestamp();
BEGIN
    IF p_claim_token IS NULL OR p_claim_token = ''
       OR p_minimum_age_seconds <= 0 THEN
        RAISE EXCEPTION 'invalid orphan claim';
    END IF;
    PERFORM object_digest_fence(p_digest_algorithm, p_digest_value);
    IF EXISTS (
        SELECT 1 FROM object_blob
         WHERE digest_algorithm = p_digest_algorithm
           AND digest_value = p_digest_value
    ) THEN
        RETURN NULL;
    END IF;
    SELECT * INTO existing
      FROM object_orphan_observation
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value
     FOR UPDATE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    IF existing.state = 'claimed' THEN
        IF existing.byte_count = p_byte_count
           AND existing.locator = p_locator
           AND existing.filesystem_device = p_filesystem_device
           AND existing.filesystem_inode = p_filesystem_inode
           AND existing.filesystem_parent_device = p_filesystem_parent_device
           AND existing.filesystem_parent_inode = p_filesystem_parent_inode
           AND existing.filesystem_mtime_ns = p_filesystem_mtime_ns THEN
            RETURN existing.claim_token;
        END IF;
        RETURN NULL;
    END IF;
    IF existing.state <> 'observed'
       OR existing.byte_count <> p_byte_count
       OR existing.locator <> p_locator
       OR existing.filesystem_device <> p_filesystem_device
       OR existing.filesystem_inode <> p_filesystem_inode
       OR existing.filesystem_parent_device <> p_filesystem_parent_device
       OR existing.filesystem_parent_inode <> p_filesystem_parent_inode
       OR existing.filesystem_mtime_ns <> p_filesystem_mtime_ns
       OR existing.first_observed_at
            + make_interval(secs => p_minimum_age_seconds) > v_claimed_at THEN
        RETURN NULL;
    END IF;
    UPDATE object_orphan_observation
       SET state = 'claimed', claim_token = p_claim_token,
           claimed_at = v_claimed_at
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value;
    INSERT INTO object_orphan_event
        (digest_algorithm, digest_value, event, event_at, claim_token)
    VALUES (p_digest_algorithm, p_digest_value, 'claimed',
            v_claimed_at, p_claim_token);
    RETURN p_claim_token;
END
$function$;

-- Callers hold the transaction and advisory lock returned by this function's
-- SELECT until the filesystem unlink and completion UPDATE have committed.
CREATE FUNCTION orphan_claim_is_current(
    p_digest_algorithm text,
    p_digest_value text,
    p_byte_count bigint,
    p_locator text,
    p_filesystem_device bigint,
    p_filesystem_inode bigint,
    p_filesystem_parent_device bigint,
    p_filesystem_parent_inode bigint,
    p_filesystem_mtime_ns bigint,
    p_claim_token text
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    PERFORM object_digest_fence(p_digest_algorithm, p_digest_value);
    RETURN NOT EXISTS (
        SELECT 1 FROM object_blob
         WHERE digest_algorithm = p_digest_algorithm
           AND digest_value = p_digest_value
    ) AND EXISTS (
        SELECT 1 FROM object_orphan_observation
         WHERE digest_algorithm = p_digest_algorithm
           AND digest_value = p_digest_value
           AND state = 'claimed'
           AND claim_token = p_claim_token
           AND byte_count = p_byte_count
           AND locator = p_locator
           AND filesystem_device = p_filesystem_device
           AND filesystem_inode = p_filesystem_inode
           AND filesystem_parent_device = p_filesystem_parent_device
           AND filesystem_parent_inode = p_filesystem_parent_inode
           AND filesystem_mtime_ns = p_filesystem_mtime_ns
         FOR UPDATE
    );
END
$function$;

CREATE FUNCTION complete_unregistered_object_delete(
    p_digest_algorithm text,
    p_digest_value text,
    p_claim_token text
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE completed_at timestamptz := clock_timestamp();
BEGIN
    -- The caller must already hold this transaction's digest fence, but taking
    -- it again is harmless and makes direct calls safe.
    PERFORM object_digest_fence(p_digest_algorithm, p_digest_value);
    UPDATE object_orphan_observation
       SET state = 'deleted', claim_token = NULL, claimed_at = NULL,
           deleted_at = completed_at
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value
       AND state = 'claimed' AND claim_token = p_claim_token;
    IF NOT FOUND THEN RETURN false; END IF;
    INSERT INTO object_orphan_event
        (digest_algorithm, digest_value, event, event_at, claim_token)
    VALUES (p_digest_algorithm, p_digest_value, 'deleted', completed_at, p_claim_token);
    RETURN true;
END
$function$;

CREATE FUNCTION record_unregistered_object_delete_failure(
    p_digest_algorithm text,
    p_digest_value text,
    p_claim_token text,
    p_detail text
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    IF p_detail IS NULL OR p_detail = '' OR length(p_detail) > 1000 THEN
        RAISE EXCEPTION 'invalid orphan delete failure detail';
    END IF;
    PERFORM object_digest_fence(p_digest_algorithm, p_digest_value);
    IF NOT EXISTS (
        SELECT 1 FROM object_orphan_observation
         WHERE digest_algorithm = p_digest_algorithm
           AND digest_value = p_digest_value
           AND state = 'claimed' AND claim_token = p_claim_token
         FOR UPDATE
    ) THEN RETURN false; END IF;
    INSERT INTO object_orphan_event
        (digest_algorithm, digest_value, event, claim_token, detail)
    VALUES (p_digest_algorithm, p_digest_value, 'delete_failed',
            p_claim_token, p_detail)
    ON CONFLICT (digest_algorithm, digest_value, event, claim_token)
    DO NOTHING;
    -- An ambiguous external failure remains claimed. This intentionally blocks
    -- registration until an idempotent maintenance retry proves deletion.
    RETURN true;
END
$function$;

-- Replace the publication function so registration and orphan deletion use
-- the same digest fence. Registration never proceeds through an unresolved
-- orphan claim, even if a maintenance process has crashed.
CREATE OR REPLACE FUNCTION register_live_object_blob(
    p_digest_algorithm text,
    p_digest_value text,
    p_byte_count bigint,
    p_media_type text,
    p_format_id text,
    p_locator text
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    v_registered_at timestamptz := clock_timestamp();
    registered object_blob%ROWTYPE;
BEGIN
    PERFORM object_digest_fence(p_digest_algorithm, p_digest_value);
    IF EXISTS (
        SELECT 1 FROM object_orphan_observation
         WHERE digest_algorithm = p_digest_algorithm
           AND digest_value = p_digest_value
           AND state = 'claimed'
    ) THEN
        RAISE EXCEPTION 'object has an active orphan-deletion claim'
            USING ERRCODE = '40001';
    END IF;

    INSERT INTO object_blob
        (digest_algorithm, digest_value, byte_count, media_type, format_id, locator)
    VALUES
        (p_digest_algorithm, p_digest_value, p_byte_count,
         p_media_type, p_format_id, p_locator)
    ON CONFLICT DO NOTHING;

    UPDATE object_blob
       SET lifecycle_state = 'live',
           gc_deleted_at = NULL,
           gc_last_error = NULL,
           verified_at = NULL,
           registered_at = v_registered_at
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value
       AND lifecycle_state = 'gc_deleted'
       AND byte_count = p_byte_count
       AND media_type = p_media_type
       AND format_id = p_format_id
       AND locator = p_locator;

    SELECT * INTO registered
      FROM object_blob
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value
     FOR KEY SHARE;
    IF NOT FOUND
       OR registered.lifecycle_state <> 'live'
       OR registered.byte_count <> p_byte_count
       OR registered.media_type <> p_media_type
       OR registered.format_id <> p_format_id
       OR registered.locator <> p_locator THEN
        -- Preserve the existing publisher contract: adapters perform their
        -- exact verification query and translate a mismatch into their typed
        -- collision error. Most importantly, do not falsely mark an orphan
        -- observation registered when immutable identity conflicts.
        RETURN;
    END IF;

    UPDATE object_orphan_observation
       SET state = 'registered', claim_token = NULL, claimed_at = NULL,
           deleted_at = NULL, registered_at = v_registered_at
     WHERE digest_algorithm = p_digest_algorithm
       AND digest_value = p_digest_value
       AND state <> 'registered';
    IF FOUND THEN
        INSERT INTO object_orphan_event
            (digest_algorithm, digest_value, event, event_at)
        VALUES (p_digest_algorithm, p_digest_value, 'registered', v_registered_at);
    END IF;
END
$function$;

REVOKE ALL ON object_orphan_observation, object_orphan_event
    FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard;
-- Every ordinary publisher already uses register_live_object_blob. Removing
-- legacy direct INSERT closes the only path that could bypass the digest fence.
REVOKE INSERT ON object_blob FROM leo_capture, leo_analysis;
REVOKE ALL ON FUNCTION object_digest_fence(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION observe_unregistered_object(text, text, bigint, text, bigint, bigint, bigint, bigint, bigint)
    FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard;
REVOKE ALL ON FUNCTION claim_unregistered_object(text, text, bigint, text, bigint, bigint, bigint, bigint, bigint, text, bigint)
    FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard;
REVOKE ALL ON FUNCTION orphan_claim_is_current(text, text, bigint, text, bigint, bigint, bigint, bigint, bigint, text)
    FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard;
REVOKE ALL ON FUNCTION complete_unregistered_object_delete(text, text, text)
    FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard;
REVOKE ALL ON FUNCTION record_unregistered_object_delete_failure(text, text, text, text)
    FROM PUBLIC, leo_capture, leo_analysis, leo_dashboard;

GRANT SELECT ON object_orphan_observation, object_orphan_event TO leo_maintenance;
GRANT USAGE, SELECT ON SEQUENCE object_orphan_event_event_id_seq TO leo_maintenance;
GRANT EXECUTE ON FUNCTION observe_unregistered_object(text, text, bigint, text, bigint, bigint, bigint, bigint, bigint),
    claim_unregistered_object(text, text, bigint, text, bigint, bigint, bigint, bigint, bigint, text, bigint),
    orphan_claim_is_current(text, text, bigint, text, bigint, bigint, bigint, bigint, bigint, text),
    complete_unregistered_object_delete(text, text, text),
    record_unregistered_object_delete_failure(text, text, text, text)
TO leo_maintenance;

COMMIT;
