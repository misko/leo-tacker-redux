BEGIN;

-- The analysis deployment verifies the immutable migration receipt inventory
-- before assuming its capability role.  Grant that read-only evidence to the
-- capability role so an inheriting, non-elevated analysis login can fail closed
-- on an unexpected schema without receiving any catalog mutation privilege.
GRANT SELECT ON TABLE public.schema_migration TO leo_analysis;

COMMIT;
