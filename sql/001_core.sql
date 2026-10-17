-- Install into a NEW, dedicated PostgreSQL 16+ database as its owner.
-- Versioned files + ordinary main tables + SQL-visible isolated branch overlays.
BEGIN;
CREATE SCHEMA vc;
CREATE SCHEMA agent;

CREATE TABLE vc.changeset (
  id uuid PRIMARY KEY,
  author text NOT NULL,
  message text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE vc.parent (
  child uuid NOT NULL REFERENCES vc.changeset,
  ordinal smallint NOT NULL CHECK (ordinal > 0),
  parent uuid NOT NULL REFERENCES vc.changeset,
  PRIMARY KEY (child, ordinal), UNIQUE(child, parent), CHECK(child <> parent)
);
CREATE INDEX ON vc.parent(parent);
CREATE TABLE vc.branch (
  name text PRIMARY KEY CHECK (length(name) BETWEEN 1 AND 128),
  head uuid NOT NULL REFERENCES vc.changeset,
  base uuid NOT NULL REFERENCES vc.changeset
);
CREATE TABLE vc.blob (
  hash bytea PRIMARY KEY,
  content bytea NOT NULL,
  CHECK(hash = sha256(content))
);
CREATE TABLE vc.tracked_table (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  relation regclass NOT NULL UNIQUE,
  pk_columns text[] NOT NULL,
  schema_version uuid NOT NULL DEFAULT gen_random_uuid(),
  schema_description jsonb NOT NULL
);
CREATE TABLE vc.path_change (
  changeset uuid NOT NULL REFERENCES vc.changeset,
  path text NOT NULL,
  blob bytea REFERENCES vc.blob,
  executable boolean NOT NULL DEFAULT false,
  PRIMARY KEY (changeset, path)
);
CREATE INDEX ON vc.path_change(path, changeset);
CREATE TABLE vc.row_change (
  changeset uuid NOT NULL REFERENCES vc.changeset,
  table_id uuid NOT NULL REFERENCES vc.tracked_table,
  schema_version uuid NOT NULL,
  pk jsonb NOT NULL CHECK(jsonb_typeof(pk) = 'object'),
  op char(1) NOT NULL CHECK(op IN ('I','U','D')),
  before_row jsonb, after_row jsonb,
  PRIMARY KEY (changeset, table_id, pk),
  CHECK(before_row IS DISTINCT FROM after_row),
  CHECK(before_row IS NULL OR jsonb_typeof(before_row) = 'object'),
  CHECK(after_row IS NULL OR jsonb_typeof(after_row) = 'object')
);
CREATE INDEX ON vc.row_change(table_id, pk, changeset);

-- Drafts exist only inside the originating SQL transaction, never as published history.
CREATE TABLE vc.work_tx (
  tx bigint PRIMARY KEY,
  revision uuid NOT NULL UNIQUE,
  branch text NOT NULL REFERENCES vc.branch,
  base uuid NOT NULL REFERENCES vc.changeset,
  extra_parent uuid REFERENCES vc.changeset,
  author text NOT NULL, message text NOT NULL
);
CREATE TABLE vc.pending_row (
  tx bigint NOT NULL REFERENCES vc.work_tx ON DELETE CASCADE,
  table_id uuid NOT NULL REFERENCES vc.tracked_table,
  pk jsonb NOT NULL,
  before_row jsonb, after_row jsonb,
  PRIMARY KEY(tx, table_id, pk)
);
CREATE TABLE vc.pending_path (
  tx bigint NOT NULL REFERENCES vc.work_tx ON DELETE CASCADE,
  path text NOT NULL,
  before_state jsonb, after_state jsonb,
  PRIMARY KEY(tx, path)
);
CREATE TABLE vc.snapshot_manifest (
  changeset uuid NOT NULL REFERENCES vc.changeset,
  table_id uuid NOT NULL REFERENCES vc.tracked_table,
  PRIMARY KEY(changeset, table_id)
);
CREATE TABLE vc.snapshot_row (
  changeset uuid NOT NULL,
  table_id uuid NOT NULL,
  pk jsonb NOT NULL, doc jsonb NOT NULL,
  PRIMARY KEY(changeset, table_id, pk),
  FOREIGN KEY(changeset, table_id) REFERENCES vc.snapshot_manifest
    DEFERRABLE INITIALLY DEFERRED
);

INSERT INTO vc.changeset(id, author, message)
VALUES ('00000000-0000-0000-0000-000000000001', 'system', 'genesis');
INSERT INTO vc.branch VALUES ('main', '00000000-0000-0000-0000-000000000001',
                             '00000000-0000-0000-0000-000000000001');

CREATE FUNCTION vc._immutable() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,vc AS $$
BEGIN RAISE EXCEPTION 'Published version data is immutable' USING ERRCODE='55000'; END $$;
DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['changeset','parent','blob','path_change','row_change',
                           'tracked_table','snapshot_manifest','snapshot_row'] LOOP
    EXECUTE format('CREATE TRIGGER immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON vc.%I '
                   'FOR EACH STATEMENT EXECUTE FUNCTION vc._immutable()', t);
  END LOOP;
END $$;

CREATE FUNCTION vc.head(p_branch text DEFAULT 'main') RETURNS uuid
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
DECLARE result uuid;
BEGIN
  SELECT head INTO result FROM vc.branch WHERE name=p_branch;
  IF NOT FOUND THEN RAISE EXCEPTION 'Unknown branch: %',p_branch; END IF;
  RETURN result;
END $$;

CREATE FUNCTION vc.table_id(p_table regclass) RETURNS uuid
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
DECLARE result uuid;
BEGIN
  SELECT id INTO result FROM vc.tracked_table WHERE relation=p_table;
  IF NOT FOUND THEN RAISE EXCEPTION 'Table is not registered: %',p_table; END IF;
  RETURN result;
END $$;

CREATE FUNCTION vc.fork(p_name text, p_revision uuid) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
BEGIN
  INSERT INTO vc.branch VALUES(p_name,p_revision,p_revision);
  RETURN p_revision;
END $$;

CREATE FUNCTION vc.checkout(p_branch text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
DECLARE result uuid := vc.head(p_branch);
BEGIN
  IF EXISTS(SELECT 1 FROM vc.work_tx WHERE tx=txid_current()) THEN
    RAISE EXCEPTION 'Cannot change read revision during a write';
  END IF;
  PERFORM set_config('vc.read_revision',result::text,true);
  RETURN result;
END $$;

CREATE FUNCTION vc.pin(p_revision uuid) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
BEGIN
  IF NOT EXISTS(SELECT 1 FROM vc.changeset WHERE id=p_revision) THEN
    RAISE EXCEPTION 'Unknown revision';
  END IF;
  IF EXISTS(SELECT 1 FROM vc.work_tx WHERE tx=txid_current()) THEN
    RAISE EXCEPTION 'Cannot change read revision during a write';
  END IF;
  PERFORM set_config('vc.read_revision',p_revision::text,true);
  RETURN p_revision;
END $$;

CREATE FUNCTION vc.begin_change(p_branch text, p_expected uuid,
                               p_author text DEFAULT 'agent', p_message text DEFAULT '')
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
DECLARE current_head uuid; result uuid := gen_random_uuid();
BEGIN
  IF EXISTS(SELECT 1 FROM vc.work_tx WHERE tx=txid_current()) THEN
    RAISE EXCEPTION 'A changeset is already open in this transaction';
  END IF;
  SELECT head INTO current_head FROM vc.branch WHERE name=p_branch FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Unknown branch'; END IF;
  IF current_head IS DISTINCT FROM p_expected THEN
    RAISE EXCEPTION 'Stale head: expected %, actual %',p_expected,current_head
      USING ERRCODE='40001';
  END IF;
  INSERT INTO vc.work_tx(tx,revision,branch,base,author,message)
  VALUES(txid_current(),result,p_branch,current_head,p_author,p_message);
  PERFORM set_config('vc.read_revision',current_head::text,true);
  RETURN result;
END $$;

CREATE FUNCTION vc._unfinished() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
BEGIN
  IF EXISTS(SELECT 1 FROM vc.work_tx WHERE tx=NEW.tx) THEN
    RAISE EXCEPTION 'finish_change() is required before COMMIT' USING ERRCODE='23514';
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER finish_required AFTER INSERT ON vc.work_tx
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION vc._unfinished();

CREATE FUNCTION vc._context() RETURNS vc.work_tx
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
DECLARE result vc.work_tx;
BEGIN
  SELECT * INTO result FROM vc.work_tx WHERE tx=txid_current();
  IF NOT FOUND THEN RAISE EXCEPTION 'Call begin_change() inside BEGIN/COMMIT first'; END IF;
  RETURN result;
END $$;

CREATE FUNCTION vc._pk(p_table uuid, p_row jsonb) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
  SELECT jsonb_object_agg(k,p_row->k)
  FROM vc.tracked_table t, unnest(t.pk_columns) AS k WHERE t.id=p_table
$$;

CREATE FUNCTION vc.rows_at(p_revision uuid,p_table uuid)
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
    SELECT p.parent,l.depth+1 FROM lineage l JOIN vc.parent p ON p.child=l.id AND p.ordinal=1
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

CREATE FUNCTION vc.visible_rows(p_table uuid) RETURNS TABLE(pk jsonb,doc jsonb)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
DECLARE base_id uuid; current_tx bigint := txid_current();
BEGIN
  SELECT base INTO base_id FROM vc.work_tx WHERE tx=current_tx;
  IF NOT FOUND THEN
    base_id := nullif(current_setting('vc.read_revision',true),'')::uuid;
    IF base_id IS NULL THEN RAISE EXCEPTION 'Pin a revision with checkout() or pin() inside a transaction'; END IF;
  END IF;
  RETURN QUERY
    SELECT r.pk,r.doc FROM vc.rows_at(base_id,p_table) r
    WHERE NOT EXISTS(SELECT 1 FROM vc.pending_row p
                     WHERE p.tx=current_tx AND p.table_id=p_table AND p.pk=r.pk)
    UNION ALL
    SELECT p.pk,p.after_row FROM vc.pending_row p
    WHERE p.tx=current_tx AND p.table_id=p_table AND p.after_row IS NOT NULL;
END $$;

CREATE FUNCTION vc._stage(p_table uuid,p_pk jsonb,p_before jsonb,p_after jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
DECLARE ctx vc.work_tx := vc._context();
BEGIN
  INSERT INTO vc.pending_row VALUES(ctx.tx,p_table,p_pk,p_before,p_after)
  ON CONFLICT(tx,table_id,pk) DO UPDATE SET after_row=EXCLUDED.after_row;
  DELETE FROM vc.pending_row WHERE tx=ctx.tx AND table_id=p_table AND pk=p_pk
    AND before_row IS NOT DISTINCT FROM after_row;
END $$;

CREATE FUNCTION vc._capture_main() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
DECLARE ctx vc.work_tx := vc._context(); tid uuid := TG_ARGV[0]::uuid;
        old_doc jsonb; new_doc jsonb; old_pk jsonb; new_pk jsonb;
BEGIN
  IF ctx.branch <> 'main' THEN RAISE EXCEPTION 'Write branch data through agent views'; END IF;
  IF TG_OP <> 'INSERT' THEN old_doc:=to_jsonb(OLD); old_pk:=vc._pk(tid,old_doc); END IF;
  IF TG_OP <> 'DELETE' THEN new_doc:=to_jsonb(NEW); new_pk:=vc._pk(tid,new_doc); END IF;
  IF TG_OP='UPDATE' AND old_pk IS DISTINCT FROM new_pk THEN
    PERFORM vc._stage(tid,old_pk,old_doc,NULL);
    PERFORM vc._stage(tid,new_pk,NULL,new_doc);
  ELSE
    PERFORM vc._stage(tid,coalesce(new_pk,old_pk),old_doc,new_doc);
  END IF;
  RETURN NULL;
END $$;

CREATE FUNCTION vc._overlay_write() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
DECLARE ctx vc.work_tx := vc._context(); tid uuid := TG_ARGV[0]::uuid;
        old_doc jsonb; new_doc jsonb; old_pk jsonb; new_pk jsonb;
BEGIN
  IF ctx.branch='main' THEN RAISE EXCEPTION 'Write main through ordinary app tables'; END IF;
  IF TG_OP<>'INSERT' THEN old_doc:=to_jsonb(OLD); old_pk:=vc._pk(tid,old_doc); END IF;
  IF TG_OP<>'DELETE' THEN
    new_doc:=to_jsonb(NEW); new_pk:=vc._pk(tid,new_doc);
    IF EXISTS(SELECT 1 FROM jsonb_each(new_pk) e WHERE e.value='null'::jsonb) THEN
      RAISE EXCEPTION 'Primary key cannot be null' USING ERRCODE='23502';
    END IF;
  END IF;
  IF TG_OP='INSERT' OR (TG_OP='UPDATE' AND old_pk IS DISTINCT FROM new_pk) THEN
    IF EXISTS(SELECT 1 FROM vc.visible_rows(tid) r WHERE r.pk=new_pk) THEN
      RAISE EXCEPTION 'Duplicate branch primary key' USING ERRCODE='23505';
    END IF;
  END IF;
  IF TG_OP='UPDATE' AND old_pk IS DISTINCT FROM new_pk THEN
    PERFORM vc._stage(tid,old_pk,old_doc,NULL);
    PERFORM vc._stage(tid,new_pk,NULL,new_doc);
  ELSE
    PERFORM vc._stage(tid,coalesce(new_pk,old_pk),old_doc,new_doc);
  END IF;
  IF TG_OP='DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;

-- Owner-only registration. Only empty ordinary tables, fixed schema, no generated/identity columns.
CREATE FUNCTION vc.register_table(p_relation regclass) RETURNS uuid
LANGUAGE plpgsql SET search_path=pg_catalog,vc AS $$
DECLARE tid uuid; keys text[]; description jsonb; nonempty boolean; view_name text;
BEGIN
  IF (SELECT relkind FROM pg_class WHERE oid=p_relation)<>'r' THEN
    RAISE EXCEPTION 'Only ordinary tables are supported';
  END IF;
  EXECUTE format('SELECT EXISTS(SELECT 1 FROM %s)',p_relation) INTO nonempty;
  IF nonempty THEN RAISE EXCEPTION 'Register an empty table before its initial versioned import'; END IF;
  IF EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid=p_relation
            AND (attgenerated<>'' OR attidentity<>'')) THEN
    RAISE EXCEPTION 'Generated/identity columns are not supported in this version';
  END IF;
  SELECT array_agg(a.attname ORDER BY k.ord) INTO keys
  FROM pg_index i CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum,ord)
  JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum
  WHERE i.indrelid=p_relation AND i.indisprimary AND k.ord<=i.indnkeyatts;
  IF keys IS NULL THEN RAISE EXCEPTION 'A primary key is required'; END IF;
  SELECT jsonb_agg(jsonb_build_object('name',attname,'type',format_type(atttypid,atttypmod),
                                    'not_null',attnotnull) ORDER BY attnum) INTO description
  FROM pg_attribute WHERE attrelid=p_relation AND attnum>0 AND NOT attisdropped;
  INSERT INTO vc.tracked_table(relation,pk_columns,schema_description)
  VALUES(p_relation,keys,description) RETURNING id INTO tid;
  SELECT relname INTO view_name FROM pg_class WHERE oid=p_relation;
  EXECUTE format('CREATE TRIGGER vc_capture AFTER INSERT OR UPDATE OR DELETE ON %s '
                 'FOR EACH ROW EXECUTE FUNCTION vc._capture_main(%L)',p_relation,tid);
  EXECUTE format('CREATE TRIGGER vc_no_truncate BEFORE TRUNCATE ON %s '
                 'FOR EACH STATEMENT EXECUTE FUNCTION vc._immutable()',p_relation);
  EXECUTE format('CREATE VIEW agent.%I AS SELECT (jsonb_populate_record(NULL::%s,r.doc)).* '
                 'FROM vc.visible_rows(%L::uuid) r',view_name,p_relation,tid);
  EXECUTE format('CREATE TRIGGER vc_overlay INSTEAD OF INSERT OR UPDATE OR DELETE ON agent.%I '
                 'FOR EACH ROW EXECUTE FUNCTION vc._overlay_write(%L)',view_name,tid);
  RETURN tid;
END $$;

CREATE FUNCTION vc._file_state(p_revision uuid,p_path text) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
  WITH RECURSIVE lineage(id,depth) AS (
    SELECT p_revision,0 UNION ALL SELECT p.parent,l.depth+1 FROM lineage l
    JOIN vc.parent p ON p.child=l.id AND p.ordinal=1
  ) SELECT CASE WHEN c.blob IS NULL THEN NULL ELSE
      jsonb_build_object('hash',encode(c.blob,'hex'),'executable',c.executable) END
    FROM lineage l JOIN vc.path_change c ON c.changeset=l.id AND c.path=p_path
    ORDER BY l.depth LIMIT 1
$$;

CREATE FUNCTION vc.write_file(p_path text,p_content bytea,p_executable boolean DEFAULT false)
RETURNS bytea LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
DECLARE ctx vc.work_tx := vc._context(); digest bytea; state jsonb;
BEGIN
  IF p_path IS NULL OR p_path='' OR p_path LIKE '/%' OR position(E'\\' IN p_path)>0
     OR p_path ~ '(^|/)(\.|\.\.)(/|$)' OR p_path LIKE '%//%' OR p_path LIKE '%/' THEN
    RAISE EXCEPTION 'Use a nonempty relative POSIX path';
  END IF;
  IF p_content IS NOT NULL THEN
    digest:=sha256(p_content);
    INSERT INTO vc.blob VALUES(digest,p_content) ON CONFLICT DO NOTHING;
    state:=jsonb_build_object('hash',encode(digest,'hex'),'executable',p_executable);
  END IF;
  INSERT INTO vc.pending_path VALUES(ctx.tx,p_path,vc._file_state(ctx.base,p_path),state)
  ON CONFLICT(tx,path) DO UPDATE SET after_state=EXCLUDED.after_state;
  DELETE FROM vc.pending_path WHERE tx=ctx.tx AND path=p_path
    AND before_state IS NOT DISTINCT FROM after_state;
  RETURN digest;
END $$;

CREATE FUNCTION vc.read_file(p_revision uuid,p_path text) RETURNS bytea
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
DECLARE state jsonb;
BEGIN
  IF NOT EXISTS(SELECT 1 FROM vc.changeset WHERE id=p_revision) THEN
    RAISE EXCEPTION 'Unknown revision';
  END IF;
  state:=vc._file_state(p_revision,p_path);
  RETURN (SELECT content FROM vc.blob WHERE hash=decode(state->>'hash','hex'));
END $$;

CREATE FUNCTION vc.finish_change() RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
DECLARE ctx vc.work_tx := vc._context(); constraint_list text;
BEGIN
  IF ctx.branch='main' THEN
    SELECT string_agg(format('%I.%I',n.nspname,c.conname),',') INTO constraint_list
    FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace
    WHERE c.condeferrable AND c.conrelid IN(SELECT relation FROM vc.tracked_table);
    IF constraint_list IS NOT NULL THEN EXECUTE 'SET CONSTRAINTS '||constraint_list||' IMMEDIATE'; END IF;
  END IF;
  INSERT INTO vc.changeset(id,author,message) VALUES(ctx.revision,ctx.author,ctx.message);
  INSERT INTO vc.parent VALUES(ctx.revision,1,ctx.base);
  IF ctx.extra_parent IS NOT NULL AND ctx.extra_parent<>ctx.base THEN
    INSERT INTO vc.parent VALUES(ctx.revision,2,ctx.extra_parent);
  END IF;
  INSERT INTO vc.path_change
    SELECT ctx.revision,p.path,decode(p.after_state->>'hash','hex'),
           coalesce((p.after_state->>'executable')::boolean,false)
    FROM vc.pending_path p WHERE p.tx=ctx.tx;
  INSERT INTO vc.row_change
    SELECT ctx.revision,p.table_id,t.schema_version,p.pk,
      CASE WHEN p.before_row IS NULL THEN 'I' WHEN p.after_row IS NULL THEN 'D' ELSE 'U' END,
      p.before_row,p.after_row
    FROM vc.pending_row p JOIN vc.tracked_table t ON t.id=p.table_id WHERE p.tx=ctx.tx;
  UPDATE vc.branch SET head=ctx.revision WHERE name=ctx.branch AND head=ctx.base;
  IF NOT FOUND THEN RAISE EXCEPTION 'Stale head' USING ERRCODE='40001'; END IF;
  DELETE FROM vc.work_tx WHERE tx=ctx.tx;
  PERFORM set_config('vc.read_revision',ctx.revision::text,true);
  RETURN ctx.revision;
END $$;

CREATE FUNCTION vc.materialize(p_revision uuid,p_table uuid) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
DECLARE total bigint;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtextextended(p_revision::text||p_table::text,0));
  IF NOT EXISTS(SELECT 1 FROM vc.snapshot_manifest WHERE changeset=p_revision AND table_id=p_table) THEN
    INSERT INTO vc.snapshot_row SELECT p_revision,p_table,r.pk,r.doc FROM vc.rows_at(p_revision,p_table) r;
    INSERT INTO vc.snapshot_manifest VALUES(p_revision,p_table);
  END IF;
  SELECT count(*) INTO total FROM vc.snapshot_row WHERE changeset=p_revision AND table_id=p_table;
  RETURN total;
