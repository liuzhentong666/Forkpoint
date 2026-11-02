-- Rerunnable migration for existing installations (the last step of a fresh install):
-- the boundary around an agent nobody is watching. Apply as the existing database
-- owner. Adds tables, functions, triggers and roles; changes no existing function except
-- to grant the new roles some of them, and no recorded row, file or commit.
-- docs/AGENT_BOUNDARY.md has the design.
--
-- * An agent gets its own login, a member of agentdb_worker (agent_boundary.py issues
--   it). That role reads, writes branches it created, and moves main only through
--   vc.agent_merge. vc.begin_change, vc.merge and direct writes to the tables are not
--   granted, so raw SQL on the login cannot reach them. Rerun this file after
--   vc.register_table: a new table stays out of the agents' reach until then.
-- * The database names the author (session_user survives SECURITY DEFINER), and every
--   login spends a prepaid budget: main_rows_left for what it lands on main, bytes_left
--   for what it stores. There are no top-ups. No budget row means no writes.
-- * A budget says nothing about a table's size, so a login also has max_table_share: beyond
--   its first 20 rows, it may change at most that share of a table, over its whole life and
--   in any number of merges, and the same for files. Within those 20 it still changes at most
--   half of the rows (or files) that were there before, and always at least one, so they cannot
--   wipe or rewrite a small table; adding rows is not limited by that. That refuses the commonest
--   thing agents have really done, wiping or rewriting the data while on a routine task (an agent
--   running DELETE or UPDATE with no WHERE, or fabricating rows). A login meant to rewrite whole
--   tables is issued with a share of 1, which means no limit; that is decided before it starts.
-- * agentdb_keeper can tighten and undo (vc.freeze, vc.stop_agent, vc.revert) and end
--   stuck sessions, but nothing it holds can loosen a limit. vc.unfreeze is granted to
--   no one: only the owner runs it. agentdb_broker creates logins and cannot make
--   anything stronger than agentdb_worker (PostgreSQL 16's CREATEROLE rules).
-- * vc.boundary_problems checks all of this from the catalog; vc.boundary_check, run in
--   the manager agent's loop (agent_state/maintenance.py), freezes writes when it finds
--   anything. Reads continue while frozen.
-- * Refusals carry a stable SQLSTATE, the facts as JSON in DETAIL and the next step in HINT:
--   AB000 frozen; AB001 not your branch; AB002 merge budget; AB003 storage budget;
--   AB004 not a commit on main, or nothing to undo; AB005 changed since;
--   AB006 not READ COMMITTED (an older snapshot could miss a freeze);
--   AB007 too large a share of a table or of the files.
-- * Rerunning this file also accepts the engine as it now is (the fingerprint and the list
--   of superuser logins in vc.gate). Any later engine migration has to be followed by this
--   file, or the self-check reports it and writes freeze. The freeze itself stays until
--   the owner runs vc.unfreeze.
-- * This database stops accepting every role by default: CONNECT and TEMPORARY are revoked
--   from PUBLIC, and large objects, which no budget covers, can no longer be created.
--   agentdb_client keeps CONNECT and TEMPORARY, and agentdb_manager CONNECT, through grants
--   of their own. A login that holds neither with INHERIT, or is not the owner, needs its
--   own CONNECT (experiments/readset_agents does this). Roles are server-wide, so other
--   databases on the server still need their own REVOKE; vc.boundary_status lists the open ones.
-- * Run by a non-superuser owner, the server-wide steps below are skipped: the server
--   administrator does them in deploy/staging/create_staging_role.sql. Revoking the
--   large-object functions needs a superuser; until one does, the self-check says so.
-- * Rollback: as the owner, DROP TRIGGER agent_changeset ON vc.changeset; DROP TRIGGER
--   agent_blob ON vc.blob; drop the functions and tables this file creates;
--   GRANT CONNECT, TEMPORARY ON DATABASE ... TO PUBLIC; as a superuser,
--   GRANT EXECUTE ON FUNCTION lo_from_bytea(oid,bytea), lo_create(oid), lo_creat(integer)
--   TO PUBLIC. History stays valid: commits the boundary recorded are ordinary commits.
--   The three roles can stay, with their grants on existing functions: without logins they do nothing.
BEGIN;
DO $roles$
BEGIN
  IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='agentdb_worker') THEN
    CREATE ROLE agentdb_worker NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  END IF;
  IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='agentdb_keeper') THEN
    CREATE ROLE agentdb_keeper NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  END IF;
  IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='agentdb_broker') THEN
    CREATE ROLE agentdb_broker NOLOGIN NOINHERIT CREATEROLE NOSUPERUSER NOCREATEDB NOREPLICATION NOBYPASSRLS;
  END IF;
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname IN ('agentdb_worker','agentdb_keeper','agentdb_broker')
      AND (rolsuper OR rolcreatedb OR rolreplication OR rolbypassrls
           OR (rolcreaterole AND rolname<>'agentdb_broker')
           OR (rolcanlogin AND rolname='agentdb_worker'))) THEN
    RAISE EXCEPTION 'An existing agentdb_worker, agentdb_keeper or agentdb_broker has incompatible attributes; inspect it without silently altering the shared role';
  END IF;
  -- Server-wide, and only a superuser can grant them.
  IF (SELECT rolsuper FROM pg_roles WHERE rolname=current_user) THEN
    GRANT pg_signal_backend,pg_read_all_stats TO agentdb_keeper;
    GRANT agentdb_worker TO agentdb_broker WITH ADMIN TRUE, INHERIT FALSE, SET FALSE;
    GRANT SET ON PARAMETER temp_file_limit,log_statement TO agentdb_broker;
    REVOKE EXECUTE ON FUNCTION pg_catalog.lo_from_bytea(oid,bytea),pg_catalog.lo_create(oid),pg_catalog.lo_creat(integer) FROM PUBLIC;
  END IF;
  EXECUTE format('REVOKE CONNECT,TEMPORARY ON DATABASE %I FROM PUBLIC',current_database());
  EXECUTE format('GRANT CONNECT,TEMPORARY ON DATABASE %I TO agentdb_client',current_database());
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO agentdb_manager,agentdb_keeper',current_database());
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO agentdb_broker WITH GRANT OPTION',current_database());
END $roles$;

