-- Rerunnable migration for existing installations (and the last step of a fresh
-- install): history walks whose cost no longer grows with the whole history.
-- Same signatures, results, errors and grants; CREATE OR REPLACE keeps grants.
-- Apply as the existing database owner.
--
-- * Every first-parent / ancestor walk looks up the next parent through a LATERAL
--   index probe (OFFSET 0 keeps it from being flattened). The planner otherwise
--   hash-joined the recursive step against a full scan of vc.parent on every
--   step, which made one walk quadratic in history length.
-- * vc._file_state stops at the nearest first-parent ancestor that recorded the
--   path (it used to walk to genesis and then pick the nearest), and skips the
--   walk entirely for a path that no changeset ever recorded.
-- * vc.merge walks the target's ancestors once for the already-merged test and
--   the merge base, and checks only paths that can differ between the merge base
--   and the source instead of every path ever recorded.
-- * vc.snapshot_due names the (revision, table) snapshots a maintenance executor
--   should materialize so that reading a branch head replays a bounded number of
--   commits. Snapshots stay derived data (vc.materialize, unchanged).
BEGIN;
CREATE OR REPLACE FUNCTION vc._ancestors(p_start uuid) RETURNS TABLE(id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
  WITH RECURSIVE a(id) AS (
    SELECT p_start UNION SELECT p.parent FROM a CROSS JOIN LATERAL (
      SELECT p.parent FROM vc.parent p WHERE p.child=a.id OFFSET 0) p
  ) SELECT a.id FROM a
$$;

CREATE OR REPLACE FUNCTION vc.rows_at(p_revision uuid,p_table uuid)
RETURNS TABLE(pk jsonb,doc jsonb)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
BEGIN
  IF NOT EXISTS(SELECT 1 FROM vc.changeset WHERE id=p_revision) OR
     NOT EXISTS(SELECT 1 FROM vc.tracked_table WHERE id=p_table) THEN
    RAISE EXCEPTION 'Unknown revision or table';
  END IF;
  RETURN QUERY
  WITH RECURSIVE lineage(id,depth) AS (
    SELECT p_revision,0
    UNION ALL
    SELECT p.parent,l.depth+1 FROM lineage l CROSS JOIN LATERAL (
      SELECT p.parent FROM vc.parent p WHERE p.child=l.id AND p.ordinal=1 OFFSET 0) p
    WHERE NOT EXISTS(SELECT 1 FROM vc.snapshot_manifest m WHERE m.changeset=l.id AND m.table_id=p_table)
  ), candidates AS (
    SELECT r.pk,r.after_row AS doc,l.depth FROM lineage l
    JOIN vc.row_change r ON r.changeset=l.id AND r.table_id=p_table
    WHERE NOT EXISTS(SELECT 1 FROM vc.snapshot_manifest m WHERE m.changeset=l.id AND m.table_id=p_table)
    UNION ALL
    SELECT s.pk,s.doc,l.depth FROM lineage l JOIN vc.snapshot_row s
      ON s.changeset=l.id AND s.table_id=p_table
  ), latest AS (
    SELECT DISTINCT ON(c.pk) c.pk,c.doc FROM candidates c ORDER BY c.pk,c.depth
  ) SELECT l.pk,l.doc FROM latest l WHERE l.doc IS NOT NULL;
END $$;

CREATE OR REPLACE FUNCTION vc._file_state(p_revision uuid,p_path text) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
  -- The nearest first-parent ancestor that recorded the path decides its state;
  -- the walk ends there. found marks that changeset; at most one row has it.
  WITH RECURSIVE lineage(id,found) AS (
    SELECT p_revision,EXISTS(SELECT 1 FROM vc.path_change c WHERE c.changeset=p_revision AND c.path=p_path)
    WHERE EXISTS(SELECT 1 FROM vc.path_change c WHERE c.path=p_path)
    UNION ALL
    SELECT p.parent,EXISTS(SELECT 1 FROM vc.path_change c WHERE c.changeset=p.parent AND c.path=p_path)
    FROM lineage l CROSS JOIN LATERAL (
      SELECT p.parent FROM vc.parent p WHERE p.child=l.id AND p.ordinal=1 OFFSET 0) p
    WHERE NOT l.found
  ) SELECT CASE WHEN c.blob IS NULL THEN NULL ELSE
      jsonb_build_object('hash',encode(c.blob,'hex'),'executable',c.executable) END
    FROM lineage l JOIN vc.path_change c ON c.changeset=l.id AND c.path=p_path
    WHERE l.found
$$;

-- vc.merge_base given the ancestors of its first argument, so that vc.merge can
-- reuse the walk it already made.
CREATE OR REPLACE FUNCTION vc._merge_base(p_a_ancestors uuid[],p_b uuid) RETURNS uuid
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
DECLARE candidates uuid[];
BEGIN
  WITH common AS (SELECT unnest(p_a_ancestors) AS id INTERSECT SELECT id FROM vc._ancestors(p_b))
  SELECT array_agg(c.id) INTO candidates FROM common c WHERE NOT EXISTS(
    SELECT 1 FROM vc.parent p JOIN common child ON child.id=p.child WHERE p.parent=c.id);
  IF coalesce(cardinality(candidates),0)<>1 THEN
    RAISE EXCEPTION 'A unique merge base is required';
  END IF;
  RETURN candidates[1];
END $$;
REVOKE ALL ON FUNCTION vc._merge_base(uuid[],uuid) FROM PUBLIC;

CREATE OR REPLACE FUNCTION vc.merge_base(p_a uuid,p_b uuid) RETURNS uuid
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
BEGIN
  RETURN vc._merge_base(ARRAY(SELECT id FROM vc._ancestors(p_a)),p_b);
END $$;

CREATE OR REPLACE FUNCTION vc.merge(p_target text,p_source uuid,p_expected uuid,p_author text DEFAULT 'agent')
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
DECLARE base_id uuid; ctx vc.work_tx; t record; d record;
        file_path text; old_file jsonb; source_file jsonb; target_file jsonb;
        target_ancestors uuid[]; ours uuid[]; theirs uuid[]; a uuid; b uuid; meet uuid;
BEGIN
  PERFORM vc.begin_change(p_target,p_expected,p_author,'merge '||p_source::text);
  ctx:=vc._context();
  IF NOT EXISTS(SELECT 1 FROM vc.changeset WHERE id=p_source) THEN RAISE EXCEPTION 'Unknown source'; END IF;
  -- One walk of the target's ancestors serves the already-merged test and the merge base.
  target_ancestors:=ARRAY(SELECT id FROM vc._ancestors(ctx.base));
  IF p_source=ANY(target_ancestors) THEN
    DELETE FROM vc.work_tx WHERE tx=ctx.tx;
    RETURN ctx.base;
  END IF;
  base_id:=vc._merge_base(target_ancestors,p_source);
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
  -- File states follow first-parent chains. Walk the chains of the merge base and
  -- the source in step until one reaches a changeset the other has visited; from
  -- there down the chains are the same, so a path recorded on neither chain above
  -- that changeset has the same state on both sides and the first test below
  -- would skip it. Only the paths recorded above it are checked, in path order.
  a:=base_id; b:=p_source; ours:=ARRAY[a]; theirs:=ARRAY[b];
  LOOP
    meet:=CASE WHEN a=ANY(theirs) THEN a WHEN b=ANY(ours) THEN b END;
    EXIT WHEN meet IS NOT NULL OR (a IS NULL AND b IS NULL);
    IF a IS NOT NULL THEN
      a:=(SELECT p.parent FROM vc.parent p WHERE p.child=a AND p.ordinal=1);
      IF a IS NOT NULL THEN ours:=ours||a; END IF;
    END IF;
    IF b IS NOT NULL THEN
      b:=(SELECT p.parent FROM vc.parent p WHERE p.child=b AND p.ordinal=1);
      IF b IS NOT NULL THEN theirs:=theirs||b; END IF;
    END IF;
  END LOOP;
  FOR file_path IN SELECT DISTINCT c.path FROM vc.path_change c
      WHERE c.changeset=ANY(ours[1:coalesce(array_position(ours,meet),cardinality(ours)+1)-1] ||
                            theirs[1:coalesce(array_position(theirs,meet),cardinality(theirs)+1)-1])
      ORDER BY c.path LOOP
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

-- Snapshot policy, read-only: the head of p_branch, once per tracked table whose
-- rows_at(head) would replay at least p_interval commits since its nearest
-- snapshot (the walk looks at most p_interval commits back). A maintenance
-- executor materializes each returned target with vc.materialize; with the
-- policy applied every few commits a head read replays fewer than about
-- p_interval commits plus the commits since the last check.
CREATE OR REPLACE FUNCTION vc.snapshot_due(p_branch text DEFAULT 'main',p_interval integer DEFAULT 100)
RETURNS TABLE(revision uuid,table_id uuid)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
DECLARE h uuid := vc.head(p_branch);
BEGIN
  IF p_interval IS NULL OR p_interval NOT BETWEEN 1 AND 100000 THEN
    RAISE EXCEPTION 'Snapshot interval must be 1..100000';
  END IF;
  RETURN QUERY SELECT h,t.id FROM vc.tracked_table t WHERE (
    WITH RECURSIVE lineage(id,depth) AS (
      SELECT h,0
      UNION ALL
      SELECT p.parent,l.depth+1 FROM lineage l CROSS JOIN LATERAL (
        SELECT p.parent FROM vc.parent p WHERE p.child=l.id AND p.ordinal=1 OFFSET 0) p
      WHERE l.depth<p_interval AND NOT EXISTS(
        SELECT 1 FROM vc.snapshot_manifest m WHERE m.changeset=l.id AND m.table_id=t.id)
    ) SELECT max(l.depth) FROM lineage l) >= p_interval
  ORDER BY t.id;
END $$;
REVOKE ALL ON FUNCTION vc.snapshot_due(text,integer) FROM PUBLIC;
DO $$ BEGIN
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='agentdb_manager') THEN
    GRANT EXECUTE ON FUNCTION vc.snapshot_due(text,integer) TO agentdb_manager;
  END IF;
END $$;
COMMIT;
