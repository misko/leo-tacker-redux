BEGIN;

-- The 0044 SECURITY DEFINER routines are owned by the non-login
-- leo_routine_owner role.  They close candidate/admission identity over these
-- exact recording columns, so the owner needs column-scoped read authority.
-- Callers still have no direct table access and only receive the bounded
-- routine results granted in 0044.
GRANT SELECT (
    recording_id,
    data_digest_algorithm,
    data_digest_value,
    metadata_digest_algorithm,
    metadata_digest_value,
    manifest_digest_value,
    published_at
) ON public.recording TO leo_routine_owner;

COMMIT;