CREATE TABLE IF NOT EXISTS vc.branch_owner(branch text PRIMARY KEY REFERENCES vc.branch, login name NOT NULL);
CREATE TABLE IF NOT EXISTS vc.agent_budget(login name PRIMARY KEY,
  main_rows_left bigint NOT NULL CHECK(main_rows_left>=0),
  bytes_left bigint NOT NULL CHECK(bytes_left>=0),
  max_table_share numeric NOT NULL DEFAULT 0.1 CHECK(max_table_share>0 AND max_table_share<=1));
-- What each login has landed on main, by table id or 'files', for max_table_share; the same
-- target followed by ' existing' counts only the rows or files that were there before.
CREATE TABLE IF NOT EXISTS vc.agent_spend(login name NOT NULL,target text NOT NULL,changed bigint NOT NULL,
  PRIMARY KEY(login,target));
CREATE TABLE IF NOT EXISTS vc.gate(one boolean PRIMARY KEY DEFAULT true CHECK(one),
  frozen_reason text, frozen_by name, frozen_at timestamptz,
  engine_fingerprint text, superuser_logins name[]);
INSERT INTO vc.gate DEFAULT VALUES ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION vc._refuse(p_code text,p_message text,p_detail jsonb,p_next text) RETURNS void
LANGUAGE plpgsql SET search_path=pg_catalog,vc,pg_temp AS $$
BEGIN RAISE EXCEPTION '%',p_message USING ERRCODE=p_code,DETAIL=p_detail::text,HINT=p_next; END $$;

-- Roles holding agentdb_worker's rights, directly or through another role. Read from the
-- catalog because pg_has_role is true for every superuser. A membership with neither
-- INHERIT nor SET gives no rights: that is how the broker holds its ADMIN.
CREATE OR REPLACE FUNCTION vc._agent_roles() RETURNS SETOF oid
LANGUAGE sql STABLE SET search_path=pg_catalog,vc,pg_temp AS $$
  WITH RECURSIVE holder(role) AS (
    SELECT m.member FROM pg_auth_members m
    WHERE m.roleid='agentdb_worker'::regrole AND (m.inherit_option OR m.set_option)
    UNION
    SELECT m.member FROM pg_auth_members m JOIN holder h ON m.roleid=h.role
    WHERE m.inherit_option OR m.set_option
  ) SELECT role FROM holder
$$;

CREATE OR REPLACE FUNCTION vc._is_agent() RETURNS boolean
LANGUAGE sql STABLE SET search_path=pg_catalog,vc,pg_temp AS $$
  SELECT EXISTS(SELECT 1 FROM vc._agent_roles() a JOIN pg_roles r ON r.oid=a WHERE r.rolname=session_user)
$$;

CREATE OR REPLACE FUNCTION vc._gate_open() RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,vc,pg_temp AS $$
DECLARE g vc.gate;
BEGIN
  IF current_setting('transaction_isolation')<>'read committed' THEN
    PERFORM vc._refuse('AB006','Agent writes need READ COMMITTED',
      jsonb_build_object('transaction_isolation',current_setting('transaction_isolation')),
      'An older snapshot could miss a freeze. Start a new transaction at READ COMMITTED.');
  END IF;
  SELECT * INTO g FROM vc.gate;
  IF NOT FOUND OR g.frozen_reason IS NOT NULL THEN
    PERFORM vc._refuse('AB000','Writes are frozen',
      jsonb_build_object('reason',coalesce(g.frozen_reason,'vc.gate is empty'),'by',g.frozen_by,'at',g.frozen_at),
      'Reads still work. Stop writing and report this reason in your task status.');
  END IF;
END $$;

CREATE OR REPLACE FUNCTION vc._charge_bytes(p_cost bigint) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,vc,pg_temp AS $$
BEGIN
  UPDATE vc.agent_budget SET bytes_left=bytes_left-p_cost WHERE login=session_user AND bytes_left>=p_cost;
  IF NOT FOUND THEN
    PERFORM vc._refuse('AB003','Storage budget spent',
      jsonb_build_object('login',session_user,'bytes',p_cost,
        'bytes_left',(SELECT bytes_left FROM vc.agent_budget WHERE login=session_user)),
      'Nothing was written. Make the change smaller; a new budget comes only with a new login.');
  END IF;
END $$;

-- Every changeset an agent's login creates, by any path: the database names the author and
-- the storage is paid for. A flat cost per commit and per row or path, so nothing is free:
-- text lengths, not stored sizes, so a row that compresses well pays for what it unpacks to.
CREATE OR REPLACE FUNCTION vc._agent_changeset() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,vc,pg_temp AS $$
BEGIN
  IF vc._is_agent() THEN
    PERFORM vc._gate_open();
    NEW.author:=session_user;
    PERFORM vc._charge_bytes(256+octet_length(NEW.message)
      +coalesce((SELECT sum(64+octet_length(pk::text)+coalesce(octet_length(before_row::text),0)
                            +coalesce(octet_length(after_row::text),0))
                 FROM vc.pending_row WHERE tx=txid_current()),0)
      +coalesce((SELECT sum(64+octet_length(path)) FROM vc.pending_path WHERE tx=txid_current()),0));
  END IF;
  RETURN NEW;
