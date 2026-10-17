-- Rerunnable migration for existing installations: vc.merge resolves the target
-- state of all changed rows of a table with one vc.rows_at() call instead of one
-- per changed row. Same signature, conflict rules and history; must stay identical
-- to vc.merge in 001_core.sql. Existing grants are kept by CREATE OR REPLACE.
-- Apply as the existing database owner.
BEGIN;
CREATE OR REPLACE FUNCTION vc.merge(p_target text,p_source uuid,p_expected uuid,p_author text DEFAULT 'agent')
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
DECLARE base_id uuid; ctx vc.work_tx; t record; d record;
        file_path text; old_file jsonb; source_file jsonb; target_file jsonb;
BEGIN
  PERFORM vc.begin_change(p_target,p_expected,p_author,'merge '||p_source::text);
  ctx:=vc._context();
  IF NOT EXISTS(SELECT 1 FROM vc.changeset WHERE id=p_source) THEN RAISE EXCEPTION 'Unknown source'; END IF;
  IF EXISTS(SELECT 1 FROM vc._ancestors(ctx.base) WHERE id=p_source) THEN
    DELETE FROM vc.work_tx WHERE tx=ctx.tx;
    RETURN ctx.base;
  END IF;
  base_id:=vc.merge_base(ctx.base,p_source);
  UPDATE vc.work_tx SET extra_parent=p_source WHERE tx=ctx.tx;
  FOR t IN SELECT id FROM vc.tracked_table LOOP
    -- Resolve the target state of all changed rows with one rows_at() call, not one
    -- per row; it is skipped when nothing changed, and rows keep diff_rows' order.
    FOR d IN WITH diff AS MATERIALIZED (
               SELECT x.pk,x.before_row,x.after_row,x.n
               FROM vc.diff_rows(base_id,p_source,t.id) WITH ORDINALITY x(pk,before_row,after_row,n)
             ), target AS MATERIALIZED (
               SELECT r.pk,r.doc FROM vc.rows_at(ctx.base,t.id) r WHERE EXISTS(SELECT 1 FROM diff))
             SELECT diff.pk,diff.before_row,diff.after_row,target.doc AS ours
             FROM diff LEFT JOIN target ON target.pk=diff.pk ORDER BY diff.n LOOP
      IF d.ours IS NOT DISTINCT FROM d.after_row THEN CONTINUE; END IF;
      IF d.ours IS DISTINCT FROM d.before_row THEN
        RAISE EXCEPTION 'Row conflict: table %, key %',t.id,d.pk USING ERRCODE='P0001';
      END IF;
      IF p_target='main' THEN PERFORM vc._apply_main(t.id,d.pk,d.after_row);
      ELSE PERFORM vc._stage(t.id,d.pk,d.ours,d.after_row); END IF;
    END LOOP;
  END LOOP;
  FOR file_path IN SELECT DISTINCT path FROM vc.path_change LOOP
    old_file:=vc._file_state(base_id,file_path);
    source_file:=vc._file_state(p_source,file_path);
    IF old_file IS NOT DISTINCT FROM source_file THEN CONTINUE; END IF;
    target_file:=vc._file_state(ctx.base,file_path);
    IF target_file IS NOT DISTINCT FROM source_file THEN CONTINUE; END IF;
    IF target_file IS DISTINCT FROM old_file THEN
      RAISE EXCEPTION 'File conflict: %',file_path USING ERRCODE='P0001';
    END IF;
    INSERT INTO vc.pending_path VALUES(ctx.tx,file_path,target_file,source_file);
  END LOOP;
  RETURN vc.finish_change();
END $$;
REVOKE ALL ON FUNCTION vc.merge(text,uuid,uuid,text) FROM PUBLIC;
COMMIT;
