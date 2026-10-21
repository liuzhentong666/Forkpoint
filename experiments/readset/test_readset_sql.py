"""Contract tests for the xr prototype (readset.sql) on the existing PostgreSQL server.

  python -m unittest discover -s experiments/readset -p test_readset_sql.py -v

Builds its own agentdb_test_xrcontract_* database the way run_readset.py builds its
template (install, the 4,000-row seed, bundle stock 60 on rows 3001-3010,
readset.sql) and drops it at the end. Each scenario runs as agentdb_client in one
transaction that is rolled back; the lock-timeout case needs a second session.
The tests record what the prototype does now, including the behaviour listed as
known issues in docs/READSET_EXPERIMENT.md: a test that has to change means the
prototype's behaviour changed.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import time
import unittest
import uuid

from db import sqlstate
from run_readset import connect, prepare_template

ARGS = argparse.Namespace(config=None, password=False, role=None)
DECLARE = """DECLARE b uuid; r uuid; s uuid; p uuid; q uuid; h uuid; m uuid;
  tid uuid := vc.table_id('app.products'); state text; msg text; detail text; hint text;"""


def reads(*ids, table="tid::text"):
    keys = ",".join(f"jsonb_build_object('id',{i})" for i in ids)
    return f"jsonb_build_object({table},jsonb_build_array({keys}))"


def proposal(var, branch, update):
    """A one-commit proposal forked from b on a new branch, as run_readset's writers make them."""
    return (f"PERFORM vc.fork('{branch}',b);\nPERFORM vc.begin_change('{branch}',b);\n"
            f"UPDATE agent.products SET {update};\n{var}:=vc.finish_change();\n")


def on_main(update):
    return f"PERFORM vc.begin_change('main',vc.head());\nUPDATE app.products SET {update};\nPERFORM vc.finish_change();\n"


def expect(call, code):
    """Run call and require it to fail with code; leaves state, msg, detail and hint set."""
    return f"""BEGIN
  PERFORM {call};
  RAISE EXCEPTION 'no error, expected {code}' USING ERRCODE='XX000';
EXCEPTION WHEN OTHERS THEN
  GET STACKED DIAGNOSTICS state=RETURNED_SQLSTATE, msg=MESSAGE_TEXT, detail=PG_EXCEPTION_DETAIL, hint=PG_EXCEPTION_HINT;
  ASSERT state='{code}', format('expected {code}, got %s: %s', state, msg);
END;
"""


class XrContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = "agentdb_test_xrcontract_" + uuid.uuid4().hex[:8]
        cls.owner = connect(ARGS, cls.name)
        cls.template = prepare_template(ARGS, cls.owner, cls.name)
        cls.client = connect(ARGS, cls.name, "agentdb_client")

    @classmethod
    def tearDownClass(cls):
        cls.owner.sql(f'DROP DATABASE "{cls.name}" WITH (FORCE);', cls.owner.settings["maintenance_database"])

    def scenario(self, body):
        """body runs as agentdb_client in a DO block; the transaction is rolled back."""
        result = self.client.execute(f"BEGIN;\nDO $t$\n{DECLARE}\nBEGIN\nb:=vc.head();\n{body}\nEND $t$;\nROLLBACK;")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_first_merge_validates_nothing_and_lands(self):
        self.scenario(proposal("r", "a", "stock=45 WHERE id=3001") + f"""
          h:=xr.merge_validated('main',r,{reads(3002)},'t');
          ASSERT h<>b AND vc.head()=h AND vc.merge_base(h,r)=r;
          ASSERT (SELECT stock FROM app.products WHERE id=3001)=45;""")

    def test_changed_read_key_raises_rs001_with_old_and_new_values(self):
        self.scenario(proposal("r", "a", "stock=45 WHERE id=3001") + proposal("s", "c", "stock=45 WHERE id=3002") + f"""
          h:=xr.merge_validated('main',r,{reads(3002)},'t');
          {expect(f"xr.merge_validated('main',s,{reads(3001, 3005)},'t')", "RS001")}
          ASSERT vc.head()=h, 'main moved after RS001';
          ASSERT (SELECT stock FROM app.products WHERE id=3002)=60;
          ASSERT jsonb_array_length(detail::jsonb)=1, detail;
          ASSERT (detail::jsonb)->0->'key'='{{"id": 3001}}'::jsonb, detail;
          ASSERT (detail::jsonb)->0->>'table'=tid::text, detail;
          ASSERT ((detail::jsonb)->0->'base'->>'stock')::int=60 AND ((detail::jsonb)->0->'now'->>'stock')::int=45, detail;
          ASSERT hint ~ '^validated_at=[0-9.]+$', hint;""")

    def test_unchanged_read_key_lands_although_main_moved(self):
        self.scenario(proposal("r", "a", "stock=45 WHERE id=3001") + proposal("s", "c", "stock=45 WHERE id=3002") + f"""
          h:=xr.merge_validated('main',r,{reads(3002)},'t');
          m:=xr.merge_validated('main',s,{reads(3005)},'t');
          ASSERT m<>h AND vc.head()=m AND vc.merge_base(m,s)=s AND vc.merge_base(m,r)=r;
          ASSERT (SELECT sum(stock) FROM app.products WHERE id IN (3001,3002))=90;""")

    def test_row_changed_and_changed_back_is_not_a_conflict(self):
        self.scenario(proposal("s", "c", "stock=45 WHERE id=3004") + on_main("stock=50 WHERE id=3003")
                      + on_main("stock=60 WHERE id=3003") + f"""
          m:=xr.merge_validated('main',s,{reads(3003)},'t');
          ASSERT vc.merge_base(m,s)=s;""")

    def test_empty_read_sets_skip_validation(self):
        self.scenario(proposal("r", "a", "stock=45 WHERE id=3001") + proposal("s", "c", "stock=45 WHERE id=3002")
                      + proposal("p", "d", "stock=45 WHERE id=3003") + """
          h:=xr.merge_validated('main',r,'{}'::jsonb,'t');
          m:=xr.merge_validated('main',s,'{}'::jsonb,'t');
          m:=xr.merge_validated('main',p,jsonb_build_object(tid::text,'[]'::jsonb),'t');
          ASSERT vc.merge_base(m,s)=s AND vc.merge_base(m,p)=p;""")

    def test_write_conflict_is_still_p0001_from_vc_merge(self):
        self.scenario(proposal("r", "a", "stock=45 WHERE id=3006") + proposal("s", "c", "stock=30 WHERE id=3006") + f"""
          h:=xr.merge_validated('main',r,'{{}}'::jsonb,'t');
          {expect("xr.merge_validated('main',s,'{}'::jsonb,'t')", "P0001")}
          ASSERT msg LIKE 'Row conflict:%', msg;
          ASSERT vc.head()=h;""")

    def test_read_conflict_is_reported_before_a_write_conflict(self):
        # run_readset never puts written keys in a read set; this records what happens if a caller does.
        self.scenario(proposal("r", "a", "stock=45 WHERE id=3006") + proposal("s", "c", "stock=30 WHERE id=3006") + f"""
          h:=xr.merge_validated('main',r,'{{}}'::jsonb,'t');
          {expect(f"xr.merge_validated('main',s,{reads(3006)},'t')", "RS001")}""")

    def test_malformed_read_sets_raise_p0001_known_issue(self):
        # Known issue: P0001 is also vc.merge's row/file conflict code, so a caller cannot tell them apart by SQLSTATE.
        message = "p_reads must map table ids to arrays of primary keys"
        body = proposal("r", "a", "stock=45 WHERE id=3001")
        for bad in ("'[]'::jsonb", "NULL::jsonb", "jsonb_build_object(tid::text,jsonb_build_object('id',3002))"):
            body += expect(f"xr.merge_validated('main',r,{bad},'t')", "P0001") + f"ASSERT msg='{message}', msg;\n"
        self.scenario(body + "ASSERT vc.head()=b;")

    def test_unknown_table_is_ignored_until_main_moves_known_issue(self):
        # Known issue: with base = head nothing is validated, so a wrong table id passes silently.
        self.scenario(proposal("r", "a", "stock=45 WHERE id=3001") + proposal("s", "c", "stock=45 WHERE id=3002") + f"""
          h:=xr.merge_validated('main',r,{reads(3002, table="gen_random_uuid()::text")},'t');
          ASSERT vc.head()=h;
          {expect(f"xr.merge_validated('main',s,{reads(3001, table=chr(39) + '00000000-0000-0000-0000-0000000000aa' + chr(39))},'t')", "P0001")}
          ASSERT msg='Unknown revision or table', msg;
          {expect(f"xr.merge_validated('main',s,{reads(3001, table=chr(39) + 'products' + chr(39))},'t')", "22P02")}
          ASSERT vc.head()=h;""")

    def test_merge_head_keeps_the_stale_head_check(self):
        self.scenario(proposal("r", "a", "stock=45 WHERE id=3001") + proposal("s", "c", "stock=45 WHERE id=3002") + f"""
          h:=xr.merge_head('main',r,b,'t');
          {expect("xr.merge_head('main',s,b,'t')", "40001")}
          ASSERT vc.head()=h;
          m:=xr.merge_head('main',s,h,'t');
          ASSERT vc.merge_base(m,s)=s;""")

    def test_lock_main_rejects_an_unknown_branch(self):
        self.scenario(expect("xr.lock_main('no-such-branch')", "P0001") + "ASSERT msg='Unknown branch: no-such-branch', msg;")

    def test_queued_merge_fails_with_lock_timeout_and_leaves_main_unchanged(self):
        base = self.owner.sql("SELECT vc.head();")
        revision = self.client.sql(f"""SELECT vc.fork('lock-check','{base}');
BEGIN;
SELECT vc.begin_change('lock-check','{base}','t','lock check') AS draft \\gset
UPDATE agent.products SET stock=98 WHERE id=3999;
SELECT vc.finish_change() AS revision \\gset
COMMIT;
SELECT :'revision';""").splitlines()[-1]
        hold = ("BEGIN;\nSELECT vc.begin_change('main',vc.head(),'holder','hold main') AS draft \\gset\n"
                "SELECT pg_sleep(4);\nROLLBACK;")
        holding = ("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                   "AND state='active' AND query LIKE 'SELECT pg_sleep(4)%';")
        with ThreadPoolExecutor(max_workers=1) as pool:
            holder = pool.submit(self.owner.execute, hold)
            deadline = time.monotonic() + 20
            while self.owner.sql(holding) != "1":
                self.assertTrue(time.monotonic() < deadline and not holder.done(), "holder did not take main's lock")
                time.sleep(0.1)
            began = time.perf_counter()
            result = self.client.execute(f"""BEGIN;
SET LOCAL lock_timeout = '1s';
SELECT xr.merge_validated('main','{revision}','{{}}'::jsonb,'t');
COMMIT;""")
            waited = time.perf_counter() - began
            self.assertEqual(holder.result(timeout=30).returncode, 0)
        self.assertEqual(sqlstate(result.stderr), "55P03", result.stderr)
        self.assertGreaterEqual(waited, 1.0)
        self.assertEqual(self.owner.sql("SELECT vc.head();"), base)
        self.assertEqual(self.owner.sql("SELECT stock FROM app.products WHERE id=3999;"), "100")

    def test_functions_are_security_definer_with_pinned_search_path_and_client_only_execute(self):
        rows = json.loads(self.owner.sql("""
          SELECT json_agg(json_build_object('name',p.proname,'secdef',p.prosecdef,'config',p.proconfig,
            'acl_set',p.proacl IS NOT NULL,
            'client',has_function_privilege('agentdb_client',p.oid,'EXECUTE'),
            'manager',has_function_privilege('agentdb_manager',p.oid,'EXECUTE'),
            'public',EXISTS(SELECT 1 FROM aclexplode(p.proacl) a WHERE a.grantee=0)) ORDER BY p.proname)
          FROM pg_proc p WHERE p.pronamespace='xr'::regnamespace;"""))
        self.assertEqual([r["name"] for r in rows], ["lock_main", "merge_head", "merge_validated"])
        for row in rows:
            self.assertEqual(row, dict(name=row["name"], secdef=True, config=["search_path=pg_catalog, vc, xr, pg_temp"],
                                       acl_set=True, client=True, manager=False, public=False))


if __name__ == "__main__":
    unittest.main()
