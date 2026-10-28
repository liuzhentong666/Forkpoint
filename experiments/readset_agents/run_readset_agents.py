"""Run the read-set experiment with real agents (docs/READSET_AGENTS_EXPERIMENT.md): does Forkpoint's
read-set merge stop a stale decision more often than plain PostgreSQL used by agents told to lock and
check again?

The harness is experiments/task_state/run_state_trials.py's (Claude Code headless, one MCP tool per
agent through experiments/gpt_repeat/agentdb_mcp_server.py, every request and response kept as raw
JSONL, an SQL audit after every call that can write), except that both agents' processes run at once
and that their tool runs SQL, in one transaction per call that the runner opens and ends:
- P, plain PostgreSQL: a new agentdb_test_plain_<10 hex> database with sql/002_demo_app.sql's app
  tables, not enrolled, and the same 4,000 rows; query and execute.
- F, Forkpoint: agent_lab.py init; query, fork, execute (one change on the agent's newest branch) and
  merge, which issues agent_lab's merge --reads SQL (vc.merge_validated) so that the runner times its
  commit.
gpt-a sells 15 of product 101 and gpt-b 15 of product 202, each only if the bundle keeps 180: either
sale alone is allowed, both break the rule. Collision holds each agent's first write call (P execute,
F merge) for the other's, then runs the later one while the earlier one's change is in flight.
Rounds alternate P, F. --claude may name a .py stand-in (fake_readset_claude.py) for a dry run.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
GPT_REPEAT = ROOT / "experiments" / "gpt_repeat"
sys.path[:0] = [str(ROOT), str(GPT_REPEAT)]

import agent_lab  # noqa: E402
import db  # noqa: E402
from claude_scoring import trace_metrics  # noqa: E402
from claude_stream import ClaudeAgent, child_environment  # noqa: E402
from codex_rpc import wire  # noqa: E402
from repeat_scoring import wilson  # noqa: E402
from run_claude_repeats import ACTORS, Coordinator, Trial, check_init, claude_command, save, sha256  # noqa: E402
from run_gpt_repeats import connect_owner, run_helper  # noqa: E402

GROUPS = ("P", "F")                      # P: plain PostgreSQL; F: Forkpoint
PRODUCTS = {"gpt-a": 101, "gpt-b": 202}  # the product each agent is asked to sell
START, SALE, FLOOR = 100, 15, 180        # each product's stock at the start, one sale, the bundle's floor
SOLD = START - SALE                      # a product's stock once its sale landed
WRITES = {"P": "execute", "F": "merge"}  # the call that can change shared data
HOLD_SECONDS = 300       # a first write call waits at most this long for the other agent's
POLL_SECONDS = 0.05      # how often the later call is checked for a lock wait
COMMIT_SECONDS = 10      # the earlier call commits at most this long after the later one starts
LOCK_TIMEOUT = agent_lab.MERGE_LOCK_TIMEOUT  # "10s", for every call in both groups
CALL_BUDGET = 40         # tool calls per agent
TOOL_TIMEOUT = 600       # s: Claude Code's tool-call limit and the MCP server's wait, above hold, commit and lock waits
STATEMENT_SECONDS = 120  # an agent's statements, or the end of its transaction, as for agent_lab's helper calls
OUTPUT_BYTES = 8192      # psql output and errors returned to the agent; the rest is cut with a marker
SQL_BYTES = 16384

# The document's prompt, word for word: the common lines, then the group's lines without their
# "P: " or "F: " label (a test compares them with the document).
PROMPT = ("You are an order agent for a shop database. Products 101 and 202 are sold as a bundle:\n"
          "their combined stock must never fall below 180. No database constraint checks this.\n"
          "A customer wants 15 units of product {product}. Sell them by lowering product {product}'s stock by 15,\n"
          "but only if the bundle still has at least 180 units afterwards; otherwise do not sell.\n"
          "Change nothing else. Finish by saying whether you sold, and the stock values your decision used.\n"
          "Other agents and processes change this database at the same time as you, including the stock\n"
          "of other products. Your change must not rest on values that changed after you read them:\n")
GUARDS = {"P": "in the same execute call as the write, lock the rows your decision depends on\n"
               "(SELECT ... FOR UPDATE) and check the rule again before changing anything.",
          "F": "when you merge, list in reads every row your decision depends on; the merge is refused\n"
               "if one of them changed on main."}
# The general lines of the earlier experiments' instructions (run_gpt_repeats.INSTRUCTIONS), without
# those about their task (versioned policies, fixed versions, branches); the same in both groups.
INSTRUCTIONS = """You are a real Claude agent in a database experiment. Use only the agentdb tool; do not simulate results.
Database errors must guide your next decision; never claim success from a failed call.
Use at most 40 tool calls. Do not delegate, use shell, access files, networks, or credentials.
Only the database is authoritative.
"""
SQL_RULES = ("Table names without a schema are the shop's tables: products (id, sku, name, price, stock) and "
             "orders (id, product_id, quantity). The harness opens and ends each transaction: sql may begin with "
             "SET TRANSACTION ISOLATION LEVEL ..., but BEGIN, COMMIT, ROLLBACK and role changes are refused. "
             f"lock_timeout is {LOCK_TIMEOUT}. Results come back as psql prints them, at most {OUTPUT_BYTES} bytes; "
             "errors give their SQLSTATE. actor and database are bound by the harness.")
TOOLS = {
    "P": {"type": "function", "name": "agentdb", "description": (
        "Run SQL on this shop's PostgreSQL database. query runs sql in one read-only transaction; execute runs "
        "sql in one read-write transaction and commits it if every statement succeeds. " + SQL_RULES),
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["query", "execute"]}, "sql": {"type": "string"}},
            "required": ["action", "sql"], "additionalProperties": False}},
    "F": {"type": "function", "name": "agentdb", "description": (
        "Run SQL on this shop's Forkpoint database: PostgreSQL where you change a branch, then merge it into "
        "main. fork starts a new branch from current main and makes it your newest branch. query runs sql in "
        "one read-only transaction on your newest branch, or on current main if main is true or you have no "
        "branch yet. execute runs sql as one change on your newest branch. merge lands your newest branch on "
        'main; reads is required and may be empty: it lists every row your decision depends on, as {"products": '
        '[{"id": 1}]}. The merge is refused with read_conflict, listing each changed row\'s value when the branch '
        "started and now, if one of those rows, or a row the branch wrote, changed on main since the branch "
        "started. sql may not name the vc or app schemas. " + SQL_RULES),
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["query", "fork", "execute", "merge"]},
            "sql": {"type": "string"}, "main": {"type": "boolean"},
            "reads": {"type": "object", "additionalProperties": {"type": "array", "items": {"type": "object"}}}},
            "required": ["action"], "additionalProperties": False}}}
ARGUMENTS = {"query": {"sql", "main"}, "execute": {"sql"}, "fork": set(), "merge": {"reads"}}
READS_SHAPE = 'reads must map products or orders to arrays of primary keys, such as {"products": [{"id": 1}]}'
ROLE_REFUSAL = "role changes are refused: the tool runs as agentdb_client"
TURN_ENDED = {"error": "turn_ended", "detail": "Your turn ended before this call's hold did; it did not run."}
# P's database: sql/002_demo_app.sql's tables and role, agentdb_client's privileges on app as there,
# and the 4,000 rows review_local.seed inserts; nothing is registered with vc.
DEMO = (ROOT / "sql" / "002_demo_app.sql").read_text(encoding="utf-8")
PLAIN_SETUP = ("BEGIN;\n" + DEMO[DEMO.index("CREATE SCHEMA app;"):DEMO.index("SELECT vc.register_table")]
               + DEMO[DEMO.index("DO $$ BEGIN"):DEMO.index("GRANT USAGE")]
               + "GRANT USAGE ON SCHEMA app TO agentdb_client;\n"
               "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA app TO agentdb_client;\n"
               "INSERT INTO app.products SELECT n,'sku-'||n,'product-'||n,10,100 FROM generate_series(1,4000) n;\n"
               "COMMIT;\n")
# Every project file a run loads or runs (a test checks the loaded modules are all here).
SOURCES = ("experiments/readset_agents/run_readset_agents.py", "experiments/gpt_repeat/run_claude_repeats.py",
           "experiments/gpt_repeat/claude_stream.py", "experiments/gpt_repeat/agentdb_mcp_server.py",
           "experiments/gpt_repeat/claude_scoring.py", "experiments/gpt_repeat/repeat_scoring.py",
           "experiments/gpt_repeat/run_gpt_repeats.py", "experiments/gpt_repeat/codex_rpc.py",
           "agent_lab.py", "db.py", "review_local.py", "docs/READSET_AGENTS_EXPERIMENT.md",
           # agent_lab.py init installs the version engine; P's tables come from 002
           "sql/001_core.sql", "sql/002_demo_app.sql", "sql/003_manager_role.sql", "sql/004_merge_batch.sql",
           "sql/005_bounded_history.sql", "sql/006_history_health.sql", "sql/007_search_path_pg_temp.sql",
           "sql/008_read_set_merge.sql", "sql/009_canonical_row_docs.sql", "sql/010_unknown_branch_code.sql")

WORD = re.compile(r"[A-Za-z_\u0080-\uffff][A-Za-z0-9_$\u0080-\uffff]*")
DOLLAR = re.compile(r"\$(?:[A-Za-z_\u0080-\uffff][A-Za-z0-9_\u0080-\uffff]*)?\$")
TRANSACTION_WORDS = {"begin", "start", "commit", "end", "rollback", "abort"}
MODE_WORDS = {"isolation", "level", "serializable", "repeatable", "read", "committed", "uncommitted",
              "write", "only", "not", "deferrable"}
# On a statement's words (keywords and identifiers, as words() joins them):
LOCKING = re.compile(r"\bfor (?:update|no key update|share|key share)\b")
WRITES_PRODUCTS = re.compile(r"\b(?:update(?: only)?|insert into|delete from|merge into) (?:app )?products\b")
ISOLATION = re.compile(r"\blevel (serializable|repeatable read|read committed|read uncommitted)\b")


def prompt_for(group, actor):
    return PROMPT.format(product=PRODUCTS[actor]) + GUARDS[group]


def closing(sql, i, quote, escapes=False):
    """Index of the quote that ends a literal whose contents start at i (len(sql) if none does)."""
    while i < len(sql):
        if escapes and sql[i] == "\\":
            i += 2
        elif sql[i] == quote and sql[i + 1:i + 2] == quote:
            i += 2
        elif sql[i] == quote:
            return i
        else:
            i += 1
    return len(sql)


def split_sql(sql):
    """sql's statements as PostgreSQL's lexer separates them: [(text, tokens)], a token being ("word",
    lowercased), ("quoted", identifier), ("string", contents) or ("punct", character). A semicolon in a
    comment, string, quoted identifier or dollar quote does not end a statement; empty ones are left out."""
    statements, tokens, start, i, n = [], [], 0, 0, len(sql)
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end < 0 else end + 1
        elif sql.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                step = sql[i:i + 2]
                depth += {"/*": 1, "*/": -1}.get(step, 0)
                i += 2 if step in ("/*", "*/") else 1
        elif c == "'" or (c in "eEbBxXnN" and sql[i + 1:i + 2] == "'") or (c in "uU" and sql[i + 1:i + 3] == "&'"):
            begin = sql.index("'", i) + 1
            end = closing(sql, begin, "'", escapes=c in "eE")
            tokens.append(("string", sql[begin:end]))
            i = end + 1
        elif c == '"' or (c in "uU" and sql[i + 1:i + 3] == '&"'):
            begin = sql.index('"', i) + 1
            end = closing(sql, begin, '"')
            tokens.append(("quoted", sql[begin:end].replace('""', '"')))
            i = end + 1
        elif DOLLAR.match(sql, i):
            tag = DOLLAR.match(sql, i).group(0)
            end = sql.find(tag, i + len(tag))
            end = n if end < 0 else end
            tokens.append(("string", sql[i + len(tag):end]))
            i = end + len(tag)
        elif WORD.match(sql, i):
            word = WORD.match(sql, i).group(0)
            tokens.append(("word", word.lower()))
            i += len(word)
        elif c == ";":
            if tokens:
                statements.append((sql[start:i], tokens))
            tokens, start, i = [], i + 1, i + 1
        else:
            tokens.append(("punct", c))
            i += 1
    if tokens:
        statements.append((sql[start:], tokens))
    return statements


def refusal(tokens, group):
    """Why the tool refuses this statement, or None. The runner logs in as a superuser and sets the role,
    so a role change would leave agentdb_client."""
    lead = [value for kind, value in tokens[:2] if kind == "word"]
    if tokens[0] in {("word", w) for w in TRANSACTION_WORDS} or lead == ["prepare", "transaction"]:
        return "BEGIN, COMMIT and ROLLBACK are refused: the harness opens and ends each transaction"
    if tokens[0] in (("word", "set"), ("word", "reset")):
        setting = [value.lower() for kind, value in tokens[1:4] if kind in ("word", "quoted")]
        if setting[:1] in (["session"], ["local"]) and setting[1:2] != ["authorization"]:
            setting = setting[1:]
        if setting[:1] in (["role"], ["session_authorization"]) or setting[:2] == ["session", "authorization"]:
            return ROLE_REFUSAL
    if ("word", "set_config") in tokens and {"role", "session_authorization"} & {
            value.lower() for kind, value in tokens if kind == "string"}:
        return ROLE_REFUSAL
    if group == "F" and any(kind in ("word", "quoted") and value in ("vc", "app") for kind, value in tokens):
        return "sql may not name the vc or app schemas"
    return None


def transaction_modes(tokens):
    """A SET TRANSACTION statement of transaction modes only, rendered from its words, else None."""
    words = [value for kind, value in tokens if kind == "word"]
    if (words[:2] != ["set", "transaction"] or len(words) < 3 or not set(words[2:]) <= MODE_WORDS
            or any(token != ("punct", ",") for token in tokens if token[0] != "word")):
        return None
    return " ".join(value.upper() if kind == "word" else "," for kind, value in tokens)


def plan_sql(sql, group):
    """(prelude, body) for the agent's sql, or a refusal. prelude holds its leading SET TRANSACTION
    statements, which the runner sends right after BEGIN (its own queries come before the agent's
    statements); body holds the other statements' text, which psql sends as it is."""
    if not isinstance(sql, str) or not sql.strip():
        return {"error": "invalid_arguments", "detail": "sql is required"}
    if len(sql.encode("utf-8")) > SQL_BYTES:
        return {"error": "invalid_arguments", "detail": f"sql exceeds {SQL_BYTES} bytes"}
    statements = split_sql(sql)
    if not statements:
        return {"error": "invalid_arguments", "detail": "sql has no statement"}
    for _, tokens in statements:
        why = refusal(tokens, group)
        if why:
            return {"error": "refused_statement", "detail": why}
    prelude = []
    while statements and transaction_modes(statements[0][1]):
        prelude.append(transaction_modes(statements.pop(0)[1]))
    return prelude, [text for text, _ in statements]


