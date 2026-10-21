-- Additive, rerunnable migration. Apply as the existing database owner.
BEGIN;
DO $$ BEGIN
  IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='agentdb_manager') THEN
    CREATE ROLE agentdb_manager NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB
      NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  END IF;
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='agentdb_manager' AND
      (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls OR rolcanlogin))
    OR EXISTS(SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON m.member=r.oid
      WHERE r.rolname='agentdb_manager') THEN
    RAISE EXCEPTION 'Existing agentdb_manager has incompatible privileges; inspect it without silently altering the shared role';
  END IF;
END $$;

CREATE OR REPLACE FUNCTION vc.snapshot_status(p_revision uuid,p_table uuid) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,vc,pg_temp AS $$
  SELECT jsonb_build_object(
    'revision_exists',EXISTS(SELECT 1 FROM vc.changeset WHERE id=p_revision),
    'table_exists',EXISTS(SELECT 1 FROM vc.tracked_table WHERE id=p_table),
    'snapshot_exists',EXISTS(SELECT 1 FROM vc.snapshot_manifest
      WHERE changeset=p_revision AND table_id=p_table))
$$;
REVOKE ALL ON FUNCTION vc.snapshot_status(uuid,uuid) FROM PUBLIC;
GRANT USAGE ON SCHEMA vc TO agentdb_manager;
GRANT EXECUTE ON FUNCTION vc.snapshot_status(uuid,uuid),vc.materialize(uuid,uuid)
  TO agentdb_manager;
COMMIT;
