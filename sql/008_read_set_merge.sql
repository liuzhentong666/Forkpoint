-- Rerunnable migration for existing installations (and the last step of a fresh
-- install): merges that refuse write skew. Adds new functions only; vc.merge and
-- every existing function are unchanged. Apply as the existing database owner.
--
-- * vc.merge_validated(target, source, reads, author) merges like vc.merge, but
--   instead of naming the exact current head the caller names the rows its
--   decision read and did not write. When any of them differs between the merge
--   base and the target's head, it raises RS001 (read_conflict) with each changed
--   key and its old and new value in DETAIL, and nothing is merged. Write-write
--   conflicts and native constraints stay vc.merge's (P0001, 23xxx).
-- * p_reads maps registered table ids to arrays of primary keys:
--   {"<vc.table_id('app.products')>": [{"id": 1}, ...]}. A malformed read set, an
--   unregistered table, or a key that is not the table's primary key exactly as
--   history records it (other columns, a null, or a value of another type such as
--   {"id": "1"} for an integer id) raises 22023, so a mistake cannot pass as
--   "nothing changed".
-- * The read set replaces the expected-head check, so it is the caller's own
--   declaration: a row the decision used but the set leaves out is not checked
--   (an empty set declares that the decision read nothing and merges like a
--   blind retry). Callers send every row their decision used; agent_lab.py does
--   not expose this function.
-- * Rows are compared by value at the two versions with vc._rows_at_keys, which
--   follows vc.rows_at's first-parent lineage but resolves only the given keys:
--   the cost follows that lineage and the number of keys, not the table size.
--   docs/READSET_EXPERIMENT.md has the experiment behind this design, and
--   scripts/benchmark_read_set_merge.py measures this implementation.
-- * Rollback (no data to undo; merges made through it are ordinary vc.merge
--   commits): DROP FUNCTION vc.merge_validated(text,uuid,jsonb,text);
--   DROP FUNCTION vc._rows_at_keys(uuid,uuid,jsonb[]);
BEGIN;
-- ROWS 10: at most one row per key. With the default estimate (1,000 rows) the
-- planner costs merge_validated's check above jit_above_cost and JIT-compiles it
-- on every call, about 10 ms even for an empty read set (docs/READSET_EXPERIMENT.md).
CREATE OR REPLACE FUNCTION vc._rows_at_keys(p_revision uuid,p_table uuid,p_keys jsonb[])
RETURNS TABLE(pk jsonb,doc jsonb)
LANGUAGE sql STABLE SECURITY DEFINER ROWS 10 SET search_path=pg_catalog,vc,pg_temp AS $$
  -- vc.rows_at for p_keys only. Each key is looked up by itself through the
  -- (table_id, pk, changeset) history index and the snapshot's primary key, so no
  -- table-sized set is scanned: the newest history row on the lineage wins, else
  -- the snapshot row. ARRAY[...] keeps a deletion (NULL) from falling through.
  WITH RECURSIVE lineage(id,depth) AS (
    SELECT p_revision,0
    UNION ALL
    SELECT p.parent,l.depth+1 FROM lineage l CROSS JOIN LATERAL (
      SELECT p.parent FROM vc.parent p WHERE p.child=l.id AND p.ordinal=1 OFFSET 0) p
    WHERE NOT EXISTS(SELECT 1 FROM vc.snapshot_manifest m WHERE m.changeset=l.id AND m.table_id=p_table)
  ), snapshot AS (
    SELECT l.id FROM lineage l
    WHERE EXISTS(SELECT 1 FROM vc.snapshot_manifest m WHERE m.changeset=l.id AND m.table_id=p_table)
  ), latest AS (
    SELECT k.pk,coalesce(
      (SELECT ARRAY[r.after_row] FROM vc.row_change r JOIN lineage l ON l.id=r.changeset
       WHERE r.table_id=p_table AND r.pk=k.pk AND NOT EXISTS(SELECT 1 FROM snapshot n WHERE n.id=l.id)
       ORDER BY l.depth LIMIT 1),
      (SELECT ARRAY[s.doc] FROM snapshot n JOIN vc.snapshot_row s
         ON s.changeset=n.id AND s.table_id=p_table AND s.pk=k.pk)) AS found
    FROM (SELECT DISTINCT unnest(p_keys) AS pk) k
  ) SELECT l.pk,l.found[1] FROM latest l WHERE l.found[1] IS NOT NULL
$$;

