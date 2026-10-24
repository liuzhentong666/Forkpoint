"""Scripted agentdb_client worker and CLI scenarios for agent_lab.py characterization."""
import contextlib
import io
import json
import subprocess
import sys
import traceback
from unittest.mock import patch

DB = "agentdb_test_gpt_0123456789"
HEAD = "11111111-1111-1111-1111-111111111111"
BASE = "22222222-2222-2222-2222-222222222222"
NEW = "33333333-3333-3333-3333-333333333333"
PRODUCTS = "44444444-4444-4444-4444-444444444444"
ORDERS = "55555555-5555-5555-5555-555555555555"
# psql's report of an RS001 refusal, as a real lab database printed it on 2026-10-22.
READ_CONFLICT = (
    f"ERROR:  RS001: Read conflict: 1 row(s) the source read or wrote changed on main since {BASE}\n"
    'DETAIL:  [{"key": {"id": 101}, "now": {"id": 101, "sku": "sku-101", "name": "product-101", "price": 10.00, '
    '"stock": 95}, "base": {"id": 101, "sku": "sku-101", "name": "product-101", "price": 10.00, "stock": 100}, '
    f'"table": "{PRODUCTS}", "written": true}}]\n'
    "CONTEXT:  PL/pgSQL function merge_validated(text,uuid,jsonb,text) line 93 at RAISE\n"
    "LOCATION:  exec_stmt_raise, pl_exec.c:3897\n")
INVALID_READ_SET = (
    "ERROR:  22023: p_reads must map registered table ids to arrays of primary keys\n"
    'HINT:  {"<vc.table_id(...)>": [{"id": 1}, ...]}\n'
    "CONTEXT:  PL/pgSQL function merge_validated(text,uuid,jsonb,text) line 17 at RAISE\n"
    "LOCATION:  exec_stmt_raise, pl_exec.c:3897\n")
# psql's report of a fork of an existing branch, as a real lab database printed it on 2026-10-24.
BRANCH_EXISTS = (
    'ERROR:  23505: duplicate key value violates unique constraint "branch_pkey"\n'
    "DETAIL:  Key (name)=(gpt-a-1) already exists.\n"
    'CONTEXT:  SQL statement "INSERT INTO vc.branch VALUES(p_name,p_revision,p_revision)"\n'
    "PL/pgSQL function fork(text,uuid) line 3 at SQL statement\n"
    "SCHEMA NAME:  vc\nTABLE NAME:  branch\nCONSTRAINT NAME:  branch_pkey\n"
    "LOCATION:  _bt_check_unique, nbtinsert.c:666\n")


class FakeWorker:
    """Answers the statements agent_lab sends; records them in order."""

    def __init__(self, product=None, pricing="PRICE = 11\n", inventory="STOCK = 95\n", merge=None, fork=None):
        self.product = product or {"id": 101, "price": 10, "stock": 100}
        self.pricing, self.inventory, self.merge, self.fork = pricing, inventory, merge, fork
        self.statements, self.closed = [], False

    def close(self):
        self.closed = True

    def sql(self, statement, database=None):
        self.statements.append(statement)
        if "SELECT vc.head();" == statement:
            return HEAD
        if "SELECT current_user;" == statement:
            return "agentdb_client"
        if "vc.rows_at(" in statement and "SELECT doc" in statement:
            return json.dumps(self.product)
        if "'content',convert_from" in statement:
            return json.dumps({"revision": BASE, "path": "experiments/pricing.py", "content": self.pricing})
        if "experiments/pricing.py')" in statement:
            return self.pricing
        if "experiments/inventory.py')" in statement:
            return self.inventory
        if "vc.fork(" in statement:
            if self.fork == "exists":
                raise RuntimeError(BRANCH_EXISTS)  # What db.py's sql() raises for it.
            return NEW
        if "vc.finish_change()" in statement:
            return NEW
        if "vc.diff_rows(" in statement:
            return json.dumps({"rows": [], "files": [{"path": "experiments/pricing.py"}]})
        raise RuntimeError("unexpected statement\n" + statement)

    def execute(self, statement, database=None, timeout=120):
        self.statements.append(statement)
        if self.fork == "exists":
            return subprocess.CompletedProcess("psql", 3, "", BRANCH_EXISTS)
        if self.merge == "stale":
            return subprocess.CompletedProcess("psql", 3, "", "ERROR:  40001: stale head\n")
        if self.merge == "lock_timeout":
            return subprocess.CompletedProcess("psql", 3, "", "ERROR:  55P03: canceling statement due to lock timeout\n")
        # merge --reads prints the table names before BEGIN, so they arrive even on a refusal.
        tables = ("tables|" + json.dumps({PRODUCTS: "app.products", ORDERS: "app.orders"}) + "\n"
                  if "SELECT 'tables',json_build_object(" in statement else "")
        if self.merge in ("read_conflict", "unreadable_detail"):
            stderr = READ_CONFLICT if self.merge == "read_conflict" else READ_CONFLICT.replace("DETAIL", "NOTE")
            return subprocess.CompletedProcess("psql", 3, tables, stderr)
        if self.merge == "invalid_read_set":
            return subprocess.CompletedProcess("psql", 3, tables, INVALID_READ_SET)
        if self.merge == "not_found":
            return subprocess.CompletedProcess("psql", 3, tables, "ERROR:  P0002: Unknown source\n")
        return subprocess.CompletedProcess("psql", 0, tables + NEW + "\n", "")