END $$;

CREATE FUNCTION vc.diff_rows(p_before uuid,p_after uuid,p_table uuid)
RETURNS TABLE(pk jsonb,before_row jsonb,after_row jsonb)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
  SELECT coalesce(a.pk,b.pk),a.doc,b.doc
  FROM vc.rows_at(p_before,p_table) a FULL JOIN vc.rows_at(p_after,p_table) b USING(pk)
  WHERE a.doc IS DISTINCT FROM b.doc
$$;

CREATE FUNCTION vc._ancestors(p_start uuid) RETURNS TABLE(id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
  WITH RECURSIVE a(id) AS (
    SELECT p_start UNION SELECT p.parent FROM a JOIN vc.parent p ON p.child=a.id
  ) SELECT a.id FROM a
$$;

CREATE FUNCTION vc.diff_files(p_before uuid,p_after uuid)
RETURNS TABLE(path text,before_state jsonb,after_state jsonb)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
BEGIN
  IF (SELECT count(*) FROM vc.changeset WHERE id IN(p_before,p_after)) <
     (CASE WHEN p_before=p_after THEN 1 ELSE 2 END) THEN RAISE EXCEPTION 'Unknown revision'; END IF;
  RETURN QUERY SELECT p.path,vc._file_state(p_before,p.path),vc._file_state(p_after,p.path)
    FROM (SELECT DISTINCT c.path FROM vc.path_change c) p
    WHERE vc._file_state(p_before,p.path) IS DISTINCT FROM vc._file_state(p_after,p.path);
END $$;

CREATE FUNCTION vc.history(p_revision uuid,p_limit integer DEFAULT 50)
RETURNS TABLE(id uuid,parents uuid[],author text,message text,created_at timestamptz)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
BEGIN
  IF p_limit NOT BETWEEN 1 AND 1000 THEN RAISE EXCEPTION 'History limit must be 1..1000'; END IF;
  IF NOT EXISTS(SELECT 1 FROM vc.changeset c WHERE c.id=p_revision) THEN RAISE EXCEPTION 'Unknown revision'; END IF;
  RETURN QUERY WITH RECURSIVE lineage(revision,depth) AS (
    SELECT p_revision,0 UNION ALL SELECT p.parent,l.depth+1 FROM lineage l
    JOIN vc.parent p ON p.child=l.revision AND p.ordinal=1 WHERE l.depth+1<p_limit
  ) SELECT c.id,ARRAY(SELECT p.parent FROM vc.parent p WHERE p.child=c.id ORDER BY p.ordinal),
      c.author,c.message,c.created_at FROM lineage l JOIN vc.changeset c ON c.id=l.revision
    ORDER BY l.depth;
END $$;

CREATE FUNCTION vc.merge_base(p_a uuid,p_b uuid) RETURNS uuid
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
DECLARE candidates uuid[];
BEGIN
  WITH common AS (SELECT id FROM vc._ancestors(p_a) INTERSECT SELECT id FROM vc._ancestors(p_b))
  SELECT array_agg(c.id) INTO candidates FROM common c WHERE NOT EXISTS(
    SELECT 1 FROM vc.parent p JOIN common child ON child.id=p.child WHERE p.parent=c.id);
  IF coalesce(cardinality(candidates),0)<>1 THEN
    RAISE EXCEPTION 'A unique merge base is required';
  END IF;
  RETURN candidates[1];
END $$;

CREATE FUNCTION vc._apply_main(p_table uuid,p_pk jsonb,p_after jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
DECLARE t vc.tracked_table; predicate text; key_list text; assignments text;
BEGIN
  SELECT * INTO STRICT t FROM vc.tracked_table WHERE id=p_table;
  SELECT string_agg(format('target.%I=(jsonb_populate_record(NULL::%s,$1)).%I',k,t.relation,k),' AND '),
         string_agg(format('%I',k),',') INTO predicate,key_list FROM unnest(t.pk_columns) k;
  IF p_after IS NULL THEN
    EXECUTE format('DELETE FROM %s target WHERE %s',t.relation,predicate) USING p_pk;
  ELSE
    SELECT string_agg(format('%I=EXCLUDED.%I',attname,attname),',') INTO assignments
    FROM pg_attribute WHERE attrelid=t.relation AND attnum>0 AND NOT attisdropped
      AND NOT(attname=ANY(t.pk_columns));
    EXECUTE format('INSERT INTO %s SELECT (jsonb_populate_record(NULL::%s,$1)).* '
                   'ON CONFLICT (%s) %s',t.relation,t.relation,key_list,
                   CASE WHEN assignments IS NULL THEN 'DO NOTHING' ELSE 'DO UPDATE SET '||assignments END)
    USING p_after;
  END IF;
END $$;

CREATE FUNCTION vc.merge(p_target text,p_source uuid,p_expected uuid,p_author text DEFAULT 'agent')
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,vc AS $$
DECLARE base_id uuid; ctx vc.work_tx; t record; d record; ours jsonb;
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
    FOR d IN SELECT * FROM vc.diff_rows(base_id,p_source,t.id) LOOP
      SELECT r.doc INTO ours FROM vc.rows_at(ctx.base,t.id) r WHERE r.pk=d.pk;
      IF ours IS NOT DISTINCT FROM d.after_row THEN CONTINUE; END IF;
      IF ours IS DISTINCT FROM d.before_row THEN
        RAISE EXCEPTION 'Row conflict: table %, key %',t.id,d.pk USING ERRCODE='P0001';
      END IF;
      IF p_target='main' THEN PERFORM vc._apply_main(t.id,d.pk,d.after_row);
      ELSE PERFORM vc._stage(t.id,d.pk,ours,d.after_row); END IF;
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

-- Internal functions and history tables are not a client write surface.
REVOKE ALL ON SCHEMA vc,agent FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA vc FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA vc FROM PUBLIC;
COMMIT;
