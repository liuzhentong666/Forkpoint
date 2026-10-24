-- Rerunnable migration for existing installations (a fresh install runs it after
-- 009): vc.begin_change reports a branch that does not exist as P0002
-- (no_data_found) instead of PL/pgSQL's default P0001. Apply as the existing
-- database owner.
--
-- P0001 is also vc.merge's row and file conflict code, so a client could not tell
-- "this branch does not exist" from a conflict by SQLSTATE, and agent_lab.py let a
-- propose on such a branch end in a Python traceback. vc.merge_validated already
-- reports an unknown branch or source as P0002 (008). Nothing else changes: the
-- same signature, body, owner, grants, SECURITY DEFINER and search_path. The
-- definition in 001 is the old one and 010 takes precedence.
--
-- Rollback: as the owner, rerun vc.begin_change's definition from sql/001_core.sql
-- as CREATE OR REPLACE; no data depends on the error code.
BEGIN;
CREATE OR REPLACE FUNCTION vc.begin_change(p_branch text, p_expected uuid,
                               p_author text DEFAULT 'agent', p_message text DEFAULT '')
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,vc,pg_temp AS $$
DECLARE current_head uuid; result uuid := gen_random_uuid();
BEGIN
  IF EXISTS(SELECT 1 FROM vc.work_tx WHERE tx=txid_current()) THEN
    RAISE EXCEPTION 'A changeset is already open in this transaction';
  END IF;
  SELECT head INTO current_head FROM vc.branch WHERE name=p_branch FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Unknown branch' USING ERRCODE='P0002'; END IF;
  IF current_head IS DISTINCT FROM p_expected THEN
    RAISE EXCEPTION 'Stale head: expected %, actual %',p_expected,current_head
      USING ERRCODE='40001';
  END IF;
  INSERT INTO vc.work_tx(tx,revision,branch,base,author,message)
  VALUES(txid_current(),result,p_branch,current_head,p_author,p_message);
  PERFORM set_config('vc.read_revision',current_head::text,true);
  RETURN result;
END $$;
COMMIT;
