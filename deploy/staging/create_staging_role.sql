-- Staging login role for running the AgentDB suites without a superuser.
-- Run once as a superuser on the staging server (PostgreSQL 16+):
--   psql -X -v ON_ERROR_STOP=1 -d postgres -f deploy/staging/create_staging_role.sql
-- Then set the password interactively, never in this file or a command argument:
--   \password agentdb_staging
-- Rerunnable: existing roles keep their attributes; memberships are re-granted.
BEGIN;
DO $$
BEGIN
  -- install creates these only when missing, which needs CREATEROLE; pre-create
  -- them here with the same attributes as sql/002_demo_app.sql and 003_manager_role.sql.
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='agentdb_client') THEN
    CREATE ROLE agentdb_client NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='agentdb_manager') THEN
    CREATE ROLE agentdb_manager NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB
      NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  END IF;
  -- Same attributes as sql/011_agent_boundary.sql.
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='agentdb_worker') THEN
    CREATE ROLE agentdb_worker NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='agentdb_keeper') THEN
    CREATE ROLE agentdb_keeper NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='agentdb_broker') THEN
    CREATE ROLE agentdb_broker NOLOGIN NOINHERIT CREATEROLE NOSUPERUSER NOCREATEDB NOREPLICATION NOBYPASSRLS;
  END IF;
  -- CREATEDB: run_tests.py, review_local.py and scripts/check_merge_migration.py each
  -- create their own agentdb_test_* database and own everything in it.
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='agentdb_staging') THEN
    CREATE ROLE agentdb_staging LOGIN NOSUPERUSER NOCREATEROLE CREATEDB
      NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 40;
  END IF;
END $$;
-- SET ROLE only: the suites switch to these roles explicitly and must not
-- silently inherit their privileges.
GRANT agentdb_client, agentdb_manager, agentdb_keeper, agentdb_broker TO agentdb_staging WITH INHERIT FALSE, SET TRUE;
-- What sql/011_agent_boundary.sql skips when a non-superuser installs it, because only a superuser can do it.
-- Its per-database REVOKE of the large-object functions has to be run here too, in each database that takes
-- agent logins; vc.boundary_status reports it until then.
GRANT pg_signal_backend, pg_read_all_stats TO agentdb_keeper;
GRANT agentdb_worker TO agentdb_broker WITH ADMIN TRUE, INHERIT FALSE, SET FALSE;
GRANT SET ON PARAMETER temp_file_limit,log_statement TO agentdb_broker;
-- A vanished client's open transaction (and main's lock) ends after 60 s idle.
-- Set on this role only, so admin sessions keep PostgreSQL's default (off).
ALTER ROLE agentdb_staging SET idle_in_transaction_session_timeout = '60s';
COMMIT;