def words(tokens):
    """A statement's keywords and identifiers in order: no strings, comments or punctuation."""
    return " ".join(value.lower() for kind, value in tokens if kind in ("word", "quoted"))


def p_guard(sql):
    """P's guard as the SQL of the later writer's first execute wrote it (the document's note of
    2026-10-28): locked_before_write, whether a statement before the first one that writes products
    has a locking clause (FOR UPDATE, FOR NO KEY UPDATE, FOR SHARE or FOR KEY SHARE; a lock in the
    writing statement itself, as in a CTE, does not count; None when no statement writes products
    outside a string, as inside a DO block), and isolation, the level its leading SET TRANSACTION
    statements set (read committed if none)."""
    statements = split_sql(sql) if isinstance(sql, str) else []
    isolation = "read committed"
    while statements and transaction_modes(statements[0][1]):
        level = ISOLATION.search(words(statements.pop(0)[1]))
        isolation = level.group(1) if level else isolation
    texts = [words(tokens) for _, tokens in statements]
    write = next((index for index, text in enumerate(texts) if WRITES_PRODUCTS.search(text)), None)
    return {"locked_before_write": None if write is None else any(LOCKING.search(text) for text in texts[:write]),
            "isolation": isolation}


def read_set(reads):
    """agent_lab's --reads text for the agent's reads, which name tables as its sql does; ValueError if
    they are not a read set."""
    if (not isinstance(reads, dict) or not set(reads) <= {"products", "orders"} or not all(
            isinstance(keys, list) and all(isinstance(key, dict) and key for key in keys) for keys in reads.values())):
        raise ValueError(READS_SHAPE)
    text = json.dumps({"app." + table: keys for table, keys in reads.items()})
    agent_lab.read_set(text)  # its size limit; the engine checks each key against the primary key
    return text


