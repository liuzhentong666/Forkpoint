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


class FakeWorker:
    """Answers the statements agent_lab sends; records them in order."""

    def __init__(self, product=None, pricing="PRICE = 11\n", inventory="STOCK = 95\n", merge=None):
        self.product = product or {"id": 101, "price": 10, "stock": 100}
        self.pricing, self.inventory, self.merge = pricing, inventory, merge
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
            return NEW
        if "vc.finish_change()" in statement:
            return NEW
        if "vc.diff_rows(" in statement:
            return json.dumps({"rows": [], "files": [{"path": "experiments/pricing.py"}]})
        raise RuntimeError("unexpected statement\n" + statement)

    def execute(self, statement, database=None, timeout=120):
        self.statements.append(statement)
        if self.merge == "stale":
            return subprocess.CompletedProcess("psql", 3, "", "ERROR:  40001: stale head\n")
        if self.merge == "lock_timeout":
            return subprocess.CompletedProcess("psql", 3, "", "ERROR:  55P03: canceling statement due to lock timeout\n")
        return subprocess.CompletedProcess("psql", 0, NEW + "\n", "")


COMMON = ["--database", DB]
SCENARIOS = {
    "info": (["info", *COMMON], {}),
    "info_fixed": (["info", *COMMON, "--revision", BASE], {}),
    "read_code": (["read-code", *COMMON, "--path", "experiments/pricing.py", "--revision", BASE], {}),
    "read_code_missing_path": (["read-code", *COMMON, "--revision", BASE], {}),
    "fork": (["fork", *COMMON, "--branch", "gpt-a-1", "--base", BASE], {}),
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