END $$;
CREATE OR REPLACE TRIGGER agent_changeset BEFORE INSERT ON vc.changeset
  FOR EACH ROW EXECUTE FUNCTION vc._agent_changeset();

-- A file's content stays in vc.blob even if the path is overwritten before finish_change,
-- so it is paid for when it is stored. A BEFORE trigger fires ahead of write_file's
-- ON CONFLICT, so storing the same content again is charged again.
CREATE OR REPLACE FUNCTION vc._agent_blob() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,vc,pg_temp AS $$
BEGIN
  IF vc._is_agent() THEN PERFORM vc._charge_bytes(64+octet_length(NEW.content)); END IF;
  RETURN NEW;
END $$;
CREATE OR REPLACE TRIGGER agent_blob BEFORE INSERT ON vc.blob
  FOR EACH ROW EXECUTE FUNCTION vc._agent_blob();

-- The agent's whole write surface: three functions, none taking an author, plus
-- vc.write_file and vc.finish_change on the change it opened.
CREATE OR REPLACE FUNCTION vc.agent_fork(p_name text,p_revision uuid) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,vc,pg_temp AS $$
BEGIN
  PERFORM vc._gate_open();
  PERFORM vc._charge_bytes(256);
  PERFORM vc.fork(p_name,p_revision);
  INSERT INTO vc.branch_owner VALUES(p_name,session_user);
  RETURN p_revision;
END $$;

CREATE OR REPLACE FUNCTION vc.agent_begin(p_branch text,p_expected uuid,p_message text DEFAULT '') RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,vc,pg_temp AS $$
BEGIN
  PERFORM vc._gate_open();
  IF NOT EXISTS(SELECT 1 FROM vc.branch_owner WHERE branch=p_branch AND login=session_user) THEN
    PERFORM vc._refuse('AB001','Not your branch',jsonb_build_object('branch',p_branch,'you',session_user),
      'Fork your own branch with vc.agent_fork(name, revision) and work there. main moves only through vc.agent_merge.');
  END IF;
  RETURN vc.begin_change(p_branch,p_expected,session_user,p_message);
END $$;

