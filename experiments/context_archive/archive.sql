-- Context archive prototype (docs/CONTEXT_ARCHIVE_EXPERIMENT.md). Experiment only:
-- run_archive_trials.py, replay.py and the tests install this into their own
-- agentdb_test_* databases as the database owner; db.py install never does.
-- vc.* is neither called nor changed, and nothing is granted to other roles.
--
-- xc.blob     one row per distinct body, keyed by its SHA-256; written once.
--             A body is the exact bytes of one stream-json line or prompt line.
-- xc.entry    one row per entry of a task, numbered 1, 2, 3 ... in the order
--             produced; identical bodies share a blob but stay separate entries
-- xc.append   checks the caller's hash against the body, then adds the task's
--             next entry and returns its number
-- xc.fetch    the task's entries with the given numbers, in number order
BEGIN;
CREATE SCHEMA xc;
REVOKE ALL ON SCHEMA xc FROM PUBLIC;

CREATE TABLE xc.blob(
  hash text PRIMARY KEY CHECK (hash = encode(sha256(body),'hex')),
  body bytea NOT NULL);

CREATE TABLE xc.entry(
  task text NOT NULL,
  seq integer NOT NULL CHECK (seq > 0),
  hash text NOT NULL REFERENCES xc.blob,
  kind text NOT NULL,
  label text NOT NULL,
  bytes integer NOT NULL,
  PRIMARY KEY (task, seq));

CREATE FUNCTION xc.append(p_task text, p_hash text, p_kind text, p_label text, p_body bytea) RETURNS integer
LANGUAGE plpgsql SET search_path=pg_catalog,xc,pg_temp AS $$
DECLARE
  n integer;
BEGIN
  -- Checked here as well as by the CHECK: when the blob already exists the
  -- insert below does nothing, and the entry must not point at other bytes.
  IF p_hash IS DISTINCT FROM encode(sha256(p_body),'hex') THEN
    RAISE EXCEPTION 'Hash does not match the body: %',p_hash USING ERRCODE='22023';
  END IF;
  INSERT INTO xc.blob VALUES (p_hash,p_body) ON CONFLICT (hash) DO NOTHING;
  -- One writer per task in this experiment; the lock keeps numbers gap-free anyway.
  PERFORM pg_advisory_xact_lock(hashtext('xc.entry'),hashtext(p_task));
  SELECT coalesce(max(seq),0)+1 INTO n FROM xc.entry WHERE task=p_task;
  INSERT INTO xc.entry VALUES (p_task,n,p_hash,p_kind,p_label,octet_length(p_body));
  RETURN n;
END $$;

CREATE FUNCTION xc.fetch(p_task text, p_seqs integer[])
RETURNS TABLE(seq integer, kind text, label text, body bytea)
LANGUAGE sql STABLE SET search_path=pg_catalog,xc,pg_temp AS $$
  SELECT e.seq,e.kind,e.label,b.body FROM xc.entry e JOIN xc.blob b ON b.hash=e.hash
  WHERE e.task=p_task AND e.seq=ANY(p_seqs) ORDER BY e.seq;
$$;

REVOKE ALL ON FUNCTION xc.append(text,text,text,text,bytea) FROM PUBLIC;
REVOKE ALL ON FUNCTION xc.fetch(text,integer[]) FROM PUBLIC;
COMMIT;
