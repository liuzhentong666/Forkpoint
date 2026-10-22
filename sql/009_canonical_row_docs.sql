-- Rerunnable migration for existing installations (a fresh install runs it after
-- 008): history's row documents and primary keys no longer depend on the session
-- that wrote them. Apply as the existing database owner.
--
-- History stores each row as to_jsonb(row) and takes the primary key from that
-- document, but to_jsonb renders some types by session settings: timestamptz by
-- TimeZone (the same instant is "...T08:00:00+08:00" in Asia/Shanghai and
-- "...T00:00:00+00:00" in UTC), interval by IntervalStyle, float by
-- extra_float_digits, bytea by bytea_output. A row inserted in one time zone and
-- deleted in another got two keys, and history kept the deleted row. The capture
-- triggers, and vc.merge_validated, which rebuilds read keys the same way, now
-- render with fixed values (UTC, and the defaults for the others); their
-- definitions in 001 and 008 carry the same settings. money still renders by
-- lc_monetary and is not supported in tracked tables.
--
-- Rows recorded earlier under other settings would no longer match what new
-- writes record, so every stored row document is first rendered again with the
-- fixed settings; if any differs, the migration stops and changes nothing, and
-- that history has to be repaired first. The check runs under a lock that waits
-- for open versioned transactions and keeps new ones out until this commits, so
-- no row rendered under the old settings can land after it.
BEGIN;
SET LOCAL TimeZone='UTC';
SET LOCAL IntervalStyle='postgres';
SET LOCAL extra_float_digits=1;
SET LOCAL bytea_output='hex';
-- Captures stage rows in vc.pending_row and finish_change moves them to
-- vc.row_change; materialize writes vc.snapshot_row.
LOCK TABLE vc.pending_row, vc.row_change, vc.snapshot_row IN SHARE MODE;
DO $canonical$
DECLARE t record; differing bigint; total bigint:=0; f regprocedure;
BEGIN
  FOR t IN SELECT id,relation FROM vc.tracked_table ORDER BY relation::text LOOP
    EXECUTE format($q$SELECT
      (SELECT count(*) FROM vc.row_change r WHERE r.table_id=$1 AND (
         (r.after_row IS NOT NULL AND to_jsonb(jsonb_populate_record(NULL::%1$s,r.after_row)) IS DISTINCT FROM r.after_row) OR
         (r.before_row IS NOT NULL AND to_jsonb(jsonb_populate_record(NULL::%1$s,r.before_row)) IS DISTINCT FROM r.before_row)))
      +(SELECT count(*) FROM vc.snapshot_row s WHERE s.table_id=$1
         AND to_jsonb(jsonb_populate_record(NULL::%1$s,s.doc)) IS DISTINCT FROM s.doc)$q$, t.relation)
      INTO differing USING t.id;
    total:=total+differing;
  END LOOP;
  IF total>0 THEN
    RAISE EXCEPTION '% stored row document(s) differ from their session-independent form; repair that history first, nothing was changed', total;
  END IF;
  FOR f IN SELECT p.oid::regprocedure FROM pg_catalog.pg_proc p
           WHERE p.oid IN (to_regprocedure('vc._capture_main()'),to_regprocedure('vc._overlay_write()'),
                           to_regprocedure('vc.merge_validated(text,uuid,jsonb,text)')) ORDER BY 1 LOOP
    EXECUTE format('ALTER FUNCTION %s SET TimeZone=%L SET IntervalStyle=%L SET extra_float_digits=1 SET bytea_output=%L',
                   f,'UTC','postgres','hex');
  END LOOP;
END $canonical$;
COMMIT;
