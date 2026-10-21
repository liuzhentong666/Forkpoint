-- Read-set validation prototype (docs/READSET_EXPERIMENT.md). Experiment only:
-- run_readset.py installs this into its own agentdb_test_readset_* template as
-- the database owner; db.py install never does. vc.* is called, not changed.
--
-- xr.lock_main         takes p_branch's head row lock, as vc.begin_change does,
--                      and returns the time it was granted (instrumentation)
-- xr.merge_head        the head arms: the lock, then vc.merge with the caller's
--                      expected head, so the stale-head check (40001) is unchanged
-- xr.merge_validated   the read-set arms: the lock, then RS001 if any key in
--                      p_reads differs between the merge base and the locked head,
--                      else vc.merge with that head (write-write checks unchanged)
--
-- Both merges record the time validation ended in the transaction-local setting
-- xr.validated, which the caller reads back before COMMIT; an RS001 carries the
-- same time in its HINT, since the failed transaction's settings are gone.
BEGIN;
CREATE SCHEMA xr;
REVOKE ALL ON SCHEMA xr FROM PUBLIC;

CREATE FUNCTION xr.lock_main(p_branch text DEFAULT 'main') RETURNS timestamptz
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,vc,xr,pg_temp AS $$
BEGIN
  PERFORM 1 FROM vc.branch WHERE name=p_branch FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Unknown branch: %',p_branch; END IF;
  RETURN clock_timestamp();
END $$;

CREATE FUNCTION xr.merge_head(p_target text,p_source uuid,p_expected uuid,p_author text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,vc,xr,pg_temp AS $$
BEGIN
  PERFORM xr.lock_main(p_target);
  PERFORM set_config('xr.validated',extract(epoch FROM clock_timestamp())::text,true);
  RETURN vc.merge(p_target,p_source,p_expected,p_author);
END $$;

-- p_reads: {"<table_id>": [{"id": 1}, ...], ...}, primary keys the decision read
-- and did not write. Values are compared, so a row changed and changed back is
-- not a conflict: the decision depended only on the values it read.
CREATE FUNCTION xr.merge_validated(p_target text,p_source uuid,p_reads jsonb,p_author text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,vc,xr,pg_temp AS $$
DECLARE head uuid; base uuid; conflicts jsonb; validated text;
BEGIN
  IF jsonb_typeof(p_reads) IS DISTINCT FROM 'object' OR EXISTS(
       SELECT 1 FROM jsonb_each(p_reads) r WHERE jsonb_typeof(r.value)<>'array') THEN
    RAISE EXCEPTION 'p_reads must map table ids to arrays of primary keys';
  END IF;
  PERFORM xr.lock_main(p_target);
  head:=vc.head(p_target);
  base:=vc.merge_base(head,p_source);
  IF base<>head THEN
    -- One diff per table that has read keys; diff_rows replays both versions once.
    SELECT jsonb_agg(jsonb_build_object('table',r.key,'key',d.pk,'base',d.before_row,'now',d.after_row)
                     ORDER BY r.key,d.pk::text)
    INTO conflicts
    FROM jsonb_each(p_reads) r
    CROSS JOIN LATERAL vc.diff_rows(base,head,r.key::uuid) d
    WHERE jsonb_array_length(r.value)>0
      AND d.pk IN (SELECT e.value FROM jsonb_array_elements(r.value) e);
  END IF;
  validated:=extract(epoch FROM clock_timestamp())::text;
  PERFORM set_config('xr.validated',validated,true);
  IF conflicts IS NOT NULL THEN
    RAISE EXCEPTION 'Read conflict: % key(s) changed on % since %',jsonb_array_length(conflicts),p_target,base
      USING ERRCODE='RS001',DETAIL=conflicts::text,HINT='validated_at='||validated;
  END IF;
  RETURN vc.merge(p_target,p_source,head,p_author);
END $$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA xr FROM PUBLIC;
GRANT USAGE ON SCHEMA xr TO agentdb_client;
GRANT EXECUTE ON FUNCTION xr.lock_main(text), xr.merge_head(text,uuid,uuid,text),
  xr.merge_validated(text,uuid,jsonb,text) TO agentdb_client;
COMMIT;
