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
COMMIT;