def opening(group, prelude, read_only):
    """BEGIN, the agent's transaction modes, then the runner's settings for the agent's statements."""
    return ("BEGIN;\n" + "".join(mode + ";\n" for mode in prelude)
            + ("SET TRANSACTION READ ONLY;\n" if read_only else "")
            + f"SET LOCAL search_path = {'app' if group == 'P' else 'agent'};\n"
            + f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}';\n")


def agent_part(body):
    """psql lines that run each statement as the agent wrote it (\\gexec sends it without psql reading
    it) and print its results as psql does, with column names and command tags."""
    return ("\\pset format aligned\n\\pset tuples_only off\n\\set QUIET off\n"
            + "".join(f"SELECT convert_from(decode('{text.encode('utf-8').hex()}','hex'),'UTF8') \\gexec\n"
                      for text in body)
            + "\\set QUIET on\n\\pset tuples_only on\n\\pset format unaligned\n")


def clip(text):
    """text cut to OUTPUT_BYTES, with a marker saying how much was left out."""
    data = text.encode("utf-8")
    if len(data) <= OUTPUT_BYTES:
        return text
    return data[:OUTPUT_BYTES].decode("utf-8", "ignore") + f"\n[{len(data) - OUTPUT_BYTES} more bytes not shown]\n"


class Transaction:
    """One call's transaction in its own psql process, as agentdb_client: begin() sends its statements,
    end() its end (COMMIT, or ROLLBACK after a query) once the runner says. psql stops at the first error
    (ON_ERROR_STOP, as in every agent_lab call), which ends the process and so rolls the transaction
    back. The process starts, and reports its backend's pid, before the call is held."""

    def __init__(self, owner, database, statements, end):
        self.owner, self.statements, self.end_script = owner, statements, end
        self.process = subprocess.Popen(owner.client_args(database), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, env=owner.env, creationflags=db.FLAGS)
        self.lines = {name: queue.Queue() for name in ("stdout", "stderr")}
        self.drains = [threading.Thread(target=self._drain, args=(name,), daemon=True) for name in self.lines]
        for thread in self.drains:
            thread.start()
        self.result = self.ended = self.begun = None
        self.timed_out = False
        self._send("SELECT pg_backend_pid();")
        started = self._receive(STATEMENT_SECONDS)
        if started is None or started.returncode:
            self.close()
            raise RuntimeError("psql did not start: " + (started.stderr if started else "timeout"))
        self.pid = int(started.stdout.strip())

    def _drain(self, name):
        for raw in iter(getattr(self.process, name).readline, b""):
            self.lines[name].put(raw.decode("utf-8", "replace").replace("\r\n", "\n"))
        self.lines[name].put(None)

    def _send(self, script):
        self.marker = "__readset_" + uuid.uuid4().hex
        self.out, self.err, self.done = [], [], dict.fromkeys(self.lines, None)
        try:
            self.process.stdin.write((script + f"\n\\echo {self.marker}\n\\warn {self.marker}\n").encode("utf-8"))
            self.process.stdin.flush()
        except OSError:
            pass  # psql has exited; _receive reports what it printed

    def _receive(self, timeout):
        """The last script's result once psql printed its marker on both streams or exited, else None."""
        end = time.monotonic() + timeout
        for name, lines in (("stdout", self.out), ("stderr", self.err)):
            while self.done[name] is None:
                try:
                    line = self.lines[name].get(timeout=max(0.0, end - time.monotonic()))
                except queue.Empty:
                    return None
                if line is None or line.startswith(self.marker):
                    self.done[name] = "exited" if line is None else "marker"
                else:
                    lines.append(line)
        code = 0 if self.done["stdout"] == "marker" else (self.process.wait(timeout=30) or 3)
        return subprocess.CompletedProcess(self.process.args, code, "".join(self.out), "".join(self.err))

    def _limit(self):
        """End a backend that ran past STATEMENT_SECONDS (its transaction rolls back); what psql printed."""
        self.timed_out = True
        self.owner.sql(f"SELECT pg_terminate_backend({self.pid});")
        return self._receive(30) or subprocess.CompletedProcess(self.process.args, 124, "".join(self.out), "".join(self.err))

    def begin(self):
        self.begun = time.monotonic()
        self._send("SET ROLE agentdb_client;\n" + self.statements)

    def poll(self, timeout):
        """The statements' result once they have run, or psql stopped at an error; None while they run."""
        if self.result is None:
            self.result = self._receive(timeout)
        return self.result

    def settle(self):
        """The statements' result, waiting at most STATEMENT_SECONDS from begin()."""
        if self.poll(max(0.0, self.begun + STATEMENT_SECONDS - time.monotonic())) is None:
            self.result = self._limit()
        return self.result

    def end(self):
        """Once the statements have run, COMMIT or ROLLBACK unless psql stopped at an error; then close psql.
        Returns the end's result, or None when nothing was left to end."""
        try:
            if self.settle().returncode == 0:
                self._send(self.end_script)
                self.ended = self._receive(STATEMENT_SECONDS) or self._limit()
            return self.ended
        finally:
            self.close()

    def run(self):
        """A call that is not held: its statements, then its end."""
        self.begin()
        return self.end()

    def close(self):
        if self.process.poll() is None:
            try:
                self.process.stdin.close()
                self.process.wait(timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()
                self.process.wait(timeout=30)
        for thread in self.drains:
            thread.join(timeout=5)
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                pipe.close()
            except OSError:
                pass


class Collision:
    """The hold and the commit order of the two agents' first write calls (the document's "Task").

    write() runs an agent's first write call, a Transaction. The call waits until the other agent's first
    write call arrives, the other agent's turn ends, or `hold` seconds pass. Released by the other's
    arrival it is the earlier call: its statements run in a transaction left open, then the later call's
    statements start, and the earlier one commits as soon as the later finishes or is seen waiting for a
    lock (`waiting`, checked every `poll` seconds), at most `commit` seconds after the later started; the
    later ends its own transaction after that. Released otherwise it runs and commits alone, as does a
    call that arrives after the other's was released. A call whose own agent's turn has ended by its
    release (the turn was interrupted) does not run.
    """

    def __init__(self, waiting, hold=HOLD_SECONDS, poll=POLL_SECONDS, commit=COMMIT_SECONDS):
        self.waiting, self.hold, self.poll, self.commit = waiting, hold, poll, commit
        self.cond = threading.Condition()
        self.calls, self.ended = {}, set()  # each actor's first write call; actors whose turn ended
        self.bound = STATEMENT_SECONDS + commit + 60  # no wait on the other call is longer

    def turn_ended(self, actor):
        with self.cond:
            self.ended.add(actor)
            self.cond.notify_all()

    def write(self, actor, transaction):
        """Run actor's first write call; return a copy of its record."""
        other = next(a for a in ACTORS if a != actor)
        with self.cond:
            mine = self.calls[actor] = {"arrived_ns": time.time_ns()}
            theirs = self.calls.get(other)
            if theirs is None:
                self._wait(lambda: "release" in mine or other in self.ended, self.hold)
                role = "earlier" if "release" in mine else "alone"
                mine.setdefault("release", "other_turn_ended" if other in self.ended else "hold_cap")
            elif "release" not in theirs:  # the other agent's call is held: this is the later call
                theirs["release"], mine["release"], role = "other_arrived", "not_held", "later"
                self.cond.notify_all()
            else:
                mine["release"], role = "not_held", "alone"
            if actor in self.ended:
                if role == "later":  # the earlier call need not wait for this one
                    theirs["commit_reason"] = "later_abandoned"
                role = "abandoned"
            mine.update(released_ns=time.time_ns(), role=role)
        try:
            if mine["role"] == "later":
                self._later(mine, theirs, transaction)
            elif mine["role"] == "abandoned":
                self._done(mine, False)
            else:
                self._first(mine, transaction, mine["role"] == "earlier")
        finally:
            transaction.close()
        with self.cond:
            return dict(mine)

    def _wait(self, ready, seconds):
        """Wait (holding cond) until ready() or seconds pass; return ready()."""
        deadline = time.monotonic() + seconds
        while not ready() and time.monotonic() < deadline:
            self.cond.wait(deadline - time.monotonic())
        return ready()

    def _note(self, record, **values):
        with self.cond:
            record.update(values)
            self.cond.notify_all()

    def _done(self, record, ok):
        self._note(record, statements_ok=ok, statements_done_ns=time.time_ns())

    def _first(self, mine, transaction, earlier):
        ok = False
        try:
            transaction.begin()
            self._note(mine, started_ns=time.time_ns())
            ok = transaction.settle().returncode == 0
        finally:
            self._done(mine, ok)
        if earlier and ok:
            with self.cond:
                if not self._wait(lambda: "commit_reason" in mine, self.bound):
                    mine["commit_reason"] = "runner_limit"
        try:
            transaction.end()
        finally:
            self._note(mine, ended_ns=time.time_ns())

    def _later(self, mine, theirs, transaction):
        with self.cond:
            self._wait(lambda: "statements_done_ns" in theirs, self.bound)
            mine["in_flight"] = in_flight = bool(theirs.get("statements_ok"))
        reason = None
        try:
            transaction.begin()
            self._note(mine, started_ns=time.time_ns())
            started = time.monotonic()
            while in_flight and reason is None:
                if transaction.poll(self.poll) is not None:
                    reason = "later_finished"
                elif self.waiting(transaction.pid):
                    reason = "later_waiting"
                elif time.monotonic() - started >= self.commit:
                    reason = "commit_cap"
        finally:
            if in_flight:
                with self.cond:
                    theirs["commit_reason"] = reason or "runner_error"
                    mine["waited_for_lock"] = reason == "later_waiting"
                    self.cond.notify_all()
                    self._wait(lambda: "ended_ns" in theirs, self.bound)
        self._done(mine, transaction.settle().returncode == 0)
        try:
            transaction.end()
        finally:
            self._note(mine, ended_ns=time.time_ns())


class Abandoned(Exception):
    """The merge did not run: its agent's turn had ended before its hold did."""


class Merging:
    """The worker agent_lab.perform merges through: its statement up to COMMIT runs in a Transaction that
    the runner ends, through the Collision for the agent's first merge."""

    def __init__(self, trial, actor, note, first):
        self.trial, self.actor, self.note, self.first = trial, actor, note, first

    def execute(self, statement):
        before, after = re.split(r"(?m)^[ \t]*COMMIT;[ \t]*$", statement, maxsplit=1)
        transaction = Transaction(self.trial.owner, self.trial.database, before, "COMMIT;" + after)
        if not self.trial.run_transaction(self.actor, transaction, self.note, self.first):
            raise Abandoned()
        parts = [part for part in (transaction.result, transaction.ended) if part is not None]
        return subprocess.CompletedProcess(statement, next((p.returncode for p in parts if p.returncode), 0),
                                           "".join(p.stdout for p in parts), "".join(p.stderr for p in parts))


def sql_reply(transaction, action):
    """What the agent gets for query or execute: psql's output and, on failure, the error and its SQLSTATE."""
    result, ended = transaction.result, transaction.ended
    reply = {"output": clip(result.stdout)}
    if transaction.timed_out:
        return {"error": "statement_limit", "detail": f"The statements ran longer than {STATEMENT_SECONDS} s; "
                "the harness ended the transaction.", **reply}
    failed = result if result.returncode else ended if ended is not None and ended.returncode else None
    if failed is not None:
        return {"error": "database_error", "sqlstate": db.sqlstate(failed.stderr) or "unknown",
                "detail": clip(failed.stderr.strip()), **reply}
    if result.stderr.strip():
        reply["messages"] = clip(result.stderr.strip())
    if action == "execute":
        reply["committed"] = True
    return reply


def audit_sql(group):
    """The audit after every call that can write, and at the end: the bundle's rows, whether everything
    else is as seeded, and other client sessions in a transaction; in F also main's head and the branches."""
    extra = ",\n  'main',vc.head(),'branches',(SELECT json_agg(b ORDER BY b.name) FROM vc.branch b)" if group == "F" else ""
    return f"""SELECT json_build_object(
  'bundle',(SELECT json_object_agg(id,row_to_json(p)) FROM app.products p WHERE id IN (101,202)),
  'rows',(SELECT count(*) FROM app.products),
  'other_rows_unchanged',(SELECT bool_and(price=10 AND stock=100 AND sku='sku-'||id AND name='product-'||id)
    FROM app.products WHERE id NOT IN (101,202)),
  'orders',(SELECT count(*) FROM app.orders),
  'open_transactions',(SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()
    AND backend_type='client backend' AND pid<>pg_backend_pid() AND xact_start IS NOT NULL){extra});"""


def unchanged(final):
    """Everything but the two stocks as seeded: 4,000 products, the bundle's other columns, the other
    3,998 rows, and no orders."""
    bundle = final.get("bundle") or {}
    return bool(final.get("rows") == 4000 and final.get("other_rows_unchanged") and final.get("orders") == 0
                and all((bundle.get(str(p)) or {}).get("sku") == f"sku-{p}" and (bundle.get(str(p)) or {}).get("name")
                        == f"product-{p}" and (bundle.get(str(p)) or {}).get("price") == 10 for p in PRODUCTS.values()))


def outcome(final):
    """stale: both sales landed (the primary metric); correct: exactly one sale landed and nothing else
    changed; other: no sale, or any other change."""
    stocks = [((final.get("bundle") or {}).get(str(p)) or {}).get("stock") for p in PRODUCTS.values()]
    if stocks == [SOLD, SOLD]:
        return "stale"
    return "correct" if None not in stocks and sorted(stocks) == [SOLD, START] and unchanged(final) else "other"


def succeeded(summary, final):
    """The registered success rule on the final audit; run_trial adds that both turns completed, every
    response came from the registered model and every MCP request was answered."""
    return outcome(final) == "correct" and final.get("open_transactions") == 0 and not summary["audit_errors"]


class ReadsetTrial(Trial):
    """Trial (run_claude_repeats) with this experiment's tool. A call is recorded and checked under the
    round's lock, then runs outside it, so the two agents' calls run at once (Trial.handle ran one call
    per round at a time); each agent's MCP server still forwards one call at a time."""

    budget = CALL_BUDGET

    def __init__(self, args, folder, summary, group):
        super().__init__(args, folder, summary)
        self.group = group
        self.collision = Collision(self.waiting)
        self.worker = self.monitor = self.auditor = None
        self.branches = {actor: [] for actor in ACTORS}  # F: each agent's branches, oldest first
        self.first_writes, self.running = set(), 0

    def handle(self, actor, params):
        with self.lock:
            self.sequence += 1
            values = params.get("arguments")
            record = {"call_id": f"{self.sequence:03d}-{actor}", "sequence": self.sequence, "actor": actor,
                      "tool": params.get("name"), "arguments": values, "started_ns": time.time_ns(),
                      "tool_use_id": (params.get("_meta") or {}).get("claudecode/toolUseId")}
            self.summary["calls"].append(record)
            refusal = self.count_call(actor, params) or self.refuse(actor, values)
            if refusal is not None:
                record.update(refusal, finished_ns=time.time_ns())
                save(self.folder / "result.json", self.summary)
                return refusal
            first = values["action"] == WRITES[self.group] and actor not in self.first_writes
            if first:
                self.first_writes.add(actor)
            record["executed"], self.running = True, self.running + 1
        note, reply = {}, None
        try:
            reply = self.run_call(actor, values, note, first, record["call_id"])
        except Exception as error:  # reported to the agent by the coordinator, as in Trial
            note.update(error=type(error).__name__, detail=str(error))
            raise
        finally:
            with self.lock:
                if reply is not None:
                    note.update(error=reply.get("error"), sqlstate=reply.get("sqlstate"))
                record.update(note, finished_ns=time.time_ns())
                self.running -= 1
                save(self.folder / "result.json", self.summary)
        return reply

    def refuse(self, actor, values):
        """A refusal for a call the tool does not take, else None."""
        actions = TOOLS[self.group]["inputSchema"]["properties"]["action"]["enum"]
        if not isinstance(values, dict):
            return {"error": "invalid_arguments"}
        action = values.get("action")
        if action not in actions:
            return {"error": "unknown_action"}
        extra = set(values) - {"action"} - (ARGUMENTS[action] - ({"main"} if self.group == "P" else set()))
        if extra:
            return {"error": "invalid_arguments", "detail": f"{action} does not take {', '.join(sorted(extra))}"}
        if not isinstance(values.get("main", False), bool):
            return {"error": "invalid_arguments", "detail": "main must be true or false"}
        if action in ("query", "execute"):
            plan = plan_sql(values.get("sql"), self.group)
            if isinstance(plan, dict):
                return plan
        if self.group == "F" and action in ("execute", "merge") and not self.branches[actor]:
            return {"error": "no_branch", "detail": "fork first: execute and merge use your newest branch"}
        if action == "merge":
            try:
                read_set(values.get("reads"))
            except ValueError as error:
                return {"error": "invalid_arguments", "detail": str(error)}
        return None

    def run_call(self, actor, values, note, first, call_id):
        action = values["action"]
        try:
            if action == "fork":
                return self.fork(actor, note)
            if action == "merge":
                return self.merge(actor, values, note, first)
            return self.sql_call(actor, action, values, note, first)
        finally:
            # Includes failed calls: catch a committed effect even if the reply failed.
            if action != "query":
                self.audit_after(note, actor, action, call_id)

    def run_transaction(self, actor, transaction, note, first):
        """Run a call's transaction, through the Collision for the agent's first write call. False if the
        call did not run (its agent's turn had ended)."""
        try:
            if not first:
                transaction.run()
                return True
            note["hold"] = self.collision.write(actor, transaction)
            return note["hold"]["role"] != "abandoned"
        finally:
            transaction.close()

    def sql_call(self, actor, action, values, note, first):
        prelude, body = plan_sql(values["sql"], self.group)
        read_only = action == "query"
        statements, end = opening(self.group, prelude, read_only), ("ROLLBACK;" if read_only else "COMMIT;")
        if self.group == "F":
            with self.lock:
                branch = "main" if read_only and values.get("main") else (self.branches[actor] or ["main"])[-1]
            name = agent_lab.literal(branch)
            note["branch"] = branch
            statements += (f"SELECT vc.checkout({name}) AS checkout \\gset\n" if read_only else
                           f"SELECT vc.begin_change({name},vc.head({name}),{agent_lab.literal(actor)},'execute') "
                           "AS draft \\gset\n")
        statements += agent_part(body)
        if self.group == "F" and not read_only:
            statements += "SELECT vc.finish_change() AS revision \\gset\n"
            end = "COMMIT;\nSELECT :'revision';"
        transaction = Transaction(self.owner, self.database, statements, end)
        if not self.run_transaction(actor, transaction, note, first):
            return TURN_ENDED
        reply = sql_reply(transaction, action)
        if self.group == "F" and not read_only and "error" not in reply:
            reply["revision"] = transaction.ended.stdout.strip().splitlines()[-1]
        return reply

    def fork(self, actor, note):
        """agent_lab's fork (vc.fork and its refusals) of the agent's next branch from current main."""
        with self.lock:
            name = f"{actor}-{len(self.branches[actor]) + 1}"
        created, refusal = agent_lab.checked(self.worker, f"SELECT vc.fork({agent_lab.literal(name)},vc.head());",
                                             agent_lab.FORK_ERRORS)
        if refusal:
            return json.loads(refusal)
        with self.lock:
            self.branches[actor].append(name)
        note["branch"] = name
        return {"branch": name, "base": created}

    def merge(self, actor, values, note, first):
        """agent_lab's merge --reads (its SQL, refusals and changed rows) of the newest branch's head, with
        the commit timed by the runner."""
        with self.lock:
            branch = self.branches[actor][-1]
        source = self.worker.sql(f"SELECT vc.head({agent_lab.literal(branch)});")
        note.update(branch=branch, source=source)
        options = argparse.Namespace(action="merge", revision=source, expected=None, actor=actor,
                                     reads=read_set(values["reads"]), database=self.database)
        try:
            text, _ = agent_lab.perform(options, Merging(self, actor, note, first))
        except Abandoned:
            return TURN_ENDED
        reply = json.loads(text)
        for row in reply.get("changed") or []:
            row["table"] = str(row.get("table")).removeprefix("app.")  # the names the agent's sql uses
        return reply

    def audit(self):
        return json.loads(self.auditor.sql(audit_sql(self.group)))

    def audit_after(self, note, actor, action, call_id):
        try:
            snapshot = self.audit()
        except Exception as audit_error:
            with self.lock:
                self.summary["audit_errors"].append({"call_id": call_id, "error": str(audit_error)})
            raise
        with self.lock:
            self.audit_log.write(wire({"call_id": call_id, "actor": actor, "action": action, "snapshot": snapshot}))
            self.audit_log.flush()
        note["audit_recorded"] = True

    def waiting(self, pid):
        """Whether backend pid waits for a lock (pg_blocking_pids), asked on the monitor's own session."""
        return self.monitor.sql(f"SELECT cardinality(pg_blocking_pids({int(pid)})) > 0;") == "t"

    def wait_for_calls(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            with self.lock:
                if not self.running:
                    return True
            time.sleep(0.1)
        return False


def create_database(args, group, folder):
    """The round's database: agent_lab.py init for F; for P a new agentdb_test_plain_<10 hex> (PLAIN_SETUP)."""
    if group == "F":
        initial = run_helper(args, None, "init")
        save(folder / "setup.json", initial)
        if initial["exit_code"]:
            raise RuntimeError("Database setup failed: " + initial["stderr"])
        return initial["result"]["database"]
    name = "agentdb_test_plain_" + uuid.uuid4().hex[:10]
    owner = connect_owner(args, name)
    owner.check()
    owner.create_database(name)
    owner.sql(PLAIN_SETUP)
    save(folder / "setup.json", {"database": name, "setup_sql": PLAIN_SETUP})
    return name


def mcp_servers(args, port, token, raw, actor, tool_file):
    """The agent's MCP config: agentdb_mcp_server.py serving its group's tool. Claude Code 2.1.308 reads a
    stdio server's "timeout" (ms) before MCP_TOOL_TIMEOUT (whose own default is 1e8 ms), and caps its
    idle limit for stdio servers (1,800,000 ms by default) by it."""
    server = [str(GPT_REPEAT / "agentdb_mcp_server.py"), "--port", str(port), "--token", token, "--raw", str(raw),
              "--actor", actor, "--timeout", str(TOOL_TIMEOUT), "--tool-file", str(tool_file)]
    return {"mcpServers": {"agentdb": {"type": "stdio", "command": args.python, "args": server, "env": {},
                                       "timeout": TOOL_TIMEOUT * 1000}}}


def take_turn(args, trial, agent, actor, prompt):
    """An agent's one turn; the Collision learns when it ends. A failure comes back as a result event."""
    started = time.monotonic()
    try:
        event = agent.turn(prompt, args.turn_timeout)
    except Exception as error:  # the turn timed out, or Claude Code exited
        event = {"type": "result", "subtype": "runner_error", "is_error": True, "result": f"{type(error).__name__}: {error}"}
    trial.collision.turn_ended(actor)
    ok = event.get("subtype") == "success" and not event.get("is_error")
    with trial.lock:
        trial.summary["agents"][actor].update(turn_seconds=round(time.monotonic() - started, 3), turn={
            "status": "completed" if ok else "failed", "error": None if ok else event.get("result"), "result": event})
        save(trial.folder / "result.json", trial.summary)
    print(f"run {trial.summary['run']:02d} ({trial.group}): {actor} -> {'completed' if ok else 'failed'}", flush=True)
    return event


def collision_summary(group, calls):
    """Whether both agents made a first write call, which wrote later, whether the earlier one's change
    was in flight while the later ran, and the later writer's guard: P, the guard its first execute's
    SQL wrote (p_guard); F, whether its reads named the other product. Either way with the call's
    error and SQLSTATE, if it failed."""
    holds = {call["actor"]: call for call in calls if "hold" in call}
    roles = {actor: call["hold"]["role"] for actor, call in holds.items()}
    later = next((actor for actor, role in roles.items() if role == "later"), None)
    found = {"both_wrote": len(holds) == len(ACTORS) and "abandoned" not in roles.values(), "roles": roles,
             "later": later, "release": {actor: call["hold"]["release"] for actor, call in holds.items()},
             "held_seconds": {actor: round((call["hold"]["released_ns"] - call["hold"]["arrived_ns"]) / 1e9, 3)
                              for actor, call in holds.items()}}
    if later:
        call, earlier = holds[later], next(actor for actor in ACTORS if actor != later)
        found.update(in_flight=call["hold"].get("in_flight"), commit_reason=holds[earlier]["hold"].get("commit_reason"))
        guard = {"error": call.get("error"), "sqlstate": call.get("sqlstate")}
        if group == "P":
            guard.update(p_guard((call.get("arguments") or {}).get("sql")))
        else:
            reads = (call.get("arguments") or {}).get("reads")
            guard.update(reads=reads, named_other=any(isinstance(key, dict) and str(key.get("id")) == str(PRODUCTS[earlier])
                                                      for key in (reads or {}).get("products") or []))
        found["later_guard"] = guard
    return found


def refusals(calls):
    """Each refused or failed call, and the action its agent took next."""
    found = []
    for index, call in enumerate(calls):
        if call.get("error"):
            following = next((c for c in calls[index + 1:] if c["actor"] == call["actor"]), {})
            arguments = following.get("arguments")
            found.append({"call_id": call["call_id"], "actor": call["actor"], "error": call["error"],
                          "sqlstate": call.get("sqlstate"),
                          "next": arguments.get("action") if isinstance(arguments, dict) else None})
    return found


def run_trial(args, number, group):
    folder = args.output / f"run-{number:02d}"
    folder.mkdir(exist_ok=False)
    raw = folder / "raw"
    raw.mkdir()
    workdir = args.workdir / f"run-{number:02d}"
    workdir.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    summary = {"run": number, "group": group, "requested_model": args.model, "reasoning_effort": args.effort,
               "status": "running", "success": False, "real_cloud_verified": False, "agents": {}, "calls": [],
               "audit_errors": []}
    save(folder / "result.json", summary)
    trial = ReadsetTrial(args, folder, summary, group)
    server, agents = None, {}
    try:
        trial.database = summary["database"] = create_database(args, group, folder)
        trial.owner = connect_owner(args, trial.database)
        trial.worker = agent_lab.connect(argparse.Namespace(config=args.config, password=False), trial.database,
                                         "agentdb_client")
        trial.monitor, trial.auditor = trial.owner.session(trial.database), trial.owner.session(trial.database)
        trial.audit_log = (folder / "database_audit.jsonl").open("xb")
        trial.audit_log.write(wire({"stage": "initial", "snapshot": trial.audit()}))
        trial.audit_log.flush()
        server = Coordinator(trial)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        tool_file = folder / "tool.json"
        save(tool_file, TOOLS[group])
        env = child_environment(args.config_dir, args.child_env)
        for actor in ACTORS:
            token = secrets.token_hex(16)
            trial.tokens[token] = actor
            mcp_config = folder / f"{actor}.mcp-config.json"
            save(mcp_config, mcp_servers(args, server.server_address[1], token, raw, actor, tool_file))
            agents[actor] = ClaudeAgent(claude_command(args, mcp_config, INSTRUCTIONS), env, workdir, raw, actor)
            summary["agents"][actor] = {"pid": agents[actor].process.pid, "product": PRODUCTS[actor]}
        prompts = {actor: prompt_for(group, actor) for actor in ACTORS}
        save(folder / "prompts.json", prompts)
        with ThreadPoolExecutor(max_workers=len(ACTORS)) as pool:  # both agents start at the same time
            turns = {actor: pool.submit(take_turn, args, trial, agents[actor], actor, prompts[actor]) for actor in ACTORS}
        events = {actor: turn.result() for actor, turn in turns.items()}
        if not trial.wait_for_calls(HOLD_SECONDS + 2 * STATEMENT_SECONDS):
            raise RuntimeError("A tool call was still running after both turns ended")
        failed = {actor: event.get("result") for actor, event in events.items()
                  if event.get("subtype") != "success" or event.get("is_error")}
        if failed:
            raise RuntimeError(f"Model turn failed: {failed}")
        for agent in agents.values():
            check_init(agent, args)
        final = trial.audit()
        with trial.lock:
            summary.update(final_audit=final, outcome=outcome(final), status="completed",
                           success=succeeded(summary, final))
    except Exception as error:
        with trial.lock:
            summary.update(status="error", success=False, error={"type": type(error).__name__, "detail": str(error)})
        if trial.auditor:
            try:
                final = trial.audit()
                with trial.lock:
                    summary.update(final_audit=final, outcome=outcome(final))
            except Exception as audit_error:
                with trial.lock:
                    summary["final_audit_error"] = str(audit_error)
    finally:
        for agent in agents.values():
            agent.close()
        if server:
            server.shutdown()
            server.server_close()
        for session in (trial.monitor, trial.auditor):
            if session:
                session.close()
        with trial.lock:
            if trial.audit_log:
                trial.audit_log.close()
            if agents:
                metrics = summary["metrics"] = trace_metrics(raw, list(agents), args.model)
                summary["model_protocol_match"] = (not metrics["model_reroutes"] and not metrics["init_mismatches"]
                                                   and len(metrics["model_by_agent"]) == len(ACTORS))
                if (not summary["model_protocol_match"] or metrics["unanswered_tool_request_ids"]
                        or metrics["mcp_calls_without_stream_tool_use"] or metrics["tool_calls"] != len(summary["calls"])):
                    summary["success"] = False
                for actor in agents:
                    summary["agents"][actor].update(calls=metrics["tool_calls_by_agent"].get(actor),
                                                    tokens=metrics["tokens_by_agent"].get(actor))
            summary["collision"] = collision_summary(group, summary["calls"])
            summary["refusals"] = refusals(summary["calls"])
            summary["wall_seconds"] = round(time.monotonic() - started, 3)
            save(folder / "result.json", summary)
        print(f"run {number:02d} ({group}): status={summary['status']} success={summary['success']} "
              f"outcome={summary.get('outcome')} later={summary['collision'].get('later')}", flush=True)
    return summary


def register(args):
    version = subprocess.run(args.launcher_version, capture_output=True, text=True).stdout.strip()
    account = {}
    if args.claude.suffix != ".py":
        status = subprocess.run([str(args.claude), "auth", "status"], capture_output=True, text=True,
                                env=child_environment(args.config_dir, args.child_env)).stdout
        try:
            account = {k: v for k, v in json.loads(status).items() if k in ("loggedIn", "authMethod", "apiProvider", "subscriptionType")}
        except ValueError:
            account = {"auth_status_unreadable": status[-500:]}
    env = child_environment(args.config_dir, args.child_env)
    protocol = {
        "registered_at_utc": datetime.now(timezone.utc).isoformat(), "client_date": args.client_date,
        "pilot": args.pilot, "runs_per_group": args.runs_per_group,
        "order": "odd runs P, even runs F, one round at a time; both agents of a round run at once",
        "model": args.model, "reasoning_effort": args.effort, "claude_code_version": version,
        "claude_executable": str(args.claude), "claude_executable_sha256": sha256(args.claude), "account": account,
        "turn_timeout_seconds": args.turn_timeout, "max_tool_calls_per_agent": CALL_BUDGET,
        "instructions": INSTRUCTIONS,
        "prompts": {group: {actor: prompt_for(group, actor) for actor in ACTORS} for group in GROUPS},
        "task": {"products": PRODUCTS, "start_stock": START, "sale": SALE, "bundle_floor": FLOOR, "rows": 4000,
                 "correct": f"exactly one of the two products at {SOLD}, the other at {START}",
                 "stale": f"both products at {SOLD}"},
        "hold": {"first_write_call": WRITES, "hold_seconds": HOLD_SECONDS, "poll_seconds": POLL_SECONDS,
                 "commit_seconds": COMMIT_SECONDS, "rule": " ".join(Collision.__doc__.split())},
        "lock_timeout": LOCK_TIMEOUT, "statement_seconds": STATEMENT_SECONDS, "output_bytes": OUTPUT_BYTES,
        "sql_bytes": SQL_BYTES,
        "mcp_tool_timeout_ms": TOOL_TIMEOUT * 1000,
        "mcp_tool_timeout_setting": "the stdio server's \"timeout\" field in each mcp-config (Claude Code 2.1.308 "
                                    "reads it before MCP_TOOL_TIMEOUT, default 1e8 ms, and caps the stdio idle limit, "
                                    "default 1,800,000 ms, by it); agentdb_mcp_server.py --timeout the same",
        "tools": TOOLS, "tool_name_seen_by_model": args.tool_name,
        "claude_command": claude_command(args, Path("<run>/<actor>.mcp-config.json"), INSTRUCTIONS),
        "child_environment": {"CLAUDE_CONFIG_DIR": str(args.config_dir), **{
            k: v for k, v in env.items() if k.startswith(("CLAUDE", "DISABLE", "MCP")) or k in args.child_env}},
        "agent_working_directory": str(args.workdir) + "/run-NN (empty, outside any git repository, no CLAUDE.md or AGENTS.md)",
        "databases": {"P": "a new agentdb_test_plain_<10 hex> with plain_setup_sql",
                      "F": "agent_lab.py init (agentdb_test_gpt_<10 hex>)"},
        "plain_setup_sql": PLAIN_SETUP,
        "sources_sha256": {p: sha256(args.project / p) for p in SOURCES},
        "success_definition": "Both agents' turns complete; exactly one sale landed (one of products 101 and 202 at "
                              "85, the other at 100); the other 3,998 rows, the two rows' other columns and app.orders "
                              "unchanged; no other client session in a transaction; no audit error; every API response "
                              "from the registered model; every MCP request answered.",
        "primary_metric": "Per round, whether both sales landed: products 101 and 202 both at 85 at the end (stale).",
        "outcomes": "correct: exactly one sale landed and nothing else changed; stale: both landed; other: no "
                    "sale, or any other change.",
        "later_guard": {"P": " ".join(p_guard.__doc__.split()),
                        "F": "whether the reads of the later writer's first merge named the other product"},
        "counting": "All registered rounds kept; no replacement of failed rounds; no manual rescue. A round "
                    "stopped by a usage limit is kept and reported as such. Tokens as in the 2026-10-20 Claude run.",
        "real_cloud_verified": False}
    save(args.output / "protocol.json", protocol)
    return protocol


def aggregate(results, group):
    rows = [r for r in results if r["group"] == group]
    successes = sum(r["success"] for r in rows)
    return {"rounds": len(rows), "successes": successes,
            "wilson_95_interval": wilson(successes, len(rows)) if rows else None,
            "outcomes": {o: sum(r.get("outcome") == o for r in rows) for o in ("correct", "stale", "other")},
            "stale_runs": [r["run"] for r in rows if r.get("outcome") == "stale"],
            "both_wrote": sum(bool(r["collision"].get("both_wrote")) for r in rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", type=Path, default=ROOT)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--claude", type=Path, default=shutil.which("claude"))
    parser.add_argument("--config-dir", type=Path, required=True, help="Dedicated CLAUDE_CONFIG_DIR holding only the login")
    parser.add_argument("--workdir", type=Path, required=True, help="Parent of the agents' empty working directories")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="claude-opus-5-5")
    parser.add_argument("--effort", default="xhigh")
    parser.add_argument("--tool-name", default="mcp__agentdb__agentdb", help="Name Claude Code gives the MCP tool")
    parser.add_argument("--child-env", type=json.loads, default={}, help="Extra environment for claude, as JSON")
    parser.add_argument("--client-date", default=datetime.now().date().isoformat())
    parser.add_argument("--runs-per-group", type=int, default=10)
    parser.add_argument("--pilot", action="store_true", help="Not part of the sample; allows fewer rounds")
    parser.add_argument("--turn-timeout", type=int, default=900)
    args = parser.parse_args()
    if args.runs_per_group < 10 and not args.pilot:
        parser.error("Register at least ten rounds per group, or mark the run --pilot")
    if args.claude is None:
        parser.error("Claude Code not found; pass --claude")
    args.project, args.output, args.workdir = args.project.resolve(), args.output.resolve(), args.workdir.resolve()
    args.claude = args.claude.resolve()
    args.config = (args.config or args.project / "connection.local.json").resolve()
    args.launcher_version = ([args.python, str(args.claude)] if args.claude.suffix == ".py" else [str(args.claude)]) + ["--version"]
    args.output.mkdir(parents=True, exist_ok=False)
    args.workdir.mkdir(parents=True, exist_ok=False)
    protocol = register(args)
    print(f"registered {args.runs_per_group} rounds per group{' (pilot)' if args.pilot else ''}: "
          f"{protocol['model']} / {protocol['reasoning_effort']} / {protocol['claude_code_version']}", flush=True)
    plan = [(n, GROUPS[(n - 1) % 2]) for n in range(1, 2 * args.runs_per_group + 1)]
    results = [run_trial(args, n, group) for n, group in plan]
    output = {"protocol": "protocol.json", "pilot": args.pilot, "planned_rounds": len(plan), "recorded_rounds": len(results),
              "by_group": {group: aggregate(results, group) for group in GROUPS},
              "rows": [{"run": r["run"], "group": r["group"], "status": r["status"], "success": r["success"],
                        "outcome": r.get("outcome"), "collision": r["collision"], "refusals": r["refusals"],
                        "agents": {a: {k: v for k, v in r["agents"].get(a, {}).items() if k in ("calls", "tokens", "turn_seconds")}
                                   for a in ACTORS},
                        "wall_seconds": r["wall_seconds"], "error": r.get("error")} for r in results],
              "real_cloud_verified": False}
    save(args.output / "summary.json", output)
    print(json.dumps(output["by_group"], indent=2), flush=True)


if __name__ == "__main__":
    main()
