-- Rerunnable migration for existing installations (and the last step of a fresh
-- install): a read-only measure of how much history vc.merge still walks.
-- Apply as the existing database owner; CREATE OR REPLACE keeps grants.
--
-- vc.merge walks every changeset reachable from the target head (vc._ancestors
-- in vc.merge and again in vc._merge_base), so its cost grows with that number
-- even with snapshots. vc.history_health reports it, timed, so the manager agent
-- can tell when the walk needs generation numbers instead of a full walk
-- (docs/STAGING_S4_S5.md). It is STABLE: PostgreSQL rejects any data change
-- inside it. Its cost is one ancestor walk plus one first-parent walk.
BEGIN;
CREATE OR REPLACE FUNCTION vc.history_health(p_branch text DEFAULT 'main') RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
DECLARE h uuid := vc.head(p_branch); started timestamptz; reachable bigint; walk_ms numeric; result jsonb;
BEGIN
  -- The same walk vc.merge makes over the target's ancestors.
  started:=clock_timestamp();
  SELECT count(*) INTO reachable FROM vc._ancestors(h);
  walk_ms:=round((extract(epoch FROM clock_timestamp()-started)*1000)::numeric,3);
  -- Per tracked table: the commits rows_at(head) replays before it reaches a
  -- snapshot (the snapshot's first-parent depth), or the first-parent depth of
  -- the root when the chain has none. vc.snapshot_due(branch, n) names a table
  -- exactly when this is at least n.
  WITH RECURSIVE chain(id,depth) AS (
    SELECT h,0
    UNION ALL
    SELECT p.parent,c.depth+1 FROM chain c CROSS JOIN LATERAL (
      SELECT p.parent FROM vc.parent p WHERE p.child=c.id AND p.ordinal=1 OFFSET 0) p
  ), root AS (SELECT max(depth) AS depth FROM chain),
  tables AS (
    SELECT t.relation::text AS relation,coalesce((
      SELECT min(c.depth) FROM chain c JOIN vc.snapshot_manifest m ON m.changeset=c.id AND m.table_id=t.id),
      (SELECT depth FROM root)) AS since
    FROM vc.tracked_table t)
  SELECT jsonb_build_object(
    'branch',p_branch,'head',h,
    'reachable_changesets',reachable,
    'ancestor_walk_ms',walk_ms,
    'changesets_total',(SELECT count(*) FROM vc.changeset),
    'first_parent_depth',(SELECT depth FROM root),
    'commits_since_snapshot',(SELECT coalesce(jsonb_object_agg(relation,since ORDER BY relation),'{}') FROM tables),
    'max_commits_since_snapshot',(SELECT coalesce(max(since),0) FROM tables))
  INTO result;
  RETURN result;
END $$;
REVOKE ALL ON FUNCTION vc.history_health(text) FROM PUBLIC;
DO $$ BEGIN
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='agentdb_manager') THEN
    GRANT EXECUTE ON FUNCTION vc.history_health(text) TO agentdb_manager;
  END IF;
END $$;
COMMIT;
