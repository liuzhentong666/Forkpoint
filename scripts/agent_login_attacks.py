"""What an agent holding its own login and raw SQL can do (sql/011_agent_boundary.sql, agent_boundary.py).

scripts/check_agent_boundaries.py runs this on its fresh test database. Everything runs on real logins
over real connections: agents as logins the broker issued, and the broker and the keeper as logins of
their own, none of them a superuser. Only the steps a person does once, and the tampering the self-check
has to find, use the owner's connection. The control group runs some of the same attacks on the best
login there was before this boundary, where they have to get through, so a refusal here means the
boundary held and not that the attack was broken. Attacks land real commits on main, so this runs after
the checks that compare main before and after. An attempt that gets through is a GAP.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_boundary import BROKER, KEEPER, WORKER, issue_login, retire_login, scram_verifier
from agent_state.engine import Engine
from agent_state.maintenance import BoundaryKeeper
from agent_state.runtime import Runtime
from agent_state.storage import MemoryStore
from db import FLAGS, Connection, sqlstate

GENESIS = "00000000-0000-0000-0000-000000000001"
BIG = 2_000_000  # bytes: above the small budgets below


class Attacks:
    def __init__(self, owner, config, database, record, other_database):
        self.owner, self.config, self.database = owner, config, database
        self.record, self.other_database = record, other_database
        self.issued, self.roles = [], []
        self.pool = iter(int(x) for x in owner.sql("""SELECT id FROM app.products p WHERE NOT EXISTS(
          SELECT 1 FROM app.orders o WHERE o.product_id=p.id) ORDER BY id LIMIT 900;""").split())
        self.table = owner.sql("SELECT vc.table_id('app.products');")

    def rows(self, count):
        """Product ids no earlier attack has touched, so each attack's outcome is its own."""
        return [next(self.pool) for _ in range(count)]

    def login(self, user, password, role=None, database=None):
        return Connection(argparse.Namespace(config=self.config, database=database or self.database, role=role,
                                             password=False, user=user, host="127.0.0.1", password_value=password))

    def issue(self, rows=50, size=5_000_000, hours=1, share=0.1):
        name, password, _ = issue_login(self.broker, self.database, rows, size, hours, prefix="agentdb_atk", max_table_share=share)
        self.issued.append(name)
        return self.login(name, password), name

    def hand_built(self, statements):
        """A login built by the owner, for what the broker would never issue."""
        name, password = "agentdb_atk_" + secrets.token_hex(6), secrets.token_urlsafe(24)
        self.roles.append(name)
        self.owner.sql(f"BEGIN; CREATE ROLE {name} LOGIN PASSWORD '{scram_verifier(password)}';\n"
                       + statements.format(name=name, database=self.database) + "\nCOMMIT;")
        return self.login(name, password), name

    def state(self):
        return json.loads(self.owner.sql("""SELECT json_build_object('head',vc.head(),
          'rows',(SELECT md5(string_agg(p::text,',' ORDER BY id)) FROM app.products p));"""))

    def ok(self, connection, statement):
        result = connection.execute(statement, self.database)
        if result.returncode:
            raise RuntimeError(statement + "\n" + result.stderr)
        return result.stdout.strip()

    def refused(self, group, name, connection, statement, states=("42501",)):
        result = connection.execute(statement, self.database)
        state = sqlstate(result.stderr)
        held = result.returncode != 0 and state in states
        self.record(group, name, held, f"refused {state}" if held else (
            f"ACCEPTED, output {result.stdout.strip()[-160:]!r}" if result.returncode == 0
            else f"failed with unexpected {state}: {result.stderr.strip()[:200]!r}"))
        return state

    def work(self, connection, branch, ids, set_to="price+1"):
        listed = ",".join(map(str, ids))
        self.ok(connection, f"""SELECT vc.agent_fork('{branch}',vc.head());
          BEGIN; SELECT vc.agent_begin('{branch}',vc.head('{branch}'),'change');
          UPDATE agent.products SET price={set_to} WHERE id IN ({listed});
          SELECT vc.finish_change(); COMMIT;""")

    def merged(self, connection, branch, ids, set_to="price+1"):
        self.work(connection, branch, ids, set_to=set_to)
        return self.ok(connection, f"SELECT vc.agent_merge('{branch}');")

    def hold(self, connection, script):
        """An open session that has run script, and a way to read what it prints."""
        process = subprocess.Popen(connection.client_args(self.database), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=connection.env, creationflags=FLAGS)
        process.stdin.write(script.encode("utf-8") + b"\n")
        process.stdin.flush()
        return process

    @staticmethod
    def ask(process, statement):
        """One line of what an open session answers; empty once its backend has been ended."""
        try:
            process.stdin.write(statement.encode("utf-8") + b"\n")
            process.stdin.flush()
        except OSError:
            return ""
        return process.stdout.readline().decode().strip()

    @staticmethod
    def close(process):
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=30)
        process.stdout.close()
        process.stderr.close()

    def budget(self, connection):
        status = json.loads(self.ok(connection, "SELECT vc.agent_status();"))
        return status["main_rows_left"], status["bytes_left"]

    def problems(self):
        return json.loads(self.keeper.sql("SELECT vc.boundary_status()->'problems';", self.database))

    def end_stuck(self, idle, transaction, frozen=False):
        ended = json.loads(self.keeper.sql(
            f"SELECT vc.end_stuck_agent_sessions({idle},{transaction},{'true' if frozen else 'false'});", self.database))
        return {item["login"]: item["reason"] for item in ended}

    def set_up(self):
        """The one-time part: a broker and a keeper that hold nothing else. Installing did the rest."""
        broker_name, keeper_name = "agentdb_atkbroker_" + secrets.token_hex(4), "agentdb_atkkeeper_" + secrets.token_hex(4)
        broker_password, keeper_password = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        self.roles += [broker_name, keeper_name]
        self.owner.sql(f"""BEGIN;
          CREATE ROLE {broker_name} LOGIN NOINHERIT PASSWORD '{scram_verifier(broker_password)}';
          GRANT {BROKER} TO {broker_name} WITH INHERIT TRUE, SET TRUE;
          CREATE ROLE {keeper_name} LOGIN PASSWORD '{scram_verifier(keeper_password)}';
          GRANT {KEEPER} TO {keeper_name};
          COMMIT;""")
        self.broker = self.login(broker_name, broker_password, role=BROKER)
        self.broker_login = self.login(broker_name, broker_password)
        self.keeper, self.keeper_name = self.login(keeper_name, keeper_password), keeper_name
        self.boundary = BoundaryKeeper(Runtime(Engine(MemoryStore())), self.keeper)
        self.a, self.a_name = self.issue()
        self.b, self.b_name = self.issue()
        found = self.problems()
        self.record("normal", "self_check_is_clean_after_install", found == [], str(found))

    def normal(self):
        """The ordinary path has to keep working, in the same steps as today."""
        id_1, id_2 = self.rows(2)
        start = self.state()
        self.ok(self.a, f"""SELECT vc.agent_fork('a-1',vc.head());
          BEGIN; SELECT vc.agent_begin('a-1',vc.head('a-1'),'change');
          UPDATE agent.products SET price=price+1 WHERE id={id_1};
          SELECT vc.write_file('notes/a.txt',convert_to('hello','UTF8'));
          SELECT vc.finish_change(); COMMIT;""")
        self.record("normal", "branch_work_leaves_main_alone", self.state() == start, "head and rows the same")
        reads = json.dumps({self.table: [{"id": id_2}]})
        self.merge_a = self.ok(self.a, f"SELECT vc.agent_merge('a-1','{reads}'::jsonb);")
        self.record("normal", "fork_write_merge_lands_on_main", self.state()["head"] == self.merge_a, self.merge_a)
        content = self.ok(self.a, "SELECT convert_from(vc.read_file(vc.head(),'notes/a.txt'),'UTF8');")
        self.record("normal", "the_file_landed_with_the_row", content == "hello", content)
        author = self.owner.sql(f"SELECT author FROM vc.changeset WHERE id='{self.merge_a}';")
        self.record("normal", "history_names_the_real_login", author == self.a_name, author)
        left_rows, left_bytes = self.budget(self.a)
        self.record("normal", "budget_charged_for_the_row_and_the_file", left_rows == 48 and left_bytes < 5_000_000,
                    f"main_rows_left={left_rows}, bytes_left={left_bytes}")
        self.ok(self.owner, """SET ROLE agentdb_client; SELECT vc.fork('old-path',vc.head());
          BEGIN; SELECT vc.begin_change('old-path',vc.head('old-path'),'gpt-a','x');
          SELECT vc.finish_change(); COMMIT;""")
        legacy = self.owner.sql("SELECT author FROM vc.history(vc.head('old-path'),1);")
        self.record("normal", "the_trusted_path_is_unchanged", legacy == "gpt-a", f"author={legacy}")

    def escalate(self):
        a, name = self.a, self.a_name
        result = a.execute("RESET ROLE; SELECT session_user||'/'||current_user||'/'||current_setting('is_superuser');",
                           self.database)
        self.record("escalate", "reset_role_leaves_nothing", result.stdout.strip() == f"{name}/{name}/off",
                    result.stdout.strip() or result.stderr.strip()[:160])
        for label, statement in (
                ("set_role_postgres", "SET ROLE postgres;"), ("set_role_old_client", "SET ROLE agentdb_client;"),
                ("set_role_worker", f"SET ROLE {WORKER};"), ("set_role_manager", "SET ROLE agentdb_manager;"),
                ("set_role_keeper", f"SET ROLE {KEEPER};"), ("set_role_broker", f"SET ROLE {BROKER};"),
                ("set_session_authorization", "SET SESSION AUTHORIZATION postgres;"),
                ("create_role", "CREATE ROLE x_probe;"), ("create_database", "CREATE DATABASE x_probe;"),
                ("create_schema", "CREATE SCHEMA x_probe;"), ("create_temp_table", "CREATE TEMP TABLE x_probe(a int);"),
                ("create_table_in_public", "CREATE TABLE public.x_probe(a int);"),
                ("create_function_in_public", "CREATE FUNCTION public.x_probe() RETURNS int LANGUAGE sql AS 'SELECT 1';"),
                ("alter_self_superuser", f"ALTER ROLE {name} SUPERUSER;"),
                ("alter_self_createrole", f"ALTER ROLE {name} CREATEROLE;"),
                ("alter_self_bypassrls", f"ALTER ROLE {name} BYPASSRLS;"),
                ("extend_own_expiry", f"ALTER ROLE {name} VALID UNTIL 'infinity';"),
                ("raise_own_connection_limit", f"ALTER ROLE {name} CONNECTION LIMIT -1;"),
                ("grant_self_the_keeper_role", f"GRANT {KEEPER} TO {name};"),
                ("grant_self_set_on_worker", f"GRANT {WORKER} TO {name} WITH SET TRUE;"),
                ("session_replication_role", "SET session_replication_role=replica;"),
                ("own_temp_file_limit", "SET temp_file_limit=-1;"),
                ("own_temp_file_limit_default", f"ALTER ROLE {name} RESET temp_file_limit;"),
                ("alter_database", f'ALTER DATABASE "{self.database}" SET statement_timeout=0;'),
                ("alter_system", "ALTER SYSTEM SET max_connections=1;"), ("reload_config", "SELECT pg_reload_conf();"),
                ("read_server_file", "SELECT pg_read_file('PG_VERSION');"),
                ("list_server_directory", "SELECT pg_ls_dir('.');"),
                ("copy_to_program", "COPY (SELECT 1) TO PROGRAM 'id';"),
                ("copy_to_server_file", "COPY (SELECT 1) TO '/tmp/x_probe';"),
                ("copy_from_server_file", "COPY agent.products FROM '/etc/passwd';"),
                ("read_password_hashes", "SELECT * FROM pg_authid;"),
                ("change_the_role_catalog", "UPDATE pg_authid SET rolsuper=true;")):
            self.refused("escalate", label, a, statement)
        self.refused("escalate", "create_extension", a, "CREATE EXTENSION pgcrypto;", ("42501", "0A000"))
        self.ok(a, f"ALTER ROLE {name} RESET ALL;")
        limit = self.ok(a, "SHOW temp_file_limit;")
        self.record("escalate", "reset_all_keeps_its_temp_file_limit", limit == "100MB", limit)

    def main_table(self):
        """Touching main or history without going through the gate."""
        a = self.a
        row = self.rows(1)[0]
        before = self.state()
        for label, statement in (
                ("update_main_table", f"UPDATE app.products SET price=1 WHERE id={row};"),
                ("delete_main_table", "DELETE FROM app.products;"), ("truncate_main_table", "TRUNCATE app.products;"),
                ("drop_main_table", "DROP TABLE app.products;"), ("alter_main_table", "ALTER TABLE app.products DROP COLUMN price;"),
                ("disable_capture_trigger", "ALTER TABLE app.products DISABLE TRIGGER vc_capture;"),
                ("lock_main_table", "BEGIN; LOCK TABLE app.products IN ACCESS EXCLUSIVE MODE; ROLLBACK;"),
                ("select_for_update_main", f"BEGIN; SELECT 1 FROM app.products WHERE id={row} FOR UPDATE; ROLLBACK;"),
                ("old_begin_change", "SELECT vc.begin_change('main',vc.head(),'x','y');"),
                ("old_merge", "SELECT vc.merge('main',vc.head('a-1'),vc.head(),'x');"),
                ("old_merge_validated", "SELECT vc.merge_validated('main',vc.head('a-1'),'{}','x');"),
                ("old_fork", "SELECT vc.fork('x-old',vc.head());"),
                ("inner_apply_main", f"""SELECT vc._apply_main('{self.table}','{{"id":1}}','{{}}');"""),
                ("materialize", f"SELECT vc.materialize(vc.head(),'{self.table}');"),
                ("read_changeset_table", "SELECT * FROM vc.changeset;"),
                ("insert_changeset", "INSERT INTO vc.changeset VALUES(gen_random_uuid(),'x','y');"),
                ("rewrite_history", "UPDATE vc.changeset SET author='x';"),
                ("delete_history", "DELETE FROM vc.row_change;"),
                ("move_main_head", "UPDATE vc.branch SET head=base WHERE name='main';"),
                ("raise_own_budget", "UPDATE vc.agent_budget SET main_rows_left=1000000;"),
                ("read_the_budgets", "SELECT * FROM vc.agent_budget;"),
                ("steal_a_branch", f"UPDATE vc.branch_owner SET login='{self.a_name}';"),
                ("open_the_gate", "UPDATE vc.gate SET frozen_reason=NULL;"),
                ("issue_own_budget", f"SELECT vc.issue_budget('{self.a_name}',1,1);"),
                ("freeze", "SELECT vc.freeze('x');"), ("unfreeze", "SELECT vc.unfreeze();"),
                ("stop_another_agent", f"SELECT vc.stop_agent('{self.b_name}');"),
                ("revert", f"SELECT vc.revert('{self.merge_a}');"),
                ("run_the_self_check", "SELECT vc.boundary_check();"), ("read_the_self_check", "SELECT vc.boundary_problems();"),
                ("inner_refuse", "SELECT vc._refuse('AB000','x','{}','x');"), ("inner_gate", "SELECT vc._gate_open();"),
                ("inner_charge", "SELECT vc._charge_bytes(-1000000);"), ("inner_stage", f"SELECT vc._stage('{self.table}','{{}}',NULL,NULL);")):
            self.refused("main", label, a, statement)
        for label, statement, states in (
                ("gate_refuses_main_as_a_branch", "SELECT vc.agent_begin('main',vc.head(),'x');", ("AB001",)),
                ("gate_refuses_merging_main", "SELECT vc.agent_merge('main');", ("AB001",)),
                ("gate_refuses_a_null_branch", "SELECT vc.agent_begin(NULL,NULL);", ("AB001",)),
                ("gate_refuses_merging_a_null_branch", "SELECT vc.agent_merge(NULL);", ("AB001",)),
                ("fork_cannot_recreate_main", "SELECT vc.agent_fork('main',vc.head());", ("23505",)),
                ("branch_view_write_needs_a_begin", f"UPDATE agent.products SET price=1 WHERE id={row};", ("P0001",)),
                ("commit_without_finish_is_refused", """SELECT vc.agent_fork('a-unfinished',vc.head());
                  BEGIN; SELECT vc.agent_begin('a-unfinished',vc.head('a-unfinished'),'x'); COMMIT;""", ("23514",)),
                ("finish_cannot_be_skipped_by_setting_constraints", """BEGIN;
                  SELECT vc.agent_begin('a-unfinished',vc.head('a-unfinished'),'x'); SET CONSTRAINTS ALL IMMEDIATE; COMMIT;""", ("23514",)),
                ("snapshot_isolation_is_refused_at_the_gate", """BEGIN ISOLATION LEVEL REPEATABLE READ;
                  SELECT vc.agent_merge('a-unfinished');""", ("AB006",)),
                ("serializable_is_refused_at_the_gate", """BEGIN ISOLATION LEVEL SERIALIZABLE;
                  SELECT vc.agent_begin('a-unfinished',vc.head('a-unfinished'),'x');""", ("AB006",))):
            self.refused("main", label, a, statement, states)
        self.record("main", "main_unchanged_after_all_of_this", self.state() == before, "head and rows the same")

    def agents(self):
        a, b, b_name = self.a, self.b, self.b_name
        self.work(b, "b-1", self.rows(1))
        self.refused("agents", "write_another_agents_branch", a, "SELECT vc.agent_begin('b-1',vc.head('b-1'),'x');", ("AB001",))
        self.refused("agents", "merge_another_agents_branch", a, "SELECT vc.agent_merge('b-1');", ("AB001",))
        self.refused("agents", "fork_over_another_agents_branch", a, "SELECT vc.agent_fork('b-1',vc.head());", ("23505",))
        self.refused("agents", "become_another_agent", a, f"SET ROLE {b_name};")
        self.refused("agents", "share_its_own_login_with_another", a, f"GRANT {self.a_name} TO {b_name};")
        count = self.ok(a, f"SELECT count(*) FROM vc.rows_at(vc.head('b-1'),'{self.table}');")
        self.record("agents", "reading_other_branches_is_allowed", None, f"{count} rows visible; agents coordinate by reading")
        self.ok(a, "SELECT vc.agent_fork('a-takes-b',vc.head('b-1'));")
        took = self.ok(a, "SELECT vc.agent_merge('a-takes-b');")
        who = self.owner.sql(f"SELECT author FROM vc.changeset WHERE id='{took}';")
        self.record("agents", "taking_anothers_work_is_recorded_as_the_taker", who == self.a_name,
                    f"recorded as {who} and charged to it: it could copy the rows anyway")
        session = self.hold(b, "SELECT pg_backend_pid(); SELECT 'ready';")
        pid = session.stdout.readline().decode().strip()
        session.stdout.readline()
        self.refused("agents", "end_another_agents_session", a, f"SELECT pg_terminate_backend({pid});")
        text = self.ok(a, f"SELECT coalesce(query,'') FROM pg_stat_activity WHERE pid={pid};")
        self.record("agents", "cannot_read_another_agents_statements", "ready" not in text, repr(text))
        self.close(session)
        other = json.loads(self.ok(a, "SELECT vc.agent_status();"))
        self.record("agents", "status_shows_only_itself", other["login"] == self.a_name and "b-1" not in other["branches"],
                    json.dumps(other["branches"]))

    def budgets(self):
        """The blast radius is fixed before the agent starts."""
        c, _ = self.issue(rows=10)
        self.work(c, "c-big", self.rows(11))
        before = self.state()
        self.refused("budget", "merge_over_budget", c, "SELECT vc.agent_merge('c-big');", ("AB002",))
        self.record("budget", "over_budget_merge_left_main_alone", self.state() == before, "head and rows the same")
        result = c.execute("DO $$ BEGIN PERFORM vc.agent_merge('c-big'); EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'caught'; END $$;",
                           self.database)
        self.record("budget", "catching_the_refusal_commits_no_merge", result.returncode == 0 and self.state() == before,
                    "the DO block committed, main the same")
        left = self.budget(c)[0]
        self.record("budget", "failed_merges_cost_nothing", left == 10, f"main_rows_left={left}")
        landed = 0
        for number in range(4):  # many small merges cannot beat the budget
            self.work(c, f"c-s{number}", self.rows(4))
            landed += c.execute(f"SELECT vc.agent_merge('c-s{number}');", self.database).returncode == 0
        left = self.budget(c)[0]
        self.record("budget", "many_small_merges_stop_at_the_budget", landed == 2 and left == 2, f"{landed} of 4 merges of 4 rows; {left} left")

        d, _ = self.issue(rows=20)  # the same login spending from two sessions at once
        groups = self.rows(12), self.rows(12)
        self.work(d, "d-0", groups[0])
        self.work(d, "d-1", groups[1])
        outcomes = [None, None]
        def spend(position):
            outcomes[position] = d.execute(f"SELECT vc.agent_merge('d-{position}');", self.database)
        threads = [threading.Thread(target=spend, args=(position,)) for position in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        states = sorted(sqlstate(o.stderr) or "landed" for o in outcomes)
        left = self.budget(d)[0]
        self.record("budget", "two_sessions_cannot_spend_the_budget_twice", states == ["AB002", "landed"] and left == 8,
                    f"{states}; {left} left")

        e, _ = self.issue(size=1_000_000)
        self.ok(e, "SELECT vc.agent_fork('e-1',vc.head());")
        begin = "SELECT vc.agent_begin('e-1',vc.head('e-1'),'x');"
        self.refused("budget", "huge_file_over_storage_budget", e,
                     f"BEGIN; {begin} SELECT vc.write_file('big.bin',decode(repeat('00',{BIG}),'hex'));", ("AB003",))
        self.refused("budget", "huge_commit_message_over_storage_budget", e,
                     f"BEGIN; SELECT vc.agent_begin('e-1',vc.head('e-1'),repeat('x',{BIG})); SELECT vc.finish_change(); COMMIT;", ("AB003",))
        # Each overwrites the last before finish_change, which sees only the final file. All were stored.
        self.refused("budget", "files_overwritten_before_finish_are_paid_for", e, f"""BEGIN; {begin}
          SELECT vc.write_file('same.bin',decode(repeat('01',600000),'hex'));
          SELECT vc.write_file('same.bin',decode(repeat('02',600000),'hex'));""", ("AB003",))
        _, before_bytes = self.budget(e)
        self.ok(e, f"""BEGIN; {begin}
          SELECT vc.write_file('gone.bin',decode(repeat('03',600000),'hex')); SELECT vc.write_file('gone.bin',NULL);
          SELECT vc.finish_change(); COMMIT;""")
        _, after_bytes = self.budget(e)
        self.record("budget", "a_file_deleted_before_finish_is_paid_for", before_bytes - after_bytes >= 600000,
                    f"charged {before_bytes - after_bytes} bytes")
        stored = self.owner.sql(f"SELECT count(*) FROM vc.blob WHERE octet_length(content)>={BIG};")
        self.record("budget", "refused_files_left_nothing_stored", stored == "0", f"large blobs stored: {stored}")

        row = self.rows(1)[0]
        f, _ = self.issue(size=200_000)  # about 1 MB of this compresses to a few KB
        self.ok(f, "SELECT vc.agent_fork('f-1',vc.head());")
        self.refused("budget", "a_row_that_compresses_well_pays_full_size", f, f"""BEGIN;
          SELECT vc.agent_begin('f-1',vc.head('f-1'),'x');
          UPDATE agent.products SET name=repeat('x',1000000) WHERE id={row};
          SELECT vc.finish_change(); COMMIT;""", ("AB003",))

        g, _ = self.issue(size=1200)
        empty = "BEGIN; SELECT vc.agent_begin('g-1',vc.head('g-1'),''); SELECT vc.finish_change(); COMMIT;"
        self.ok(g, "SELECT vc.agent_fork('g-1',vc.head());")
        made = 0
        while made < 20 and g.execute(empty, self.database).returncode == 0:
            made += 1
        last = sqlstate(g.execute(empty, self.database).stderr)
        self.record("budget", "empty_commits_are_not_free", made == 3 and last == "AB003", f"{made} empty commits, then {last}")

        h, h_name = self.hand_built(f"GRANT {WORKER} TO {{name}} WITH INHERIT TRUE, SET FALSE; GRANT CONNECT ON DATABASE \"{{database}}\" TO {{name}};")
        self.refused("budget", "no_budget_row_means_no_writes", h, "SELECT vc.agent_fork('h-1',vc.head());", ("AB003",))
        self.refused("budget", "no_budget_row_means_no_merges", h, "SELECT vc.agent_merge('h-1');", ("AB001",))
        self.refused("budget", "broker_cannot_top_up_a_budget", self.broker, f"SELECT vc.issue_budget('{self.a_name}',999999,999999);", ("23505",))
        self.owner.sql(f'DROP OWNED BY {h_name} CASCADE; DROP ROLE {h_name};')
        self.roles.remove(h_name)

    def server_log(self):
        """The end of the server log, or None where this connection cannot read it."""
        major = self.owner.sql("SELECT current_setting('server_version_num')::int/10000;")
        path = self.owner.sql("SELECT coalesce(pg_current_logfile(),'');") or f"/var/log/postgresql/postgresql-{major}-main.log"
        result = self.owner.execute(f"""SELECT pg_read_file('{path}',greatest((pg_stat_file('{path}')).size-2000000,0),2000000);""", self.database)
        return result.stdout if result.returncode == 0 else None

    def incidents(self):
        """What agents have really done on their own, replayed; docs/AGENT_BOUNDARY.md names each case and its source."""
        wide = dict(rows=1_000_000, size=100_000_000)
        a, a_name = self.a, self.a_name
        for label, statement in (("drop_the_database", f'DROP DATABASE "{self.database}";'), ("drop_the_tables_schema", "DROP SCHEMA app CASCADE;"),
                                 ("drop_the_history_schema", "DROP SCHEMA vc CASCADE;"), ("drop_a_history_table", "DROP TABLE vc.row_change;"),
                                 ("drop_a_constraint", "ALTER TABLE app.products DROP CONSTRAINT products_sku_unique;"),
                                 ("take_over_ownership", f"REASSIGN OWNED BY postgres TO {a_name};"),
                                 ("create_a_shadow_database", "CREATE DATABASE x_shadow;"), ("create_a_table_beside_the_data", "CREATE TABLE app.x_probe(a int);")):
            self.refused("incident", label, a, statement)

        # Told to clean up, it deletes everything. Its budget would have allowed it.
        before = self.state()
        wiper, _ = self.issue(**wide)
        self.ok(wiper, """SELECT vc.agent_fork('wipe',vc.head());
          BEGIN; SELECT vc.agent_begin('wipe',vc.head('wipe'),'clean up old rows');
          DELETE FROM agent.products; SELECT vc.finish_change(); COMMIT;""")
        self.refused("incident", "deleting_the_whole_table_is_refused_at_the_gate", wiper, "SELECT vc.agent_merge('wipe');", ("AB007",))
        self.ok(wiper, """SELECT vc.agent_fork('no-where',vc.head());
          BEGIN; SELECT vc.agent_begin('no-where',vc.head('no-where'),'fix a price');
          UPDATE agent.products SET price=0; SELECT vc.finish_change(); COMMIT;""")
        self.refused("incident", "an_update_with_no_where_is_refused_at_the_gate", wiper, "SELECT vc.agent_merge('no-where');", ("AB007",))
        # Real agents, refused like this, told people to merge the branch with a wider login. The hint has to give ways that work.
        hint = wiper.execute("SELECT vc.agent_merge('no-where');", self.database).stderr
        self.record("incident", "the_refusal_says_how_the_change_can_land", "branch of its own" in hint and "vc.merge" in hint, hint.strip()[:300])
        wider, _ = self.issue(share=1, **wide)
        self.refused("incident", "a_wider_login_cannot_merge_the_refused_branch", wider, "SELECT vc.agent_merge('no-where');", ("AB001",))
        landed = self.owner.execute("BEGIN; SELECT vc.merge('main',vc.head('no-where'),vc.head(),'owner'); ROLLBACK;", self.database)
        self.record("incident", "the_owner_can_merge_the_refused_branch", landed.returncode == 0, landed.stderr.strip()[:300] or "merged, then rolled back")
        faker, _ = self.issue(**wide)
        self.ok(faker, """SELECT vc.agent_fork('fake',vc.head());
          BEGIN; SELECT vc.agent_begin('fake',vc.head('fake'),'x');
          INSERT INTO agent.products SELECT 100000+n,'fake-'||n,'fake',1,1 FROM generate_series(1,500) n;
          SELECT vc.finish_change(); COMMIT;""")
        self.refused("incident", "hundreds_of_fabricated_rows_are_refused_at_the_gate", faker, "SELECT vc.agent_merge('fake');", ("AB007",))
        self.record("incident", "none_of_that_reached_main", self.state() == before, "head and rows the same")

        # The same damage a little at a time. A share of 1% of 4,000 rows is 40.
        slow, _ = self.issue(share=0.01, **wide)
        landed = []
        for number in range(3):
            self.work(slow, f"slow-{number}", self.rows(15))
            landed.append(sqlstate(slow.execute(f"SELECT vc.agent_merge('slow-{number}');", self.database).stderr) or "landed")
        self.record("incident", "small_merges_cannot_add_up_past_the_share", landed == ["landed", "landed", "AB007"], str(landed))

        # An ordinary merge stays ordinary, on a small table too, and on files.
        files = "".join(f"SELECT vc.write_file('mass/f{n}.txt',convert_to('x','UTF8'));" for n in range(30))
        self.ok(self.owner, f"SET ROLE agentdb_client; BEGIN; SELECT vc.begin_change('main',vc.head(),'seed','files'); {files} SELECT vc.finish_change(); COMMIT;")
        cleaner, _ = self.issue(**wide)
        removals = "".join(f"SELECT vc.write_file('mass/f{n}.txt',NULL);" for n in range(25))
        self.ok(cleaner, f"""SELECT vc.agent_fork('clean-files',vc.head());
          BEGIN; SELECT vc.agent_begin('clean-files',vc.head('clean-files'),'x'); {removals} SELECT vc.finish_change(); COMMIT;""")
        self.refused("incident", "deleting_most_of_the_files_is_refused_at_the_gate", cleaner, "SELECT vc.agent_merge('clean-files');", ("AB007",))

        # Plan mode that cannot be talked out of: no budget at all.
        reader, _ = self.issue(rows=0, size=0)
        rows = reader.execute("SELECT count(*) FROM app.products;", self.database)
        self.record("incident", "a_read_only_login_can_read", rows.returncode == 0, rows.stdout.strip())
        self.refused("incident", "a_read_only_login_cannot_fork", reader, "SELECT vc.agent_fork('plan',vc.head());", ("AB003",))

        # A wide login is decided on before it starts; then the wipe lands, and undo must bring it all back.
        allowed, allowed_name = self.issue(share=1, **wide)
        start = self.state()
        self.ok(allowed, """SELECT vc.agent_fork('wide',vc.head());
          BEGIN; SELECT vc.agent_begin('wide',vc.head('wide'),'rewrite'); DELETE FROM agent.products; SELECT vc.finish_change(); COMMIT;
          SELECT vc.agent_merge('wide');""")
        gone = self.owner.sql("SELECT count(*) FROM app.products;")
        report = json.loads(self.keeper.sql(f"SELECT vc.agent_report('{allowed_name}');", self.database))
        table = report["tables"].get("app.products", {})
        self.record("incident", "the_report_says_what_really_happened", table.get("deleted", 0) >= 4000 and report["merges"] == 1, json.dumps(report["tables"]))
        result = self.boundary.undo_login("keeper", allowed_name)
        self.record("incident", "a_wiped_table_comes_back_identical", gone == "0" and self.state()["rows"] == start["rows"] and not result["refused"],
                    f"{gone} rows after the wipe; identical after undo: {self.state()['rows'] == start['rows']}")
        report = json.loads(self.keeper.sql(f"SELECT vc.agent_report('{allowed_name}');", self.database))
        self.record("incident", "the_report_says_it_was_reverted", report["reverted"] == 1, f"{report['reverted']} of {report['merges']} merges reverted")
        self.refused("incident", "an_agent_cannot_read_the_reports", a, f"SELECT vc.agent_report('{allowed_name}');")

        # Refused attempts roll back, so only the server log can show them, and the agent cannot turn it off.
        for label, statement in (("turn_off_its_statement_log", "SET log_statement='none';"), ("reset_its_statement_log", f"ALTER ROLE {a_name} RESET log_statement;")):
            self.refused("incident", label, a, statement)
        marker = "x_marker_" + secrets.token_hex(6)
        a.execute(f"DELETE FROM app.products WHERE sku='{marker}';", self.database)
        log = self.server_log()
        line = next((text for text in (log or "").splitlines() if marker in text), "")
        self.record("incident", "a_refused_attempt_leaves_a_trace" if log is not None else "a_refused_attempt_leaves_a_trace_not_checked",
                    (a_name in line) if log is not None else None, line[:200] or "the server log cannot be read from here")

    def stopped(self):
        """vc.stop_agent spends the whole budget, so every kind of write stops."""
        s, s_name = self.issue()
        row_1, row_2 = self.rows(2)
        self.work(s, "s-1", [row_1])
        holder, _ = self.issue()
        self.work(holder, "s-holds-main", [row_2])
        self.ok(self.keeper, f"SELECT vc.stop_agent('{s_name}');")
        self.refused("stop", "stopped_login_cannot_fork", s, "SELECT vc.agent_fork('s-2',vc.head());", ("AB003",))
        self.refused("stop", "stopped_login_cannot_commit", s, """BEGIN;
          SELECT vc.agent_begin('s-1',vc.head('s-1'),'x'); SELECT vc.finish_change(); COMMIT;""", ("AB003",))
        session = self.hold(holder, "BEGIN; SELECT vc.agent_merge('s-holds-main'); SELECT 'holding';")
        session.stdout.readline(), session.stdout.readline()
        start = time.monotonic()
        state = self.refused("stop", "stopped_login_cannot_merge", s, "SET lock_timeout='20s'; SELECT vc.agent_merge('s-1');", ("AB002",))
        waited = time.monotonic() - start
        self.record("stop", "stopped_login_never_queues_for_mains_lock", state == "AB002" and waited < 15, f"answered in {waited:.1f} s while main's lock was held")
        self.close(session)

    def reads(self):
        """Read sets still work through the gate."""
        read, wrote = self.rows(2)
        self.work(self.a, "a-rs", [self.rows(1)[0]])
        self.work(self.b, "b-rs", [wrote])
        self.ok(self.b, "SELECT vc.agent_merge('b-rs');")
        before = self.state()
        self.refused("reads", "stale_read_refused_through_the_gate", self.a,
                     f"""SELECT vc.agent_merge('a-rs','{json.dumps({self.table: [{"id": wrote}]})}'::jsonb);""", ("RS001",))
        self.refused("reads", "sql_in_a_read_key_is_only_a_value", self.a,
                     f"""SELECT vc.agent_merge('a-rs','{json.dumps({self.table: [{"id": "1); DELETE FROM app.products; --"}]})}'::jsonb);""", ("22023",))
        self.refused("reads", "a_null_read_set_is_refused", self.a, "SELECT vc.agent_merge('a-rs',NULL);", ("22023",))
        self.record("reads", "refused_merges_left_main_alone", self.state() == before, "head and rows the same")
        self.record("reads", "a_read_that_did_not_change_merges", self.ok(
            self.a, f"""SELECT vc.agent_merge('a-rs','{json.dumps({self.table: [{"id": read}]})}'::jsonb);""") != before["head"], "landed")

    def held_up(self):
        """Holding everyone up, then the keeper's rules. Agents can turn their own timeouts off."""
        a, b, a_name = self.a, self.b, self.a_name
        self.work(a, "a-h1", self.rows(1))
        self.work(b, "b-w1", self.rows(1))
        session = self.hold(a, """SET idle_in_transaction_session_timeout=0; SET statement_timeout=0; SET lock_timeout=0;
          BEGIN; SELECT vc.agent_merge('a-h1'); SELECT 'holding';""")
        session.stdout.readline(), session.stdout.readline()
        blocked = sqlstate(b.execute("SET lock_timeout='2s'; SELECT vc.agent_merge('b-w1');", self.database).stderr)
        self.record("hold", "turning_off_timeouts_can_block_main", None, f"accepted; another agent's merge got {blocked}")
        time.sleep(1.5)
        ended = self.end_stuck(1, 3600)
        self.close(session)
        result = b.execute("SET lock_timeout='10s'; SELECT vc.agent_merge('b-w1');", self.database)
        self.record("hold", "keeper_ends_an_idle_holder_and_merges_resume",
                    blocked == "55P03" and ended.get(a_name) == "idle in transaction" and result.returncode == 0,
                    f"ended {ended}; next merge rc={result.returncode}")

        self.work(a, "a-h2", self.rows(1))
        self.work(b, "b-w2", self.rows(1))
        session = self.hold(a, "BEGIN; SELECT vc.agent_merge('a-h2'); SELECT 'holding'; SELECT pg_sleep(600);")
        session.stdout.readline(), session.stdout.readline()
        time.sleep(1.5)
        idle_only = self.end_stuck(3600, 3600)
        ended = self.end_stuck(3600, 1)
        self.close(session)
        result = b.execute("SET lock_timeout='10s'; SELECT vc.agent_merge('b-w2');", self.database)
        self.record("hold", "keeper_ends_a_holder_that_looks_busy",
                    a_name not in idle_only and ended.get(a_name) == "transaction too long" and result.returncode == 0,
                    f"ended {ended}; next merge rc={result.returncode}")

        session = self.hold(a, "SELECT pg_advisory_lock(42); SELECT 'locked';")
        session.stdout.readline(), session.stdout.readline()
        time.sleep(1.5)
        ended = self.end_stuck(1, 3600)
        self.close(session)
        self.record("hold", "keeper_ends_an_idle_session_holding_an_advisory_lock", ended.get(a_name) == "idle holding an advisory lock", str(ended))

        session = self.hold(a, "SELECT 'idle';")
        session.stdout.readline()
        time.sleep(1.5)
        ended = self.end_stuck(1, 1)
        alive = self.ask(session, "SELECT 'still here';")
        self.close(session)
        self.record("hold", "a_plainly_idle_session_is_left_alone", a_name not in ended and alive == "still here", f"{alive!r}; ended {ended}")

        session = self.hold(self.keeper, "SELECT pg_backend_pid();")
        pid = session.stdout.readline().decode().strip()
        self.refused("hold", "agent_cannot_end_the_keepers_session", a, f"SELECT pg_terminate_backend({pid});")
        self.close(session)
        session = self.hold(self.owner, "SELECT pg_backend_pid();")
        pid = session.stdout.readline().decode().strip()
        self.refused("hold", "keeper_cannot_end_a_superuser_session", self.keeper, f"SELECT pg_terminate_backend({pid});")
        self.close(session)
        result = a.execute("""BEGIN; SELECT vc.agent_begin('a-1',vc.head('a-1'),'x'); SELECT vc.finish_change();
          PREPARE TRANSACTION 'x_probe';""", self.database)
        setting = self.owner.sql("SHOW max_prepared_transactions;")
        self.record("hold", "prepared_transactions_cannot_outlive_a_session", result.returncode != 0 and setting == "0",
                    f"refused {sqlstate(result.stderr)}; max_prepared_transactions={setting}, and the self-check reports any other value")

    def storage(self):
        """Storage outside the budget."""
        a, name = self.a, self.a_name
        for label, statement in (("large_object_from_bytea", "SELECT lo_from_bytea(0,'\\x00');"), ("large_object_create", "SELECT lo_create(0);"),
                                 ("large_object_creat", "SELECT lo_creat(-1);"), ("temporary_table", "CREATE TEMP TABLE x_probe AS SELECT 1;")):
            self.refused("storage", label, a, statement)
        self.refused("storage", "own_role_settings_refuse_invented_names", a, f"ALTER ROLE {name} SET x_probe.blob='x';")
        size = 1_000_000
        result = a.execute(f"ALTER ROLE {name} SET search_path='{'x' * size}';", self.database)
        stored = self.owner.sql(f"""SELECT coalesce(sum(pg_column_size(setconfig)),0) FROM pg_db_role_setting
          WHERE setrole='{name}'::regrole;""")
        self.record("storage", "own_role_settings_hold_some_data_outside_the_budget", None,
                    f"{'accepted' if result.returncode == 0 else 'refused ' + str(sqlstate(result.stderr))}: {size} bytes of one letter, "
                    f"{stored} bytes compressed; one row per login and database, dropped with the login; listed as a limit")
        a.execute(f"ALTER ROLE {name} RESET search_path;", self.database)
        result = a.execute("SELECT pg_logical_emit_message(false,'x',repeat('x',1000));", self.database)
        self.record("storage", "write_ahead_log_messages", None, "accepted: not kept, recycled with the log" if result.returncode == 0 else f"refused {sqlstate(result.stderr)}")

    def undo(self):
        """Undo, without knowing what the change meant: updates, inserts, deletes and files."""
        u, u_name = self.issue()
        update_id, delete_id = self.rows(2)
        self.ok(self.owner, f"""SET ROLE agentdb_client;
          BEGIN; SELECT vc.begin_change('main',vc.head(),'seed','seed file');
          SELECT vc.write_file('undo/kept.txt',convert_to('original','UTF8'));
          SELECT vc.finish_change(); COMMIT;""")
        clean = self.state()
        revisions = [self.merged(u, "u-1", [update_id], set_to="1")]
        self.ok(u, f"""SELECT vc.agent_fork('u-2',vc.head());
          BEGIN; SELECT vc.agent_begin('u-2',vc.head('u-2'),'x');
          DELETE FROM agent.products WHERE id={delete_id};
          INSERT INTO agent.products(id,sku,name,price,stock) VALUES(900001,'sku-undo','undo',1,1);
          SELECT vc.write_file('undo/new.txt',convert_to('new','UTF8'));
          SELECT vc.write_file('undo/kept.txt',NULL);
          SELECT vc.finish_change(); COMMIT;""")
        revisions.append(self.ok(u, "SELECT vc.agent_merge('u-2');"))
        # A commit message the agent wrote, naming its first merge as already reverted, must not protect it.
        self.ok(u, f"""SELECT vc.agent_fork('u-3',vc.head());
          BEGIN; SELECT vc.agent_begin('u-3',vc.head('u-3'),'revert {revisions[0]}'); SELECT vc.finish_change(); COMMIT;""")
        revisions.append(self.ok(u, "SELECT vc.agent_merge('u-3');"))
        # Nor make the report say it was reverted, merged or not.
        self.ok(u, f"""SELECT vc.agent_fork('u-claim',vc.head());
          BEGIN; SELECT vc.agent_begin('u-claim',vc.head('u-claim'),'revert {revisions[1]}'); SELECT vc.finish_change(); COMMIT;""")
        claimed = json.loads(self.keeper.sql(f"SELECT vc.agent_report('{u_name}');", self.database))
        self.record("undo", "a_commit_message_cannot_make_the_report_say_reverted", claimed["reverted"] == 0,
                    f"{claimed['reverted']} of {claimed['merges']} merges reported reverted")
        self.record("undo", "bad_changes_landed", self.state()["rows"] != clean["rows"], "rows changed on main")
        session = self.hold(u, "SELECT 'open';")
        session.stdout.readline()
        result = self.boundary.undo_login("keeper", u_name)
        alive = self.ask(session, "SELECT 'alive';")
        self.close(session)
        self.record("undo", "undo_ends_the_logins_sessions", alive != "alive", repr(alive))
        self.record("undo", "finds_and_reverts_every_commit_by_login", sorted(result["reverted"]) == sorted(revisions) and not result["refused"],
                    f"{len(result['reverted'])} commits, {len(result['remaining'])} left")
        self.record("undo", "a_commit_message_cannot_hide_a_commit", revisions[0] in result["reverted"], revisions[0])
        self.record("undo", "reverting_returns_main_to_the_same_rows", self.state()["rows"] == clean["rows"], "app.products checksum identical")
        files = self.owner.sql("""SELECT coalesce(convert_from(vc.read_file(vc.head(),'undo/kept.txt'),'UTF8'),'missing')||'/'||
          coalesce(convert_from(vc.read_file(vc.head(),'undo/new.txt'),'UTF8'),'missing');""")
        self.record("undo", "reverting_restores_files", files == "original/missing", files)
        same = self.owner.sql(f"""SELECT NOT EXISTS(SELECT r.doc FROM vc.rows_at(vc.head(),'{self.table}') r
          EXCEPT SELECT to_jsonb(p) FROM app.products p) AND NOT EXISTS(SELECT to_jsonb(p) FROM app.products p
          EXCEPT SELECT r.doc FROM vc.rows_at(vc.head(),'{self.table}') r);""")
        self.record("undo", "history_and_the_live_table_still_agree", same == "t", same)
        kept = self.owner.sql(f"SELECT count(*) FROM vc.changeset WHERE id IN ({','.join(repr(r) for r in revisions)});")
        self.record("undo", "nothing_was_rewritten", kept == "3", f"{kept} of 3 commits still in history")
        head = self.state()["head"]
        again = self.boundary.undo_login("keeper", u_name)
        self.record("undo", "rerunning_undo_changes_nothing", self.state()["head"] == head and sorted(again["reverted"]) == sorted(revisions), "head the same")
        self.refused("undo", "an_undone_login_stays_stopped", u, "SELECT vc.agent_fork('u-4',vc.head());", ("AB003",))

        # A charge not yet committed holds the login's budget row, which stopping it has to update.
        holder, holder_name = self.issue()
        self.work(holder, "h-1", self.rows(1))
        session = self.hold(holder, """SET idle_in_transaction_session_timeout=0;
          BEGIN; SELECT vc.agent_begin('h-1',vc.head('h-1'),'x'); SELECT vc.finish_change(); SELECT 'charged';""")
        session.stdout.readline(), session.stdout.readline(), session.stdout.readline()
        start = time.monotonic()
        self.boundary.undo_login("keeper", holder_name)
        waited = time.monotonic() - start
        self.close(session)
        self.record("undo", "an_open_transaction_cannot_hold_up_undo", waited < 30, f"finished in {waited:.1f} s")

        v, v_name = self.issue()
        w, w_name = self.issue()
        row = self.rows(1)[0]
        bad = self.merged(v, "v-1", [row], set_to="2")
        self.merged(w, "w-1", [row], set_to="3")
        self.refused("undo", "revert_refuses_to_overwrite_newer_work", self.keeper, f"SELECT vc.revert('{bad}');", ("AB005",))
        result = self.boundary.undo_login("keeper", v_name)
        price = self.owner.sql(f"SELECT price FROM app.products WHERE id={row};")
        self.record("undo", "undo_stops_at_newer_work_and_says_why", result["refused"] is not None and result["remaining"] == [bad] and float(price) == 3,
                    f"remaining {len(result['remaining'])}; price {price}")
        self.boundary.undo_login("keeper", w_name)
        result = self.boundary.undo_login("keeper", v_name)
        self.record("undo", "undo_finishes_once_the_newer_work_is_undone", result["refused"] is None and result["reverted"] == [bad], f"{len(result['reverted'])} reverted")
        self.refused("undo", "cannot_revert_genesis", self.keeper, f"SELECT vc.revert('{GENESIS}');", ("AB004",))
        self.work(self.a, "a-branch-only", self.rows(1))
        branch_commit = self.owner.sql("SELECT vc.head('a-branch-only');")
        self.refused("undo", "cannot_revert_a_commit_that_is_not_on_main", self.keeper, f"SELECT vc.revert('{branch_commit}');", ("AB004",))
        self.refused("undo", "cannot_revert_an_unknown_revision", self.keeper, f"SELECT vc.revert('{uuid.uuid4()}');", ("AB004",))

    def small_tables(self):
        """Tables a real schema has beside the demo: small ones, an empty one, ordinary foreign keys, trigger functions."""
        # The owner registers them and reruns 011, as the docs say; parents are registered before their children.
        pairs = "".join(f"CREATE TABLE app.atk_p{k}(id int PRIMARY KEY); CREATE TABLE app.atk_c{k}(id int PRIMARY KEY,"
                        f" p int NOT NULL REFERENCES app.atk_p{k}); SELECT vc.register_table('app.atk_p{k}');"
                        f" SELECT vc.register_table('app.atk_c{k}');" for k in range(1, 5))
        self.ok(self.owner, "CREATE TABLE app.atk_flags(id int PRIMARY KEY, enabled boolean NOT NULL); SELECT vc.register_table('app.atk_flags');"
                            "CREATE TABLE app.atk_setting(id int PRIMARY KEY, value text NOT NULL); SELECT vc.register_table('app.atk_setting');"
                            "CREATE TABLE app.atk_fresh(id int PRIMARY KEY); SELECT vc.register_table('app.atk_fresh');" + pairs)
        self.ok(self.owner, """BEGIN; SELECT vc.begin_change('main',vc.head(),'seed','small tables');
          INSERT INTO app.atk_flags SELECT n,true FROM generate_series(1,15) n; INSERT INTO app.atk_setting VALUES(1,'on');
          SELECT vc.finish_change(); COMMIT;""")
        self.owner.install_agent_boundary(self.database)

        def change(connection, branch, statements):
            self.ok(connection, f"""SELECT vc.agent_fork('{branch}',vc.head());
              BEGIN; SELECT vc.agent_begin('{branch}',vc.head('{branch}'),'change'); {statements} SELECT vc.finish_change(); COMMIT;""")

        def merge_all(connection, branches):
            return [sqlstate(connection.execute(f"SELECT vc.agent_merge('{b}');", self.database).stderr) or "landed" for b in branches]

        # The first 20 rows are free of the share, but not of the table: they cannot wipe or rewrite a 15-row table.
        wiper, _ = self.issue()
        change(wiper, "flags-wipe", "DELETE FROM agent.atk_flags;")
        self.refused("incident", "wiping_a_table_smaller_than_the_free_rows_is_refused", wiper, "SELECT vc.agent_merge('flags-wipe');", ("AB007",))
        halves, _ = self.issue()
        change(halves, "flags-half", "UPDATE agent.atk_flags SET enabled=false WHERE id<=7;")
        change(halves, "flags-more", "UPDATE agent.atk_flags SET enabled=false WHERE id=8;")
        landed = merge_all(halves, ("flags-half", "flags-more"))
        self.record("incident", "a_small_table_cannot_be_rewritten_a_little_at_a_time", landed == ["landed", "AB007"], str(landed))
        ordinary, _ = self.issue()
        change(ordinary, "setting", "UPDATE agent.atk_setting SET value='off';")
        change(ordinary, "fresh", "INSERT INTO agent.atk_fresh SELECT generate_series(1,10);")
        landed = merge_all(ordinary, ("setting", "fresh"))
        self.record("incident", "a_one_row_table_and_an_empty_table_still_take_ordinary_changes", landed == ["landed", "landed"], str(landed))

        # Undo puts back rows linked by foreign keys that are not deferrable: children go before their parents.
        linker, linker_name = self.issue()
        change(linker, "linked", "".join(f"INSERT INTO agent.atk_p{k} VALUES(1); INSERT INTO agent.atk_c{k} VALUES(1,1);" for k in range(1, 5)))
        self.ok(linker, "SELECT vc.agent_merge('linked');")
        result = self.boundary.undo_login("keeper", linker_name)
        left = self.ok(self.owner, "SELECT " + "+".join(f"(SELECT count(*) FROM app.atk_p{k})+(SELECT count(*) FROM app.atk_c{k})" for k in range(1, 5)) + ";")
        self.record("undo", "undo_puts_back_rows_linked_by_ordinary_foreign_keys", result["refused"] is None and left == "0",
                    f"refused: {(result['refused'] or {}).get('error', '')[:160]!r}; rows left: {left}")

        # An application's trigger function beside its tables can only be fired, so it is no problem; a callable one is.
        self.ok(self.owner, "CREATE FUNCTION app.atk_touch() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RETURN NEW; END$$;"
                            "CREATE FUNCTION app.atk_callable() RETURNS int LANGUAGE sql AS $$SELECT 1$$;")
        try:
            found = self.problems()
        finally:
            self.ok(self.owner, "DROP FUNCTION app.atk_touch(); DROP FUNCTION app.atk_callable();")
        self.record("drift", "a_trigger_function_beside_the_tables_is_no_problem", not any("atk_touch" in p for p in found), "; ".join(found))
        self.record("drift", "a_callable_function_beside_the_tables_is_found", f"{WORKER} may execute app.atk_callable()" in found, "; ".join(found))

    def drift(self):
        """The self-check finds what nobody is watching for, from the catalog alone."""
        a_name, table = self.a_name, "app.products"
        capture = self.owner.sql("SELECT pg_get_triggerdef(oid) FROM pg_trigger WHERE tgrelid='app.products'::regclass AND tgname='vc_capture';")
        guard = self.owner.sql("SELECT pg_get_triggerdef(oid) FROM pg_trigger WHERE tgrelid='vc.changeset'::regclass AND tgname='agent_changeset';")
        function = self.owner.sql("SELECT pg_get_functiondef('vc.head(text)'::regprocedure);")
        database = self.database
        drifts = (
            ("trigger_disabled", "ALTER TABLE vc.row_change DISABLE TRIGGER immutable;", "ALTER TABLE vc.row_change ENABLE TRIGGER immutable;", "disabled trigger vc.row_change.immutable"),
            ("capture_trigger_disabled", f"ALTER TABLE {table} DISABLE TRIGGER vc_capture;", f"ALTER TABLE {table} ENABLE TRIGGER vc_capture;", "disabled trigger app.products.vc_capture"),
            ("capture_trigger_dropped", f"DROP TRIGGER vc_capture ON {table};", capture + ";", "tracked table without trigger vc_capture: app.products"),
            ("budget_trigger_dropped", "DROP TRIGGER agent_changeset ON vc.changeset;", guard + ";", "engine functions changed"),
            ("write_grant_added", f"GRANT UPDATE ON {table} TO {WORKER};", f"REVOKE UPDATE ON {table} FROM {WORKER};", f"{WORKER} may change app.products"),
            ("function_grant_added", f"GRANT EXECUTE ON FUNCTION vc.begin_change(text,uuid,text,text) TO {WORKER};",
             f"REVOKE EXECUTE ON FUNCTION vc.begin_change(text,uuid,text,text) FROM {WORKER};", f"{WORKER} may execute vc.begin_change(text,uuid,text,text)"),
            ("history_read_grant_added", f"GRANT SELECT ON vc.agent_budget TO {WORKER};", f"REVOKE SELECT ON vc.agent_budget FROM {WORKER};", f"{WORKER} may read vc.agent_budget"),
            ("schema_create_granted", f"GRANT CREATE ON SCHEMA agent TO {WORKER};", f"REVOKE CREATE ON SCHEMA agent FROM {WORKER};", f"{WORKER} may create in schema agent"),
            ("database_open_to_everyone", f'GRANT CONNECT ON DATABASE "{database}" TO PUBLIC;', f'REVOKE CONNECT ON DATABASE "{database}" FROM PUBLIC;', "any role may connect to this database"),
            ("temporary_tables_opened", f'GRANT TEMPORARY ON DATABASE "{database}" TO {WORKER};', f'REVOKE TEMPORARY ON DATABASE "{database}" FROM {WORKER};',
             f"{WORKER} may create schemas or temporary tables in this database"),
            ("large_objects_opened", "GRANT EXECUTE ON FUNCTION lo_create(oid) TO PUBLIC;", "REVOKE EXECUTE ON FUNCTION lo_create(oid) FROM PUBLIC;", f"{WORKER} may create large objects"),
            ("agent_login_made_superuser", f"ALTER ROLE {a_name} SUPERUSER;", f"ALTER ROLE {a_name} NOSUPERUSER;", f"agent login too strong: {a_name}"),
            ("agent_login_given_the_old_role", f"GRANT agentdb_client TO {a_name};", f"REVOKE agentdb_client FROM {a_name};", f"agent login {a_name} holds agentdb_client"),
            ("worker_role_given_another_role", f"GRANT agentdb_client TO {WORKER};", f"REVOKE agentdb_client FROM {WORKER};", f"{WORKER} holds agentdb_client"),
            ("worker_role_can_log_in", f"ALTER ROLE {WORKER} LOGIN;", f"ALTER ROLE {WORKER} NOLOGIN;", f"{WORKER} has attributes it must not have"),
            ("keeper_given_unfreeze", f"GRANT EXECUTE ON FUNCTION vc.unfreeze() TO {KEEPER};", f"REVOKE EXECUTE ON FUNCTION vc.unfreeze() FROM {KEEPER};", f"{KEEPER} may execute vc.unfreeze()"),
            ("keeper_given_the_old_role", f"GRANT agentdb_client TO {KEEPER};", f"REVOKE agentdb_client FROM {KEEPER};", f"{KEEPER} holds agentdb_client"),
            ("broker_given_createdb", f"ALTER ROLE {BROKER} CREATEDB;", f"ALTER ROLE {BROKER} NOCREATEDB;", f"{BROKER} has attributes it must not have"),
            ("broker_given_the_old_role_to_hand_out", f"GRANT agentdb_client TO {BROKER} WITH ADMIN TRUE, INHERIT FALSE, SET FALSE;", f"REVOKE agentdb_client FROM {BROKER};", f"{BROKER} holds agentdb_client"),
            ("new_superuser_login", "CREATE ROLE agentdb_atk_super LOGIN SUPERUSER;", "DROP ROLE agentdb_atk_super;", "superuser logins changed"),
            ("engine_function_rewritten", function.replace("SELECT head INTO result", "SELECT /*tampered*/ head INTO result") + ";", function + ";", "engine functions changed"),
        )
        for label, damage, repair, expected in drifts:
            self.owner.sql(damage)
            try:
                found = self.problems()
            finally:  # Roles are server-wide, so no failure may leave one of these behind.
                self.owner.sql(repair)
            self.record("drift", label + "_is_found", expected in found, "; ".join(found) or "NOT FOUND")
        original = self.owner.sql(f"SELECT rolvaliduntil::text FROM pg_roles WHERE rolname='{a_name}';")
        self.owner.sql(f"ALTER ROLE {a_name} VALID UNTIL 'infinity';")
        unbounded = [p for p in self.problems() if p == f"agent login not built by the broker: {a_name}"]
        self.owner.sql(f"ALTER ROLE {a_name} VALID UNTIL '{original}';")
        self.record("drift", "agent_login_with_no_expiry_is_found", bool(unbounded), "; ".join(unbounded) or "NOT FOUND")
        _, nested = self.hand_built(f"GRANT agentdb_client TO {{name}} WITH INHERIT FALSE, SET TRUE; GRANT CONNECT ON DATABASE \"{{database}}\" TO {{name}};")
        found = self.problems()
        self.record("drift", "login_outside_the_boundary_holding_the_old_role_is_found", f"login outside the boundary holds agentdb_client: {nested}" in found, "; ".join(found))
        self.owner.sql(f"DROP OWNED BY {nested} CASCADE; DROP ROLE {nested};")
        self.roles.remove(nested)
        found = self.problems()
        self.record("drift", "clean_again_after_every_repair", found == [], str(found))

    def freeze(self):
        """Fail closed: a problem freezes writes, reads go on, and only the owner opens them again."""
        b, b_name = self.b, self.b_name
        ready = self.rows(2)
        self.work(b, "b-f1", ready[:1])
        self.work(b, "b-f2", ready[1:])
        before = self.state()
        begun = self.hold(b, "BEGIN; SELECT vc.agent_begin('b-f1',vc.head('b-f1'),'x'); SELECT 'begun';")
        begun.stdout.readline(), begun.stdout.readline()
        self.keeper.sql("SELECT vc.freeze('test');", self.database)
        _, errors = begun.communicate(b"SELECT vc.finish_change();\nCOMMIT;\n", timeout=60)
        state = sqlstate(errors.decode("utf-8", "replace"))
        self.record("freeze", "a_change_begun_before_the_freeze_cannot_finish", state == "AB000", f"refused {state}")
        self.owner.sql("SELECT vc.unfreeze();")

        begun = self.hold(b, "BEGIN; SELECT vc.agent_begin('b-f1',vc.head('b-f1'),'x'); SELECT 'begun';")
        begun.stdout.readline(), begun.stdout.readline()
        merging = self.hold(b, "BEGIN; SELECT vc.agent_merge('b-f2'); SELECT 'past the gate';")
        merging.stdout.readline(), merging.stdout.readline()
        # Damage that takes no lock the open merge holds: ALTER TABLE would wait for it to end.
        grant = f"EXECUTE ON FUNCTION vc.begin_change(text,uuid,text,text) {{}} {WORKER};"
        self.owner.sql("GRANT " + grant.format("TO"))
        first = self.boundary.check("keeper")
        self.owner.sql("REVOKE " + grant.format("FROM"))
        self.record("freeze", "keepers_round_freezes_on_a_problem", bool(first["frozen"]) and "vc.begin_change" in first["frozen"], first["frozen"] or "NOT FROZEN")
        ended = {(item["login"], item["reason"]) for item in first["ended"]}
        self.record("freeze", "keepers_round_ends_transactions_already_past_the_gate", (b_name, "writing while frozen") in ended, str(first["ended"]))
        self.ask(merging, "COMMIT;")
        self.close(merging)
        self.record("freeze", "a_merge_past_the_gate_cannot_commit_after_the_freeze", self.state() == before, "head and rows the same")
        self.close(begun)
        for label, statement in (("merge", "SELECT vc.agent_merge('b-f1');"), ("begin", "SELECT vc.agent_begin('b-f1',vc.head('b-f1'),'x');"),
                                 ("fork", "SELECT vc.agent_fork('b-z',vc.head());")):
            self.refused("freeze", f"frozen_{label}_is_refused", b, statement, ("AB000",))
        result = b.execute("SELECT vc.agent_status();", self.database)
        status = json.loads(result.stdout) if result.returncode == 0 else {}
        self.record("freeze", "reads_and_the_reason_still_work", bool(status.get("frozen")), str(status.get("frozen")))
        rows = b.execute("SELECT count(*) FROM app.products;", self.database)
        self.record("freeze", "main_can_still_be_read", rows.returncode == 0, rows.stdout.strip())
        self.refused("freeze", "keeper_cannot_unfreeze", self.keeper, "SELECT vc.unfreeze();")
        self.refused("freeze", "broker_cannot_unfreeze", self.broker, "SELECT vc.unfreeze();")
        self.refused("freeze", "agent_cannot_unfreeze", b, "SELECT vc.unfreeze();")
        self.record("freeze", "main_unchanged_while_frozen", self.state() == before, "head and rows the same")
        again = self.boundary.check("keeper")
        self.record("freeze", "fixing_the_cause_does_not_reopen_writes", bool(again["frozen"]) and again["problems"] == [], "still frozen; self-check clean")
        u, u_name = self.frozen_undo_target
        result = self.boundary.undo_login("keeper", u_name)
        self.record("freeze", "undo_works_while_frozen", result["refused"] is None and len(result["reverted"]) == 1, f"{len(result['reverted'])} reverted")
        self.owner.sql("SELECT vc.unfreeze();")
        result = b.execute("SELECT vc.agent_merge('b-f1');", self.database)
        self.record("freeze", "writes_work_again_after_unfreezing", result.returncode == 0, (result.stderr.strip() or "landed")[:160])

        # A later engine change is reported until the owner reruns the migration, which accepts it and opens nothing.
        function = self.owner.sql("SELECT pg_get_functiondef('vc.head(text)'::regprocedure);")
        self.owner.sql(function.replace("SELECT head INTO result", "SELECT /*a later migration*/ head INTO result") + ";")
        frozen = self.boundary.check("keeper")["frozen"]
        self.owner.install_agent_boundary(self.database)
        found = self.problems()
        still = self.keeper.sql("SELECT vc.boundary_status()->>'frozen';", self.database)
        self.record("freeze", "rerunning_the_migration_accepts_the_engine_but_opens_nothing", bool(frozen) and found == [] and bool(still), f"problems {found}; still frozen: {bool(still)}")
        self.owner.sql(function + ";")
        self.owner.install_agent_boundary(self.database)
        self.owner.sql("SELECT vc.unfreeze();")

    def broker_limits(self):
        """The broker issues logins, and cannot make anything stronger than the worker role."""
        name = self.a_name
        for label, statement in (("create_superuser", "CREATE ROLE x_probe SUPERUSER;"), ("create_replication", "CREATE ROLE x_probe REPLICATION;"),
                                 ("create_bypassrls", "CREATE ROLE x_probe BYPASSRLS;"), ("create_createdb", "CREATE ROLE x_probe CREATEDB;"),
                                 ("grant_the_old_client_role", f"GRANT agentdb_client TO {name};"), ("grant_the_keeper_role", f"GRANT {KEEPER} TO {name};"),
                                 ("grant_server_files_role", f"GRANT pg_read_server_files TO {name};"), ("alter_postgres", "ALTER ROLE postgres PASSWORD 'x_probe';"),
                                 ("read_main", "SELECT count(*) FROM app.products;"), ("read_budgets", "SELECT * FROM vc.agent_budget;"),
                                 ("raise_a_budget", "UPDATE vc.agent_budget SET bytes_left=1;"), ("merge", "SELECT vc.merge('main',vc.head(),vc.head(),'x');"),
                                 ("drop_a_login_it_did_not_make", "DROP ROLE agentdb_manager;"), ("revert", f"SELECT vc.revert('{self.merge_a}');")):
            self.refused("broker", label, self.broker, statement)
        self.refused("broker", "use_the_worker_role", self.broker, f"SET ROLE {WORKER};")
        for label, statement in (("write_main", "UPDATE app.products SET price=1;"), ("read_main", "SELECT count(*) FROM app.products;"),
                                 ("issue_a_budget", f"SELECT vc.issue_budget('{name}x',1,1);"), ("rewrite_history", "UPDATE vc.changeset SET author='x';"),
                                 ("raise_a_budget", "UPDATE vc.agent_budget SET bytes_left=1;"), ("open_the_gate", "UPDATE vc.gate SET frozen_reason=NULL;"),
                                 ("create_a_role", "CREATE ROLE x_probe;"), ("become_an_agent", f"SET ROLE {name};")):
            self.refused("keeper", label, self.keeper, statement)

    def reach(self):
        """Roles are server-wide, so a login must not carry its rights into another database."""
        result = self.a.execute("SELECT 1;", self.other_database)
        self.record("reach", "login_cannot_connect_to_another_installation", result.returncode != 0 and "permission denied for database" in result.stderr,
                    (result.stderr.strip().splitlines() or ["ACCEPTED"])[0][:140])
        name = self.a_name
        open_to_it = self.owner.sql(f"""SELECT coalesce(string_agg(datname,', ' ORDER BY datname),'none') FROM pg_database
          WHERE datallowconn AND datname!~'^agentdb_test' AND has_database_privilege('{name}',oid,'CONNECT');""")
        self.record("reach", "other_databases_still_open_to_any_login", None, f"{open_to_it}: their owners decide; each needs REVOKE CONNECT ... FROM PUBLIC. vc.boundary_status lists them")

    def expiry(self):
        x, x_name = self.issue(rows=1, size=100_000, hours=4 / 3600)
        session = self.hold(x, "SELECT 'open';")
        session.stdout.readline()
        time.sleep(6)
        result = x.execute("SELECT 1;", self.database)
        self.record("expiry", "expired_login_cannot_reconnect", result.returncode != 0, (result.stderr.strip().splitlines() or ["ACCEPTED"])[0][:120])
        alive = self.ask(session, "SELECT 'still alive';")
        self.record("expiry", "a_session_opened_before_expiry_survives_it", alive == "still alive", f"{alive!r}: expiry stops only new connections, so the keeper must end it")
        ended = self.end_stuck(3600, 3600)
        self.close(session)
        self.record("expiry", "keeper_ends_the_expired_logins_sessions", ended.get(x_name) == "login expired", str(ended))

    def retire(self):
        y, y_name = self.issue(rows=5, size=100_000)
        session = self.hold(y, "SELECT 'open';")
        session.stdout.readline()
        retire_login(self.broker, self.keeper, self.database, y_name)
        self.issued.remove(y_name)
        self.close(session)
        left = self.owner.sql(f"SELECT count(*) FROM pg_roles WHERE rolname='{y_name}';")
        self.record("retire", "retiring_a_login_drops_it", left == "0", f"{left} left")
        result = y.execute("SELECT 1;", self.database)
        self.record("retire", "a_retired_login_cannot_connect", result.returncode != 0, (result.stderr.strip().splitlines() or ["ACCEPTED"])[0][:120])
        self.refused("retire", "a_retired_name_cannot_be_issued_again", self.broker, f"SELECT vc.issue_budget('{y_name}',1000,1000);", ("23505",))
        found = self.problems()
        self.record("retire", "self_check_stays_clean", found == [], str(found))

    def controls(self):
        """The same attacks on the best login before this boundary, built as experiments/readset_agents builds it.
        They have to get through: if one did not, the refusals above would prove nothing."""
        control, name = self.hand_built("GRANT agentdb_client TO {name} WITH INHERIT FALSE, SET TRUE; "
                                        'GRANT CONNECT ON DATABASE "{database}" TO {name};')
        row = self.rows(1)[0]
        self.work(self.b, "b-c1", self.rows(1))
        before = self.state()
        for label, statement in (
                ("commit_straight_to_main_with_a_forged_author", f"""SET ROLE agentdb_client; BEGIN;
                  SELECT vc.begin_change('main',vc.head(),'forged','x'); UPDATE app.products SET price=1 WHERE id={row};
                  SELECT vc.finish_change(); SET CONSTRAINTS ALL IMMEDIATE; SELECT 'landed'; ROLLBACK;"""),
                ("write_another_agents_branch", """SET ROLE agentdb_client; BEGIN;
                  SELECT vc.begin_change('b-c1',vc.head('b-c1'),'forged','x'); SELECT vc.finish_change(); SELECT 'landed'; ROLLBACK;"""),
                ("merge_another_agents_branch", """SET ROLE agentdb_client; BEGIN;
                  SELECT vc.merge('main',vc.head('b-c1'),vc.head(),'forged'); SELECT 'landed'; ROLLBACK;"""),
                ("temporary_table_outside_any_budget", """SET ROLE agentdb_client; BEGIN;
                  CREATE TEMP TABLE x_probe AS SELECT 1; SELECT 'landed'; ROLLBACK;""")):
            result = control.execute(statement, self.database)
            self.record("control", label, result.returncode == 0 and "landed" in result.stdout,
                        "got through on the old login, rolled back" if "landed" in result.stdout else f"ATTACK BROKEN: {result.stderr.strip()[:200]!r}")
        self.record("control", "nothing_was_kept", self.state() == before, "head and rows the same")
        self.owner.sql(f"DROP OWNED BY {name} CASCADE; DROP ROLE {name};")
        self.roles.remove(name)

    def command_line(self):
        """agent_boundary.py as a person or a runner uses it. The command it runs leaves a transaction idle, with its
        own timeout off, and waits for the keeper's rounds, which only the run itself is doing, to end the session."""
        config = ["--config", str(self.config)] if self.config else []
        def tool(action, *rest, timeout=120, env=None):
            # Connection options first: after "--" everything belongs to the agent's command.
            return subprocess.run([sys.executable, str(ROOT / "agent_boundary.py"), action, "--database", self.database, *config, *rest],
                                  capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, cwd=ROOT,
                                  creationflags=FLAGS, env=env)
        status = tool("status")
        report = json.loads(status.stdout) if status.returncode == 0 else {}
        self.record("cli", "status_reports_without_changing_anything", report.get("problems") == [] and report.get("frozen") is None,
                    f"exit {status.returncode}; {len(report.get('logins', []))} logins; open databases: {report.get('open_databases')}")
        once = tool("keep", "--once")
        self.record("cli", "one_keeper_round_exits_clean", once.returncode == 0, (once.stderr.strip() or "clean")[:200])
        agent = f"""
import argparse, os, pathlib, subprocess, sys, time
sys.path.insert(0, {str(ROOT)!r})
from db import FLAGS, Connection
config = {str(self.config) if self.config else ""!r}
connection = Connection(argparse.Namespace(config=pathlib.Path(config) if config else None, database=os.environ['PGDATABASE'],
    role=None, password=False, user=os.environ['PGUSER'], host='127.0.0.1', password_value=os.environ['PGPASSWORD']))
print(os.environ['PGUSER'], flush=True)
session = subprocess.Popen(connection.client_args(os.environ['PGDATABASE']), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, env=connection.env, creationflags=FLAGS)
session.stdin.write(b"SET idle_in_transaction_session_timeout=0; BEGIN; SELECT 'open';\\n")
session.stdin.flush()
session.stdout.readline()
time.sleep(5)
try:
    session.stdin.write(b"SELECT 'alive';\\n")
    session.stdin.flush()
    answer = session.stdout.readline().strip()
except OSError:
    answer = b''
print('ended by the keeper' if answer != b'alive' else 'NOT ENDED', flush=True)
sys.exit(0 if answer != b'alive' else 3)
"""
        ran = tool("run", "--rows", "5", "--bytes", "100000", "--hours", "0.2", "--prefix", "agentdb_atk", "--interval", "1",
                   "--idle-seconds", "1", "--", sys.executable, "-c", agent)
        lines = ran.stdout.strip().splitlines()
        self.record("cli", "run_keeps_the_keeper_going_while_the_agent_runs", ran.returncode == 0 and lines[-1:] == ["ended by the keeper"],
                    f"exit {ran.returncode}: {lines[-1:] or ran.stderr.strip()[:300]}")
        login = lines[0] if lines else ""
        left = self.owner.sql(f"SELECT count(*) FROM pg_roles WHERE rolname='{login}';") if login else "unknown"
        self.record("cli", "run_retires_the_login_when_the_command_ends", left == "0", f"{login}: {left} left")
        target, name = self.issue()
        revision = self.merged(target, "cli-1", self.rows(1))
        back = tool("undo", name)
        report = json.loads(back.stdout) if back.returncode == 0 else {}
        self.record("cli", "undo_reverts_a_login_from_the_command_line", report.get("reverted") == [revision], f"exit {back.returncode}; {report.get('reverted')}")
        told = tool("report", name)
        report = json.loads(told.stdout) if told.returncode == 0 else {}
        self.record("cli", "report_says_what_a_login_landed", report.get("merges") == 1 and report.get("reverted") == 1, f"exit {told.returncode}; {report.get('tables')}")

        # A person says stop, and no agent has to remember it: this is a new session, as after a context reset.
        worker, _ = self.issue()
        self.work(worker, "cli-frozen", self.rows(1))
        stopped = tool("freeze", "code freeze: it's release day")
        state = sqlstate(worker.execute("SELECT vc.agent_merge('cli-frozen');", self.database).stderr)
        status = tool("status")
        reason = json.loads(status.stdout)["frozen"] if status.returncode == 0 else None
        self.record("cli", "freeze_stops_agents_at_once", stopped.returncode == 0 and state == "AB000" and reason == "code freeze: it's release day", f"merge refused {state}; reason {reason!r}")
        opened = tool("unfreeze")
        self.record("cli", "the_owner_can_unfreeze", opened.returncode == 0 and worker.execute("SELECT vc.agent_merge('cli-frozen');", self.database).returncode == 0, f"exit {opened.returncode}")

        # The operator's environment stays behind unless a name is passed on purpose.
        secret = dict(os.environ, AGENTDB_ATK_TOKEN="not for agents", AGENTDB_ATK_MODEL_KEY="meant for it")
        probe = "import os; print(os.environ.get('AGENTDB_ATK_TOKEN','absent'), os.environ.get('AGENTDB_ATK_MODEL_KEY','absent'))"
        ran = tool("run", "--rows", "0", "--bytes", "0", "--hours", "0.1", "--prefix", "agentdb_atk", "--pass-env", "AGENTDB_ATK_MODEL_KEY",
                   "--", sys.executable, "-c", probe, env=secret)
        self.record("cli", "run_keeps_the_operators_environment_back", ran.stdout.strip() == "absent meant for it", (ran.stdout.strip() or ran.stderr.strip())[:200])

        # A copy outside the database, kept only if it restores.
        with tempfile.TemporaryDirectory() as folder:
            first = tool("backup", "--to", folder, "--keep", "1", timeout=600)
            facts = json.loads(first.stdout) if first.returncode == 0 else {}
            kept = sorted(p.name for p in Path(folder).iterdir())
            self.record("cli", "a_backup_is_kept_only_after_it_restores", facts.get("restored_and_checked") is True and len(kept) == 1 and kept[0].endswith(".dump"),
                        f"exit {first.returncode}; {kept}; {facts.get('tables') or first.stderr.strip()[:300]}")
            second = tool("backup", "--to", folder, "--keep", "1", timeout=600)
            kept_after = sorted(p.name for p in Path(folder).iterdir())
            self.record("cli", "only_the_newest_backups_are_kept", second.returncode == 0 and len(kept_after) == 1 and kept_after != kept, f"exit {second.returncode}; {len(kept_after)} kept")
        left = self.owner.sql("SELECT count(*) FROM pg_database WHERE datname LIKE 'agentdb\\_test\\_restore\\_%';")
        self.record("cli", "backup_leaves_no_scratch_database", left == "0", f"{left} left")

    def timing(self):
        """What the gate costs: the same fork, commit and merge of one row, on one long-lived session each."""
        rounds = 30
        agent, _ = self.issue(rows=rounds + 10)
        ids = self.rows(2 * rounds)
        def trusted(session, branch, row):
            session.sql(f"""SET ROLE agentdb_client; SELECT vc.fork('{branch}',vc.head());
              BEGIN; SELECT vc.begin_change('{branch}',vc.head('{branch}'),'timing','x');
              UPDATE agent.products SET price=price+1 WHERE id={row}; SELECT vc.finish_change(); COMMIT;
              SELECT vc.merge_validated('main',vc.head('{branch}'),'{{}}','timing');""")
        def gated(session, branch, row):
            session.sql(f"""SELECT vc.agent_fork('{branch}',vc.head());
              BEGIN; SELECT vc.agent_begin('{branch}',vc.head('{branch}'),'x');
              UPDATE agent.products SET price=price+1 WHERE id={row}; SELECT vc.finish_change(); COMMIT;
              SELECT vc.agent_merge('{branch}');""")
        taken = dict(trusted=[], gate=[])
        with self.owner.session(self.database) as owner_session, agent.session(self.database) as agent_session:
            for number in range(rounds):  # taken in turn, so history grows the same for both
                for label, run, session, row in (("trusted", trusted, owner_session, ids[2 * number]),
                                                 ("gate", gated, agent_session, ids[2 * number + 1])):
                    start = time.perf_counter()
                    run(session, f"t-{label}-{number}", row)
                    taken[label].append((time.perf_counter() - start) * 1000)
        medians = {label: round(statistics.median(values), 1) for label, values in taken.items()}
        self.record("timing", "one_row_fork_commit_and_merge", None,
                    f"median ms over {rounds} rounds on a {self.owner.sql('SELECT count(*) FROM app.products;')}-row table: "
                    f"trusted path {medians['trusted']}, through the gate {medians['gate']}, on this machine")
        checks = []
        with self.keeper.session(self.database) as session:
            for _ in range(10):
                start = time.perf_counter()
                session.sql("SELECT vc.boundary_check();")
                checks.append((time.perf_counter() - start) * 1000)
        self.record("timing", "one_self_check", None, f"median {round(statistics.median(checks), 1)} ms over 10, on this machine")

    def sandbox(self, image):
        """No database rule can stop a process that can become the superuser, so the way out has to be closed
        outside the database. Here a real container, started through agent_boundary.py run: the login works
        in it, and the routes this machine has are not there."""
        script = ROOT / "scripts" / "check_agent_sandbox.py"
        here = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=180, creationflags=FLAGS)
        wsl = bool(self.owner.settings["wsl_distribution"])
        found = "cannot_run_wsl" in json.loads(here.stdout.strip().splitlines()[-1])["gaps"]
        self.record("sandbox", "this_machines_own_route_to_superuser_is_found", found if wsl else None,
                    f"exit {here.returncode}, gaps: {json.loads(here.stdout.strip().splitlines()[-1])['gaps']}")
        if not image:
            self.record("sandbox", "real_container", None, "not run: give --sandbox-image with psql and python3")
            return
        config = ["--config", str(self.config)] if self.config else []
        inside = subprocess.run(
            [sys.executable, str(ROOT / "agent_boundary.py"), "run", "--database", self.database, *config, "--rows", "5",
             "--bytes", "100000", "--hours", "0.2", "--prefix", "agentdb_atk", "--agent-host", "host.docker.internal", "--",
             "docker", "run", "--rm", "-e", "PGUSER", "-e", "PGPASSWORD", "-e", "PGDATABASE", "-e", "PGHOST", "-e", "PGPORT",
             "-v", f"{script.parent}:/check:ro", image, "python3", "/check/check_agent_sandbox.py"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, cwd=ROOT, creationflags=FLAGS)
        lines = inside.stdout.strip().splitlines()
        self.record("sandbox", "in_a_container_every_route_is_closed_and_the_login_works",
                    inside.returncode == 0 and lines and json.loads(lines[-1])["gaps"] == [],
                    f"exit {inside.returncode}: " + "; ".join(lines[:-1] or [inside.stderr.strip()[:300]]))
        # A token left where the agent can see it, which is how one agent reached a production volume.
        planted = subprocess.run(["docker", "run", "--rm", "-e", "CLOUD_API_TOKEN=not-real", "-v", f"{script.parent}:/check:ro", image, "python3", "/check/check_agent_sandbox.py"],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, creationflags=FLAGS)
        gaps = json.loads(planted.stdout.strip().splitlines()[-1])["gaps"] if planted.stdout.strip() else []
        self.record("sandbox", "a_credential_left_in_reach_is_found", planted.returncode == 1 and gaps == ["no_other_credentials_in_the_environment"], f"exit {planted.returncode}; gaps {gaps}")

    def clean_up(self):
        for login in list(self.issued):
            try:
                retire_login(self.broker, self.keeper, self.database, login)
                self.issued.remove(login)
            except (RuntimeError, ValueError) as error:
                print(f"could not retire {login}: {error}", flush=True)
        for role in self.issued + self.roles:
            result = self.owner.execute(f"DROP OWNED BY {role} CASCADE; DROP ROLE IF EXISTS {role};", self.database)
            if result.returncode:
                print(f"could not drop {role}: {result.stderr.strip()[:200]}", flush=True)
        left = self.owner.sql("SELECT coalesce(string_agg(rolname,','),'none') FROM pg_roles WHERE rolname LIKE 'agentdb\\_atk%';")
        self.record("cleanup", "no_test_login_is_left", left == "none", left)


def run(owner, config, database, record, other_database, sandbox_image=None):
    attacks = Attacks(owner, config, database, record, other_database)
    try:
        attacks.set_up()
        attacks.normal()
        attacks.escalate()
        attacks.main_table()
        attacks.agents()
        attacks.budgets()
        attacks.incidents()
        attacks.stopped()
        attacks.reads()
        attacks.held_up()
        attacks.storage()
        attacks.undo()
        attacks.small_tables()
        attacks.drift()
        target, name = attacks.issue()
        attacks.merged(target, "t-1", attacks.rows(1))
        attacks.frozen_undo_target = target, name
        attacks.freeze()
        attacks.broker_limits()
        attacks.reach()
        attacks.expiry()
        attacks.retire()
        attacks.controls()
        attacks.command_line()
        attacks.timing()
        attacks.sandbox(sandbox_image)
    finally:
        attacks.clean_up()
