BEGIN;

DO $roles$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'leo_capture') THEN
        CREATE ROLE leo_capture NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'leo_analysis') THEN
        CREATE ROLE leo_analysis NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'leo_dashboard') THEN
        CREATE ROLE leo_dashboard NOLOGIN;
    END IF;
END
$roles$;

GRANT USAGE ON SCHEMA public TO leo_capture, leo_analysis, leo_dashboard;

GRANT SELECT, INSERT ON object_blob, recording TO leo_capture;
GRANT SELECT ON object_blob, recording TO leo_analysis, leo_dashboard;

GRANT SELECT, INSERT, UPDATE ON job TO leo_analysis;
REVOKE ALL ON job FROM leo_capture, leo_dashboard;
REVOKE UPDATE, DELETE, TRUNCATE ON object_blob, recording FROM leo_capture;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON object_blob, recording FROM leo_dashboard;

COMMIT;
