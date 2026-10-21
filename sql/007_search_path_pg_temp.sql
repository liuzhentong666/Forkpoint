-- Rerunnable migration for existing installations (and the last step of a fresh
-- install): every function in vc that sets a search_path searches pg_temp last.
-- Apply as the existing database owner; ALTER FUNCTION keeps owner, grants and body.
--
-- These functions run with the owner's rights and were created with
-- search_path=pg_catalog,vc. A temporary schema left out of search_path is
-- searched first for relation names, agentdb_client may create temporary
-- tables, and vc.finish_change and vc._apply_main name pg_constraint,
-- pg_namespace and pg_attribute without a schema. Listing pg_temp last is the
-- fix the PostgreSQL manual gives under "Writing SECURITY DEFINER Functions
-- Safely". scripts/check_agent_boundaries.py checks it on a fresh install.
--
-- The definitions in 001-006 now list pg_temp last themselves, so rerunning
-- those migrations keeps it; this file upgrades installations made before, and
-- also covers the two SECURITY INVOKER functions that set a search_path
-- (vc.register_table, vc._immutable). A function with any other search_path
-- stops the migration before anything changes.
BEGIN;
DO $harden$
DECLARE f regprocedure;
BEGIN
  IF EXISTS(SELECT 1 FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='vc' AND p.proconfig IS NOT NULL AND p.proconfig IS DISTINCT FROM '{"search_path=pg_catalog, vc"}'
              AND p.proconfig IS DISTINCT FROM '{"search_path=pg_catalog, vc, pg_temp"}') THEN
    RAISE EXCEPTION 'A function in vc has an unexpected search_path; nothing was changed';
  END IF;
  FOR f IN SELECT p.oid::regprocedure FROM pg_catalog.pg_proc p
           JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
           WHERE n.nspname='vc' AND p.proconfig IS NOT NULL ORDER BY 1 LOOP
    EXECUTE format('ALTER FUNCTION %s SET search_path = pg_catalog, vc, pg_temp', f);
  END LOOP;
END $harden$;
COMMIT;