COMMON = ["--database", DB]
READS = '{"app.products": [{"id": 101}], "app.orders": []}'
SCENARIOS = {
    "info": (["info", *COMMON], {}),
    "info_fixed": (["info", *COMMON, "--revision", BASE], {}),
    "read_code": (["read-code", *COMMON, "--path", "experiments/pricing.py", "--revision", BASE], {}),
    "read_code_missing_path": (["read-code", *COMMON, "--revision", BASE], {}),
    "fork": (["fork", *COMMON, "--branch", "gpt-a-1", "--base", BASE], {}),
    "fork_exists": (["fork", *COMMON, "--branch", "gpt-a-1", "--base", BASE], {"fork": "exists"}),
    "fork_bad_branch": (["fork", *COMMON, "--branch", "main", "--base", BASE], {}),
    "fork_bad_uuid": (["fork", *COMMON, "--branch", "gpt-a-1", "--base", "x' OR 1=1"], {}),
    "fork_missing_base": (["fork", *COMMON, "--branch", "gpt-a-1"], {}),
    "propose": (["propose", *COMMON, "--branch", "gpt-a-1", "--expected", BASE, "--actor", "gpt-a",
                 "--field", "price", "--value", "11", "--code", "PRICE = 11\n"], {}),
    "propose_missing": (["propose", *COMMON, "--branch", "gpt-a-1", "--expected", BASE, "--actor", "gpt-a"], {}),
    "propose_stock_fraction": (["propose", *COMMON, "--branch", "gpt-b-1", "--expected", BASE, "--actor", "gpt-b",
                                "--field", "stock", "--value", "94.5", "--code", "STOCK = 94.5\n"], {}),
    "propose_too_large": (["propose", *COMMON, "--branch", "gpt-a-1", "--expected", BASE, "--actor", "gpt-a",
                           "--field", "price", "--value", "11", "--code", "#" * 4097], {}),
    "propose_syntax": (["propose", *COMMON, "--branch", "gpt-a-1", "--expected", BASE, "--actor", "gpt-a",
                        "--field", "price", "--value", "11", "--code", "def ("], {}),
    "propose_range": (["propose", *COMMON, "--branch", "gpt-a-1", "--expected", BASE, "--actor", "gpt-a",
                       "--field", "price", "--value", "100001", "--code", "PRICE = 1\n"], {}),
    "propose_not_a_number": (["propose", *COMMON, "--branch", "gpt-a-1", "--expected", BASE, "--actor", "gpt-a",
                              "--field", "price", "--value", "abc", "--code", "PRICE = 1\n"], {}),
    "diff": (["diff", *COMMON, "--base", BASE, "--revision", NEW], {}),
    "merge": (["merge", *COMMON, "--revision", NEW, "--expected", BASE, "--actor", "gpt-a"], {}),
    "merge_stale": (["merge", *COMMON, "--revision", NEW, "--expected", BASE, "--actor", "gpt-b"], {"merge": "stale"}),
    "merge_lock_timeout": (["merge", *COMMON, "--revision", NEW, "--expected", BASE, "--actor", "gpt-a"],
                           {"merge": "lock_timeout"}),
    "merge_no_actor": (["merge", *COMMON, "--revision", NEW, "--expected", BASE], {}),
    "merge_reads": (["merge", *COMMON, "--revision", NEW, "--reads", READS, "--actor", "gpt-a"], {}),
    "merge_reads_conflict": (["merge", *COMMON, "--revision", NEW, "--reads", READS, "--actor", "gpt-b"],
                             {"merge": "read_conflict"}),
    "merge_reads_invalid": (["merge", *COMMON, "--revision", NEW, "--reads", '{"app.products": [{"sku": "x"}]}',
                             "--actor", "gpt-a"], {"merge": "invalid_read_set"}),
    "merge_reads_and_expected": (["merge", *COMMON, "--revision", NEW, "--expected", BASE, "--reads", READS,
                                  "--actor", "gpt-a"], {}),
    "merge_reads_unknown_table": (["merge", *COMMON, "--revision", NEW, "--reads", '{"app.users": [{"id": 1}]}',
                                   "--actor", "gpt-a"], {}),
    "info_reads": (["info", *COMMON, "--reads", READS], {}),
    "verify": (["verify", *COMMON], {"product": {"id": 101, "price": 11, "stock": 95}}),
    "verify_fail": (["verify", *COMMON], {}),
    "bad_database": (["info", "--database", "agentdb"], {}),
}


def run_cli(module, argv, worker):
    """Run module.main() in-process; return what a separate process would show."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with patch.object(module, "connect", lambda args, database, role=None: worker), \
            patch.object(sys, "argv", ["agent_lab.py", *argv]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            module.main()
        except SystemExit as stop:
            code = stop.code if isinstance(stop.code, int) else (0 if stop.code is None else 1)
        except Exception:
            traceback.print_exc()  # What the interpreter prints for an uncaught error.
            code = 1
    return dict(exit_code=code, stdout=out.getvalue(), stderr=err.getvalue(), statements=worker.statements)


def last_line(text):
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""