-- Charges one merge against max_table_share, per table and for files. Up to 20 rows the share
-- does not apply, so an ordinary merge pays nothing for a large table's size. Within those 20, a
-- login still changes at most half of the rows that were there before (and always one), so the
-- free rows cannot wipe or rewrite a table of 40 rows or fewer; that needs a count of at most 40
-- rows, and only for a merge that changes rows that were there. The size is the table's before
-- this merge, so whittling it down makes the limit tighter, not looser.
CREATE OR REPLACE FUNCTION vc._check_share(p_revision uuid,p_before uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,vc,pg_temp AS $$
DECLARE share numeric; spent record; total bigint; existing bigint; size bigint; over boolean;
BEGIN
  SELECT max_table_share INTO share FROM vc.agent_budget WHERE login=session_user;
  FOR spent IN
    SELECT r.table_id::text AS target,t.relation,count(*) AS n,
           count(*) FILTER (WHERE r.op='I') AS added,count(*) FILTER (WHERE r.op='D') AS removed,
           count(*) FILTER (WHERE r.op<>'I') AS prior
    FROM vc.row_change r JOIN vc.tracked_table t ON t.id=r.table_id
    WHERE r.changeset=p_revision GROUP BY r.table_id,t.relation
    UNION ALL
    SELECT 'files',NULL,count(*),0,0,count(*) FILTER (WHERE vc._file_state(p_before,f.path) IS NOT NULL)
    FROM vc.path_change f WHERE f.changeset=p_revision HAVING count(*)>0
  LOOP
    INSERT INTO vc.agent_spend AS s VALUES(session_user,spent.target,spent.n)
    ON CONFLICT(login,target) DO UPDATE SET changed=s.changed+EXCLUDED.changed RETURNING s.changed INTO total;
    INSERT INTO vc.agent_spend AS s VALUES(session_user,spent.target||' existing',spent.prior)
    ON CONFLICT(login,target) DO UPDATE SET changed=s.changed+EXCLUDED.changed RETURNING s.changed INTO existing;
    CONTINUE WHEN share>=1 OR (total<=20 AND spent.prior=0);
    IF total>20 THEN
      IF spent.relation IS NULL THEN
        size:=(SELECT count(*) FROM (SELECT DISTINCT c.path FROM vc.path_change c) p
               WHERE vc._file_state(p_before,p.path) IS NOT NULL);
      ELSE
        EXECUTE format('SELECT count(*) FROM %s',spent.relation) INTO size;
        size:=size-spent.added+spent.removed;
      END IF;
      over:=total>share*size;
    ELSE
      -- At most 40 counted: with 40 or more before, half is 20 or more, which total<=20 never exceeds.
      IF spent.relation IS NULL THEN
        size:=(SELECT count(*) FROM (SELECT 1 FROM (SELECT DISTINCT c.path FROM vc.path_change c) p
               WHERE vc._file_state(p_before,p.path) IS NOT NULL LIMIT 40) x);
      ELSE
        EXECUTE format('SELECT count(*) FROM (SELECT 1 FROM %s LIMIT %s) x',spent.relation,40+spent.added) INTO size;
        size:=size-spent.added+spent.removed;
      END IF;
      over:=existing>greatest(1,size/2);
    END IF;
    IF over THEN
      PERFORM vc._refuse('AB007','Too large a share of '||coalesce(spent.relation::text,'the files'),
        jsonb_build_object('login',session_user,'target',coalesce(spent.relation::text,'files'),
          'changed_by_this_login',total,'existing_changed_by_this_login',existing,'size_before_this_merge',size,
          'max_table_share',share,'rule',CASE WHEN total>20 THEN 'beyond 20 rows, at most max_table_share of the table'
                                               ELSE 'within 20 rows, at most half of what was there' END),
        'Nothing was merged. Change less. If the task really rewrites this much, a login issued with a larger max_table_share has to redo it on a branch of its own, since a login merges only branches it created; or the database owner can merge this branch with vc.merge.');
    END IF;
  END LOOP;
END $$;

-- The one way an agent moves main. A login with nothing left is refused before it can take
-- main's lock. Otherwise the merge runs first, so its real size is known; over budget
-- raises, and the whole merge rolls back with it.
CREATE OR REPLACE FUNCTION vc.agent_merge(p_branch text,p_reads jsonb DEFAULT '{}') RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,vc,pg_temp AS $$
DECLARE source uuid; before_head uuid; result uuid; n bigint;
BEGIN
  PERFORM vc._gate_open();
  SELECT b.head INTO source FROM vc.branch b JOIN vc.branch_owner o ON o.branch=b.name
  WHERE b.name=p_branch AND o.login=session_user;
  IF NOT FOUND THEN
    PERFORM vc._refuse('AB001','Not your branch',jsonb_build_object('branch',p_branch,'you',session_user),
      'You can merge only branches you created.');
  END IF;
  IF NOT EXISTS(SELECT 1 FROM vc.agent_budget WHERE login=session_user AND main_rows_left>0) THEN
    PERFORM vc._refuse('AB002','Merge budget spent',jsonb_build_object('login',session_user,
        'main_rows_left',(SELECT main_rows_left FROM vc.agent_budget WHERE login=session_user)),
      'Nothing was merged. A new budget comes only with a new login.');
  END IF;
  -- vc.merge_validated takes this lock first as well, so the head read here is the one it builds on.
  SELECT b.head INTO before_head FROM vc.branch b WHERE b.name='main' FOR UPDATE;
  result:=vc.merge_validated('main',source,p_reads,session_user);
  IF result IS DISTINCT FROM before_head THEN
    n:=(SELECT count(*) FROM vc.row_change WHERE changeset=result)+(SELECT count(*) FROM vc.path_change WHERE changeset=result);
    UPDATE vc.agent_budget SET main_rows_left=main_rows_left-n WHERE login=session_user AND main_rows_left>=n;
    IF NOT FOUND THEN
      PERFORM vc._refuse('AB002','Merge is larger than your remaining budget',
        jsonb_build_object('rows',n,'main_rows_left',(SELECT main_rows_left FROM vc.agent_budget WHERE login=session_user)),
        'Nothing was merged. Split the change; a new budget comes only with a new login.');
    END IF;
    PERFORM vc._check_share(result,before_head);
  END IF;
  RETURN result;
END $$;

CREATE OR REPLACE FUNCTION vc.agent_status() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,vc,pg_temp AS $$
  SELECT jsonb_build_object('login',session_user,'main_rows_left',b.main_rows_left,'bytes_left',b.bytes_left,
    'max_table_share',b.max_table_share,
    'changed',(SELECT coalesce(jsonb_object_agg(s.target,s.changed),'{}') FROM vc.agent_spend s WHERE s.login=session_user),
    'valid_until',(SELECT rolvaliduntil FROM pg_roles WHERE rolname=session_user),
    'frozen',(SELECT frozen_reason FROM vc.gate),
    'branches',(SELECT coalesce(jsonb_agg(o.branch ORDER BY o.branch),'[]') FROM vc.branch_owner o WHERE o.login=session_user))
  FROM (SELECT 1) one LEFT JOIN vc.agent_budget b ON b.login=session_user
$$;

-- For the broker. Insert only: no top-ups, no raising a budget, and a retired login's
-- name cannot be issued again, because its row stays.
CREATE OR REPLACE FUNCTION vc.issue_budget(p_login name,p_rows bigint,p_bytes bigint,p_max_table_share numeric DEFAULT 0.1) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,vc,pg_temp AS $$
  INSERT INTO vc.agent_budget VALUES(p_login,p_rows,p_bytes,p_max_table_share)
$$;

-- Tighten only. The first reason stays; a NULL or empty one still freezes.
CREATE OR REPLACE FUNCTION vc.freeze(p_reason text) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,vc,pg_temp AS $$
  UPDATE vc.gate SET frozen_reason=coalesce(frozen_reason,nullif(p_reason,''),'no reason given'),
    frozen_by=coalesce(frozen_by,session_user),frozen_at=coalesce(frozen_at,clock_timestamp())
$$;

-- Granted to no one: only the owner loosens. If the cause remains, the next check freezes again.
CREATE OR REPLACE FUNCTION vc.unfreeze() RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,vc,pg_temp AS $$
  UPDATE vc.gate SET frozen_reason=NULL,frozen_by=NULL,frozen_at=NULL
$$;

-- Spends a login's whole budget, so every write it tries is refused: all of them cost
-- something. Its sessions can still read until they are ended.
CREATE OR REPLACE FUNCTION vc.stop_agent(p_login name) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,vc,pg_temp AS $$
  UPDATE vc.agent_budget SET main_rows_left=0,bytes_left=0 WHERE login=p_login
$$;

-- What a login really landed on main, from history and not from anything it says about itself.
-- It works after the login is retired. Reverted merges stay counted; "reverted" says how many,
-- from the reverts on main's first-parent chain only. An agent lands nothing there but merges,
-- whose message the engine writes, so a message it writes on its own branch claims nothing.
CREATE OR REPLACE FUNCTION vc.agent_report(p_login name) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,vc,pg_temp AS $$
  WITH RECURSIVE own AS (
    SELECT c.id,c.created_at FROM vc.changeset c
    WHERE c.author=p_login AND EXISTS(SELECT 1 FROM vc.parent p WHERE p.child=c.id AND p.ordinal=2)
  ), main_line(id) AS (
    SELECT b.head FROM vc.branch b WHERE b.name='main'
    UNION ALL
    SELECT p.parent FROM main_line m JOIN vc.parent p ON p.child=m.id AND p.ordinal=1
  ), reverts AS (
    SELECT substr(c.message,8) AS target FROM main_line m JOIN vc.changeset c ON c.id=m.id WHERE c.message LIKE 'revert %'
  )
  SELECT jsonb_build_object('login',p_login,'merges',(SELECT count(*) FROM own),
    'first',(SELECT min(created_at) FROM own),'last',(SELECT max(created_at) FROM own),
    'reverted',(SELECT count(*) FROM own o WHERE o.id::text IN (SELECT target FROM reverts)),
    'tables',(SELECT coalesce(jsonb_object_agg(x.relation,jsonb_build_object('inserted',x.i,'updated',x.u,'deleted',x.d)),'{}')
              FROM (SELECT t.relation::text AS relation,count(*) FILTER (WHERE r.op='I') AS i,
                           count(*) FILTER (WHERE r.op='U') AS u,count(*) FILTER (WHERE r.op='D') AS d
                    FROM own o JOIN vc.row_change r ON r.changeset=o.id JOIN vc.tracked_table t ON t.id=r.table_id
                    GROUP BY t.relation) x),
    'files_changed',(SELECT count(*) FROM own o JOIN vc.path_change f ON f.changeset=o.id),
    'budget',(SELECT to_jsonb(b)-'login' FROM vc.agent_budget b WHERE b.login=p_login))
$$;

-- Undo one commit on main as a new commit, from what the commit recorded, so the cost follows
-- its size. Nothing is rewritten, and a row or file changed since is refused, not overwritten.
-- Rows go back in an order that ordinary (not deferrable) foreign keys between tracked tables
-- accept: rows the commit deleted come back first, referenced tables first; then updated rows;
-- rows it inserted go last, referencing tables first. Rows within one table keep no order, so a
-- table that references itself still needs its foreign key to be deferrable.
CREATE OR REPLACE FUNCTION vc.revert(p_revision uuid) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,vc,pg_temp AS $$
DECLARE h uuid; parent_id uuid; ctx vc.work_tx; t record; d record; f record; now_state jsonb;
BEGIN
  SELECT b.head INTO h FROM vc.branch b WHERE b.name='main' FOR UPDATE;
  IF NOT EXISTS(
    WITH RECURSIVE chain(id) AS (
      SELECT h UNION ALL
      SELECT p.parent FROM chain c JOIN vc.parent p ON p.child=c.id AND p.ordinal=1 WHERE c.id<>p_revision)
    SELECT 1 FROM chain WHERE id=p_revision) THEN
    PERFORM vc._refuse('AB004','Not a commit on main',jsonb_build_object('revision',p_revision),
      'Give a revision from main''s history.');
  END IF;
  SELECT p.parent INTO parent_id FROM vc.parent p WHERE p.child=p_revision AND p.ordinal=1;
  IF NOT FOUND THEN
    PERFORM vc._refuse('AB004','Nothing to undo',jsonb_build_object('revision',p_revision),'Genesis cannot be undone.');
  END IF;
  PERFORM vc.begin_change('main',h,session_user,'revert '||p_revision::text);
  ctx:=vc._context();
  FOR t IN WITH RECURSIVE edge AS (
             SELECT k.conrelid AS child,k.confrelid AS parent FROM pg_constraint k
             WHERE k.contype='f' AND k.conrelid<>k.confrelid
               AND k.conrelid IN (SELECT relation FROM vc.tracked_table) AND k.confrelid IN (SELECT relation FROM vc.tracked_table)
           ), depth(relation,n) AS (
             SELECT relation::oid,0 FROM vc.tracked_table
             UNION
             SELECT e.child,dp.n+1 FROM depth dp JOIN edge e ON e.parent=dp.relation WHERE dp.n<64
           ), level AS (
             SELECT tt.id,max(dp.n) AS n FROM vc.tracked_table tt JOIN depth dp ON dp.relation=tt.relation GROUP BY tt.id
           ), grouped AS (
             SELECT r.table_id,CASE WHEN r.after_row IS NULL THEN 1 WHEN r.before_row IS NULL THEN 3 ELSE 2 END AS step,
                    array_agg(r.pk) AS keys
             FROM vc.row_change r WHERE r.changeset=p_revision GROUP BY 1,2
           )
           SELECT g.table_id,g.step,g.keys FROM grouped g JOIN level l ON l.id=g.table_id
           ORDER BY g.step,CASE WHEN g.step=3 THEN -l.n ELSE l.n END LOOP
    FOR d IN SELECT r.pk,r.before_row,r.after_row,c.doc AS current_row FROM vc.row_change r
             LEFT JOIN vc._rows_at_keys(h,t.table_id,t.keys) c ON c.pk=r.pk
             WHERE r.changeset=p_revision AND r.table_id=t.table_id
               AND CASE WHEN r.after_row IS NULL THEN 1 WHEN r.before_row IS NULL THEN 3 ELSE 2 END=t.step LOOP
      IF d.current_row IS DISTINCT FROM d.after_row THEN
        PERFORM vc._refuse('AB005','A row changed after this commit',
          jsonb_build_object('revision',p_revision,'table',t.table_id,'key',d.pk,'now',d.current_row),
          'Undo the newer commit first, or leave this one.');
      END IF;
      PERFORM vc._apply_main(t.table_id,d.pk,d.before_row);
    END LOOP;
  END LOOP;
  FOR f IN SELECT c.path FROM vc.path_change c WHERE c.changeset=p_revision LOOP
    now_state:=vc._file_state(h,f.path);
    IF now_state IS DISTINCT FROM vc._file_state(p_revision,f.path) THEN
      PERFORM vc._refuse('AB005','A file changed after this commit',
        jsonb_build_object('revision',p_revision,'path',f.path),'Undo the newer commit first, or leave this one.');
    END IF;
    INSERT INTO vc.pending_path VALUES(ctx.tx,f.path,now_state,vc._file_state(parent_id,f.path));
  END LOOP;
  RETURN vc.finish_change();
END $$;

-- Runs with the caller's rights: the keeper's pg_read_all_stats shows another login's state,
-- and its pg_signal_backend can end only non-superuser sessions. Agents can turn off their own
-- timeouts, so the limits are enforced from outside. xact_start also catches a session that
-- keeps a transaction busy with small statements or a long sleep. While frozen (p_frozen),
-- a transaction that wrote something ends at once, so a merge already past the gate cannot
-- wait for the freeze to pass and then commit; sessions that only read go on.
CREATE OR REPLACE FUNCTION vc.end_stuck_agent_sessions(p_idle_seconds integer,p_transaction_seconds integer,p_frozen boolean) RETURNS jsonb
LANGUAGE sql SET search_path=pg_catalog,vc,pg_temp AS $$
  WITH session AS (
    SELECT a.pid,r.rolname AS login,CASE
        WHEN NOT r.rolcanlogin OR r.rolvaliduntil<=clock_timestamp() THEN 'login expired'
        WHEN p_frozen AND a.backend_xid IS NOT NULL THEN 'writing while frozen'
        WHEN a.state LIKE 'idle in transaction%' AND clock_timestamp()-a.state_change>make_interval(secs=>p_idle_seconds)
          THEN 'idle in transaction'
        WHEN clock_timestamp()-a.xact_start>make_interval(secs=>p_transaction_seconds) THEN 'transaction too long'
        WHEN a.state='idle' AND clock_timestamp()-a.state_change>make_interval(secs=>p_idle_seconds)
          AND EXISTS(SELECT 1 FROM pg_locks l WHERE l.pid=a.pid AND l.locktype='advisory') THEN 'idle holding an advisory lock'
      END AS reason
    FROM pg_stat_activity a JOIN pg_roles r ON r.oid=a.usesysid
    WHERE a.datname=current_database() AND a.backend_type='client backend'
      AND r.oid IN (SELECT vc._agent_roles())
  ), ended AS (
    SELECT s.pid,s.login,s.reason,pg_terminate_backend(s.pid) AS signalled FROM session s WHERE s.reason IS NOT NULL
  )
  SELECT coalesce(jsonb_agg(jsonb_build_object('pid',e.pid,'login',e.login,'reason',e.reason) ORDER BY e.pid),'[]'::jsonb)
  FROM ended e WHERE e.signalled
$$;

CREATE OR REPLACE FUNCTION vc._engine_fingerprint() RETURNS text
LANGUAGE sql STABLE SET search_path=pg_catalog,vc,pg_temp AS $$
  SELECT encode(sha256(convert_to(coalesce(string_agg(definition,E'\n' ORDER BY definition),''),'UTF8')),'hex') FROM (
    SELECT pg_get_functiondef(p.oid)||pg_get_userbyid(p.proowner) AS definition FROM pg_proc p
    WHERE p.pronamespace='vc'::regnamespace AND p.prokind IN ('f','p')
    UNION ALL
    SELECT pg_get_triggerdef(t.oid) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
    WHERE c.relnamespace='vc'::regnamespace AND NOT t.tgisinternal
  ) engine
$$;

-- Everything that must stay true, from the catalog alone. Empty means the boundary stands.
-- Agent-controlled data never reaches it, so no agent can cause a freeze by writing something.
CREATE OR REPLACE FUNCTION vc.boundary_problems() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,vc,pg_temp AS $$
  WITH boundary(role,may_execute) AS (VALUES
    ('agentdb_worker'::regrole,ARRAY['vc.head(text)','vc.table_id(regclass)','vc.checkout(text)','vc.pin(uuid)',
      'vc.rows_at(uuid,uuid)','vc.visible_rows(uuid)','vc.read_file(uuid,text)','vc.diff_rows(uuid,uuid,uuid)',
      'vc.diff_files(uuid,uuid)','vc.history(uuid,integer)','vc.merge_base(uuid,uuid)',
      'vc.write_file(text,bytea,boolean)','vc.finish_change()','vc.agent_fork(text,uuid)',
      'vc.agent_begin(text,uuid,text)','vc.agent_merge(text,jsonb)','vc.agent_status()']),
    ('agentdb_keeper'::regrole,ARRAY['vc.head(text)','vc.history(uuid,integer)','vc.freeze(text)','vc.stop_agent(name)',
      'vc.revert(uuid)','vc.agent_report(name)','vc.boundary_check()','vc.boundary_status()',
      'vc.end_stuck_agent_sessions(integer,integer,boolean)','vc._agent_roles()']),
    ('agentdb_broker'::regrole,ARRAY['vc.issue_budget(name,bigint,bigint,numeric)','vc.stop_agent(name)'])
  ), agent_login AS (
    SELECT r.* FROM vc._agent_roles() a JOIN pg_roles r ON r.oid=a
  ), versioned AS (
    SELECT c.oid,n.nspname||'.'||c.relname AS name FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE c.relkind IN ('r','p','v','m') AND (n.nspname='vc' OR c.oid IN (SELECT relation FROM vc.tracked_table))
  ), func AS (
    -- A trigger function can only be fired, never called, so EXECUTE on one gives nothing; an
    -- application's own trigger functions beside its tables would otherwise freeze writes.
    SELECT p.oid,n.nspname||'.'||p.proname||'('||replace(oidvectortypes(p.proargtypes),', ',',')||')' AS name
    FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
    WHERE p.prorettype NOT IN ('trigger'::regtype,'event_trigger'::regtype) AND (n.nspname IN ('vc','agent') OR n.oid IN (
      SELECT c.relnamespace FROM vc.tracked_table t JOIN pg_class c ON c.oid=t.relation))
  ), found(problem) AS (
    SELECT 'disabled trigger '||n.nspname||'.'||c.relname||'.'||t.tgname
    FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE NOT t.tgisinternal AND t.tgenabled<>'O'
      AND (n.nspname IN ('vc','agent') OR c.oid IN (SELECT relation FROM vc.tracked_table))
    UNION ALL
    SELECT 'tracked table without trigger '||w.name||': '||t.relation::text
    FROM vc.tracked_table t CROSS JOIN (VALUES ('vc_capture'),('vc_no_truncate')) w(name)
    WHERE NOT EXISTS(SELECT 1 FROM pg_trigger g WHERE g.tgrelid=t.relation AND g.tgname=w.name)
    UNION ALL
    SELECT b.role::text||' may execute '||f.name FROM boundary b CROSS JOIN func f
    WHERE has_function_privilege(b.role::oid,f.oid,'EXECUTE') AND f.name<>ALL(b.may_execute)
    UNION ALL
    SELECT 'agentdb_worker may create large objects' WHERE has_function_privilege('agentdb_worker','pg_catalog.lo_create(oid)','EXECUTE')
      OR has_function_privilege('agentdb_worker','pg_catalog.lo_creat(integer)','EXECUTE')
      OR has_function_privilege('agentdb_worker','pg_catalog.lo_from_bytea(oid,bytea)','EXECUTE')
    UNION ALL
    SELECT b.role::text||' may change '||v.name FROM boundary b CROSS JOIN versioned v
    WHERE has_table_privilege(b.role::oid,v.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
    UNION ALL
    SELECT b.role::text||' may read '||v.name FROM boundary b CROSS JOIN versioned v
    WHERE v.name LIKE 'vc.%' AND has_table_privilege(b.role::oid,v.oid,'SELECT')
    UNION ALL
    SELECT 'agentdb_worker may truncate or reference agent.'||c.relname FROM pg_class c
    WHERE c.relnamespace='agent'::regnamespace AND has_table_privilege('agentdb_worker',c.oid,'TRUNCATE,REFERENCES,TRIGGER')
    UNION ALL
    SELECT b.role::text||' may create in schema '||n.nspname FROM boundary b CROSS JOIN pg_namespace n
    WHERE n.nspname NOT LIKE 'pg\_%' AND n.nspname<>'information_schema' AND has_schema_privilege(b.role::oid,n.oid,'CREATE')
    UNION ALL
    SELECT b.role::text||' may create schemas or temporary tables in this database' FROM boundary b
    WHERE has_database_privilege(b.role::oid,current_database(),'CREATE,TEMPORARY')
    UNION ALL
    SELECT 'any role may connect to this database' FROM pg_database d
    WHERE d.datname=current_database() AND (d.datacl IS NULL OR EXISTS(
      SELECT 1 FROM aclexplode(d.datacl) a WHERE a.grantee=0 AND a.privilege_type IN ('CONNECT','TEMPORARY')))
    UNION ALL
    SELECT r.rolname||' has attributes it must not have' FROM pg_roles r
    WHERE r.rolname IN ('agentdb_worker','agentdb_keeper','agentdb_broker')
      AND (r.rolsuper OR r.rolcreatedb OR r.rolreplication OR r.rolbypassrls
           OR (r.rolcreaterole AND r.rolname<>'agentdb_broker') OR (r.rolcanlogin AND r.rolname='agentdb_worker'))
    UNION ALL
    SELECT m.member::regrole::text||' holds '||m.roleid::regrole::text FROM pg_auth_members m
    WHERE m.member IN (SELECT b.role::oid FROM boundary b)
      AND NOT (m.member='agentdb_keeper'::regrole AND NOT m.admin_option
               AND m.roleid IN ('pg_signal_backend'::regrole,'pg_read_all_stats'::regrole))
      AND NOT (m.member='agentdb_broker'::regrole AND m.admin_option AND NOT m.inherit_option AND NOT m.set_option
               AND (m.roleid='agentdb_worker'::regrole OR m.roleid IN (SELECT a.oid FROM agent_login a)))
    UNION ALL
    SELECT 'agent login too strong: '||a.rolname FROM agent_login a
    WHERE a.rolsuper OR a.rolcreaterole OR a.rolcreatedb OR a.rolreplication OR a.rolbypassrls
    UNION ALL
    SELECT 'agent login '||a.rolname||' holds '||o.roleid::regrole::text FROM agent_login a
    JOIN pg_auth_members o ON o.member=a.oid AND o.roleid<>'agentdb_worker'::regrole
    WHERE o.inherit_option OR o.set_option OR o.admin_option
    UNION ALL
    SELECT 'agent login not built by the broker: '||a.rolname FROM agent_login a
    WHERE a.rolcanlogin AND (a.rolvaliduntil IS NULL OR a.rolvaliduntil='infinity' OR a.rolconnlimit<0 OR NOT EXISTS(
      SELECT 1 FROM pg_db_role_setting s WHERE s.setrole=a.oid AND s.setconfig::text LIKE '%temp_file_limit=%'
        AND s.setconfig::text LIKE '%log_statement=all%'))
    UNION ALL
    SELECT 'login outside the boundary holds agentdb_client: '||r.rolname FROM pg_roles r
    WHERE r.rolcanlogin AND NOT r.rolsuper
      AND r.oid<>(SELECT d.datdba FROM pg_database d WHERE d.datname=current_database())
      AND (pg_has_role(r.oid,'agentdb_client','USAGE') OR pg_has_role(r.oid,'agentdb_client','SET'))
      AND has_database_privilege(r.oid,current_database(),'CONNECT')
    UNION ALL
    SELECT 'superuser logins changed' WHERE ARRAY(SELECT r.rolname FROM pg_roles r WHERE r.rolsuper AND r.rolcanlogin ORDER BY r.rolname)
      IS DISTINCT FROM (SELECT g.superuser_logins FROM vc.gate g)
    UNION ALL
    SELECT 'engine functions changed' WHERE vc._engine_fingerprint() IS DISTINCT FROM (SELECT g.engine_fingerprint FROM vc.gate g)
    UNION ALL
    -- A prepared transaction keeps main's lock with no session left for the keeper to end.
    SELECT 'max_prepared_transactions is above 0' WHERE current_setting('max_prepared_transactions')::integer>0
  ) SELECT coalesce(jsonb_agg(problem ORDER BY problem),'[]'::jsonb) FROM found
$$;

-- What anyone needs to know, without changing anything. Other databases that accept any role
-- are listed but do not freeze writes here: an agent login holds no rights in them, and they
-- are not this installation's to change. Test databases are only counted.
CREATE OR REPLACE FUNCTION vc.boundary_status() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,vc,pg_temp AS $$
  WITH open_database AS (
    SELECT d.datname FROM pg_database d
    WHERE d.datallowconn AND d.datname<>current_database() AND (d.datacl IS NULL OR EXISTS(
      SELECT 1 FROM aclexplode(d.datacl) a WHERE a.grantee=0 AND a.privilege_type='CONNECT'))
  )
  SELECT jsonb_build_object(
    'frozen',g.frozen_reason,'frozen_by',g.frozen_by,'frozen_at',g.frozen_at,
    'problems',vc.boundary_problems(),
    'logins',(SELECT coalesce(jsonb_agg(jsonb_build_object('login',r.rolname,'main_rows_left',b.main_rows_left,
        'bytes_left',b.bytes_left,'valid_until',r.rolvaliduntil,
        'sessions',(SELECT count(*) FROM pg_stat_activity a WHERE a.usesysid=r.oid)) ORDER BY r.rolname),'[]'::jsonb)
      FROM vc._agent_roles() x JOIN pg_roles r ON r.oid=x LEFT JOIN vc.agent_budget b ON b.login=r.rolname
      WHERE r.rolcanlogin),
    'open_databases',(SELECT coalesce(jsonb_agg(o.datname ORDER BY o.datname),'[]'::jsonb) FROM open_database o
      WHERE o.datname NOT LIKE 'agentdb\_test\_%'),
    'open_test_databases',(SELECT count(*) FROM open_database o WHERE o.datname LIKE 'agentdb\_test\_%'))
  FROM vc.gate g
$$;

-- One self-check: a problem freezes writes, and the status says why.
CREATE OR REPLACE FUNCTION vc.boundary_check() RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,vc,pg_temp AS $$
DECLARE found jsonb:=vc.boundary_problems();
BEGIN
  IF jsonb_array_length(found)>0 THEN
    PERFORM vc.freeze(left((SELECT string_agg(p,'; ' ORDER BY p) FROM jsonb_array_elements_text(found) p),4000));
  END IF;
  RETURN vc.boundary_status();
END $$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA vc FROM PUBLIC;

-- agent.* holds one view per tracked table, so this covers exactly what is versioned.
DO $grants$
DECLARE t record;
BEGIN
  GRANT USAGE ON SCHEMA vc,agent TO agentdb_worker;
  FOR t IN SELECT c.oid::regclass AS relation,c.relnamespace::regnamespace AS schema
           FROM vc.tracked_table r JOIN pg_class c ON c.oid=r.relation LOOP
    EXECUTE format('GRANT USAGE ON SCHEMA %s TO agentdb_worker',t.schema);
    EXECUTE format('GRANT SELECT ON %s TO agentdb_worker',t.relation);
  END LOOP;
END $grants$;
GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA agent TO agentdb_worker;
GRANT EXECUTE ON FUNCTION vc.head(text),vc.table_id(regclass),vc.checkout(text),vc.pin(uuid),
  vc.rows_at(uuid,uuid),vc.visible_rows(uuid),vc.read_file(uuid,text),vc.diff_rows(uuid,uuid,uuid),
  vc.diff_files(uuid,uuid),vc.history(uuid,integer),vc.merge_base(uuid,uuid),
  vc.write_file(text,bytea,boolean),vc.finish_change(),
  vc.agent_fork(text,uuid),vc.agent_begin(text,uuid,text),vc.agent_merge(text,jsonb),vc.agent_status()
  TO agentdb_worker;

GRANT USAGE ON SCHEMA vc TO agentdb_keeper;
GRANT EXECUTE ON FUNCTION vc.head(text),vc.history(uuid,integer),vc.freeze(text),vc.stop_agent(name),vc.revert(uuid),
  vc.agent_report(name),vc.boundary_check(),vc.boundary_status(),vc.end_stuck_agent_sessions(integer,integer,boolean),vc._agent_roles()
  TO agentdb_keeper;

GRANT USAGE ON SCHEMA vc TO agentdb_broker;
GRANT EXECUTE ON FUNCTION vc.issue_budget(name,bigint,bigint,numeric),vc.stop_agent(name) TO agentdb_broker;

UPDATE vc.gate SET engine_fingerprint=vc._engine_fingerprint(),
  superuser_logins=ARRAY(SELECT rolname FROM pg_roles WHERE rolsuper AND rolcanlogin ORDER BY rolname);
COMMIT;