CREATE OR REPLACE FUNCTION vc.merge_validated(p_target text,p_source uuid,p_reads jsonb,p_author text DEFAULT 'agent')
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,vc,pg_temp AS $$
DECLARE target_head uuid; read_base uuid; conflicts jsonb; read_table record; bad jsonb;
BEGIN
  -- CASE keeps each check from running on input an earlier check already refused.
  IF jsonb_typeof(p_reads) IS DISTINCT FROM 'object' OR EXISTS(
       SELECT 1 FROM jsonb_each(p_reads) r
       LEFT JOIN vc.tracked_table t ON t.id=CASE
         WHEN r.key ~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
         THEN r.key::uuid END
       WHERE CASE WHEN t.id IS NULL OR jsonb_typeof(r.value)<>'array' THEN true
                  ELSE EXISTS(SELECT 1 FROM jsonb_array_elements(r.value) e(key_doc) WHERE CASE
                         WHEN jsonb_typeof(e.key_doc)<>'object' THEN true
                         ELSE ARRAY(SELECT jsonb_object_keys(e.key_doc) ORDER BY 1)
                              <> ARRAY(SELECT unnest(t.pk_columns) ORDER BY 1) END) END) THEN
    RAISE EXCEPTION 'p_reads must map registered table ids to arrays of primary keys'
      USING ERRCODE='22023', HINT='{"<vc.table_id(...)>": [{"id": 1}, ...]}';
  END IF;
  -- Each key must equal the primary key the capture trigger records for that row
  -- (vc._pk of the row rebuilt from the key), else it would match no row at
  -- either version and read as unchanged. A value the column type cannot take
  -- at all, such as "abc" for an integer, fails the rebuild itself.
  FOR read_table IN SELECT r.key::uuid AS id,r.value AS keys,tt.relation
           FROM jsonb_each(p_reads) r JOIN vc.tracked_table tt ON tt.id=r.key::uuid LOOP
    BEGIN
      EXECUTE format('SELECT e.k FROM jsonb_array_elements($1) e(k)
                      WHERE e.k IS DISTINCT FROM vc._pk($2,to_jsonb(jsonb_populate_record(NULL::%s,e.k)))
                         OR EXISTS(SELECT 1 FROM jsonb_each(e.k) v WHERE v.value=''null''::jsonb) LIMIT 1',
                     read_table.relation) INTO bad USING read_table.keys,read_table.id;
    EXCEPTION WHEN data_exception THEN
      bad:=to_jsonb(SQLERRM);
    END;
    IF bad IS NOT NULL THEN
      RAISE EXCEPTION 'p_reads key does not match the primary key of %',read_table.relation
        USING ERRCODE='22023', DETAIL=bad::text;
    END IF;
  END LOOP;
  IF NOT EXISTS(SELECT 1 FROM vc.changeset WHERE id=p_source) THEN RAISE EXCEPTION 'Unknown source'; END IF;
  -- The target's head lock first, as vc.begin_change takes it, so the reads are
  -- checked against the head this merge is built on.
  SELECT b.head INTO target_head FROM vc.branch b WHERE b.name=p_target FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Unknown branch'; END IF;
  read_base:=vc.merge_base(target_head,p_source);
  -- Equal to the head: nothing landed since. Equal to the source: already merged,
  -- and vc.merge returns the head unchanged. A NULL base is neither, so the reads
  -- are still checked (and every existing read row then counts as changed).
  IF read_base IS DISTINCT FROM target_head AND read_base IS DISTINCT FROM p_source THEN
    SELECT jsonb_agg(jsonb_build_object('table',r.key,'key',d.pk,'base',d.before_doc,'now',d.after_doc)
                     ORDER BY r.key,d.pk::text)
    INTO conflicts
    FROM jsonb_each(p_reads) r
    CROSS JOIN LATERAL (SELECT ARRAY(SELECT jsonb_array_elements(r.value)) AS keys) k
    CROSS JOIN LATERAL (
      SELECT pk,a.doc AS before_doc,z.doc AS after_doc
      FROM vc._rows_at_keys(read_base,r.key::uuid,k.keys) a
      FULL JOIN vc._rows_at_keys(target_head,r.key::uuid,k.keys) z USING (pk)
      WHERE a.doc IS DISTINCT FROM z.doc) d;
    IF conflicts IS NOT NULL THEN
      RAISE EXCEPTION 'Read conflict: % row(s) the source read changed on % since %',
        jsonb_array_length(conflicts),p_target,read_base USING ERRCODE='RS001', DETAIL=conflicts::text;
    END IF;
  END IF;
  RETURN vc.merge(p_target,p_source,target_head,p_author);
END $$;

REVOKE ALL ON FUNCTION vc._rows_at_keys(uuid,uuid,jsonb[]) FROM PUBLIC;
REVOKE ALL ON FUNCTION vc.merge_validated(text,uuid,jsonb,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION vc.merge_validated(text,uuid,jsonb,text) TO agentdb_client;
COMMIT;
