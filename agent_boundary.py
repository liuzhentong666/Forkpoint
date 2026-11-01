"""Give each agent its own login, and keep the boundary around those logins.

  run     issue a login, run a command with it in PGUSER, PGPASSWORD and the like,
          keep the keeper's rounds going meanwhile, and retire the login when the command ends
  status  frozen or not, what the self-check finds, each login's budget
  keep    the keeper's rounds alone, for agents started some other way: self-check (a problem
          freezes writes), end stuck sessions
  undo    stop a login and revert its commits on main
  report  what a login really landed on main, from history
  freeze  stop every agent's writes now; unfreeze is the owner's
  backup  dump the database outside itself, and keep the dump only if it restores

The engine side is sql/011_agent_boundary.sql; docs/AGENT_BOUNDARY.md has the design.
This program is the broker and the keeper: it runs as the restricted roles
agentdb_broker and agentdb_keeper, and has to run where agents cannot reach it or its
connection config. The password is random, never leaves this process except in the
command's environment, and reaches the server only as a SCRAM verifier, so it cannot
appear in a server log.
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import uuid

from agent_state.engine import Engine
from agent_state.maintenance import BoundaryKeeper
from agent_state.runtime import Runtime
from agent_state.storage import Conflict, MemoryStore
from db import FLAGS, Connection, add_connection_arguments, database_name

WORKER, BROKER, KEEPER = "agentdb_worker", "agentdb_broker", "agentdb_keeper"
SIZE = r"[1-9][0-9]{0,8}(?:kB|MB|GB)"
LOGIN = r"[a-z][a-z0-9_]{0,62}"
# What a command needs to start at all. The rest of the operator's environment can hold credentials, and agents
# have caused real damage with credentials they inherited; --pass-env adds a name on purpose, such as a model API key.
PASSED_ENVIRONMENT = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR", "HOME",
                      "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "LANG", "LC_ALL", "TERM",
                      "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")


def scram_verifier(password):
    """The form PostgreSQL stores (RFC 5802): with it the server never sees the password itself."""
    salt, iterations = secrets.token_bytes(16), 4096
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    stored = hashlib.sha256(hmac.digest(salted, b"Client Key", "sha256")).digest()
    server = hmac.digest(salted, b"Server Key", "sha256")
    encode = lambda value: base64.b64encode(value).decode("ascii")
    return f"SCRAM-SHA-256${iterations}:{encode(salt)}${encode(stored)}:{encode(server)}"


def issue_login(broker, database, rows, size, hours, connections=4, temp_file_limit="100MB", prefix="agent",
                max_table_share=0.1):
    """(login, password, valid_until) of a new login on a connection running as agentdb_broker.

    The budget is fixed here, for the login's whole life; rows and size of 0 make a login that can only read,
    which no prompt can talk its way out of. A max_table_share of 1 lets it rewrite whole tables. The name is
    random, and vc.agent_budget keeps every name it has seen, so a login is never issued twice. Settings the
    agent can change are only defaults for a well-behaved one, and the keeper enforces the real limits. It
    cannot raise temp_file_limit or turn off log_statement, which leaves every statement it runs in the server
    log, including those it was refused: a refusal rolls back, so history cannot show them.
    """
    database_name(database)
    if not re.fullmatch(SIZE, temp_file_limit):
        raise ValueError("temp_file_limit must look like 100MB")
    for value, label, floor in ((rows, "rows", 0), (size, "bytes", 0), (hours, "hours", 1e-9), (connections, "connections", 1)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < floor:
            raise ValueError(f"{label} must be at least {floor}")
    if isinstance(max_table_share, bool) or not isinstance(max_table_share, (int, float)) or not 0 < max_table_share <= 1:
        raise ValueError("max_table_share must be above 0 and at most 1")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", prefix):
        raise ValueError("A login prefix is 1-41 lowercase letters, digits or underscores, starting with a letter")
    name, password = f"{prefix}_{secrets.token_hex(6)}", secrets.token_urlsafe(32)
    until = broker.sql(f"SELECT (clock_timestamp()+make_interval(secs=>{float(hours) * 3600}))::text;", database)
    broker.sql(f"""BEGIN;
CREATE ROLE {name} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT
  CONNECTION LIMIT {int(connections)} VALID UNTIL '{until}' PASSWORD '{scram_verifier(password)}';
GRANT {WORKER} TO {name} WITH INHERIT TRUE, SET FALSE;
GRANT CONNECT ON DATABASE "{database}" TO {name};
ALTER ROLE {name} SET temp_file_limit='{temp_file_limit}';
ALTER ROLE {name} SET idle_in_transaction_session_timeout='60s';
ALTER ROLE {name} SET log_statement='all';
SELECT vc.issue_budget('{name}',{int(rows)},{int(size)},{float(max_table_share)});
COMMIT;""", database)
    return name, password, until


def retire_login(broker, keeper, database, login):
    """End the login's sessions, spend its budget and drop it. History keeps its name as the author.

    Sessions end first: an open transaction of its could hold its budget row, and vc.stop_agent would wait for it.
    """
    database_name(database)
    if not re.fullmatch(LOGIN, login):
        raise ValueError("Not a login name")
    broker.sql(f"ALTER ROLE {login} NOLOGIN;", database)
    keeper.sql(f"SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE usename='{login}';", database)
    broker.sql(f"""BEGIN;
SELECT vc.stop_agent('{login}');
REVOKE CONNECT ON DATABASE "{database}" FROM {login};
REVOKE {WORKER} FROM {login};
DROP ROLE {login};
COMMIT;""", database)


def connect(args, role):
    # A copy, because Connection reads its role from the arguments.
    return Connection(argparse.Namespace(**dict(vars(args), role=role)))


def run(args):
    if not args.command or args.command[0] != "--" or len(args.command) < 2:
        raise ValueError("Give the agent's command after --")
    broker, keeper = connect(args, BROKER), connect(args, KEEPER)
    database = broker.settings["database"]
    login, password, until = issue_login(broker, database, args.rows, args.bytes, args.hours, args.connections,
                                         args.temp_file_limit, args.prefix, args.max_table_share)
    host = args.agent_host or broker.settings["host"]
    # A Unix socket here means a peer rule, which cannot serve a password login.
    environment = {name: os.environ[name] for name in (*PASSED_ENVIRONMENT, *args.pass_env) if name in os.environ}
    environment.update(PGUSER=login, PGPASSWORD=password, PGDATABASE=database,
                       PGHOST="127.0.0.1" if host.startswith("/") else host, PGPORT=str(broker.settings["port"]))
    if broker.settings.get("sslmode"):
        environment.update(PGSSLMODE=broker.settings["sslmode"], PGSSLROOTCERT=broker.settings["sslrootcert"])
    print(json.dumps(dict(login=login, database=database, valid_until=until, main_rows=args.rows, bytes=args.bytes,
                          max_table_share=args.max_table_share)), file=sys.stderr, flush=True)
    # Nobody has to remember the keeper: its rounds run as long as the agent does.
    stop = threading.Event()
    rounds = threading.Thread(target=keep, args=(args, stop), daemon=True)
    rounds.start()
    try:
        return subprocess.run(args.command[1:], env=environment).returncode
    finally:
        stop.set()
        rounds.join(timeout=30)
        retire_login(broker, keeper, database, login)


def keep(args, stop=None):
    """The keeper's rounds until stop is set, or one round with --once; several keepers at once do no harm."""
    stop = stop or threading.Event()
    engine = Engine(MemoryStore())
    runtime = Runtime(engine)
    limits = {name: value for name, value in (("max_idle_in_transaction_seconds", args.idle_seconds),
                                              ("max_transaction_seconds", args.transaction_seconds)) if value is not None}
    policy = engine.objects.put_bytes(json.dumps(dict(agent_boundary=limits)).encode("utf-8")) if limits else None
    session = None
    try:
        while True:
            failed = False
            try:
                session = session or connect(args, KEEPER).session()
                result = BoundaryKeeper(runtime, session).check("keeper", policy_ref=policy)
                if result["published"]:
                    print(json.dumps(result), file=sys.stderr, flush=True)
            except (Conflict, ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
                # The next round tries again. Keeping quiet here would look like a boundary that stands.
                failed = True
                print(f"keeper round failed: {error}", file=sys.stderr, flush=True)
            if args.once:
                return 1 if failed else 0
            if stop.wait(args.interval):
                return 0
    finally:
        if session:
            session.close()


def undo(args):
    keeper = BoundaryKeeper(Runtime(Engine(MemoryStore())), connect(args, KEEPER))
    result = keeper.undo_login("keeper", args.login)
    print(json.dumps(result, indent=2))
    return 0 if not result["refused"] else 1


def backup(args):
    """A copy outside the database, because history dies with the database: agents have destroyed backups
    kept beside the data as well. Restores it into a scratch database and checks that before calling it a backup,
    so a .dump file here has been restored once. Run as the owner; agents must not be able to reach the folder."""
    owner = connect(args, None)
    database = owner.settings["database"]
    settings = owner.settings
    def tool(program, *argv):
        return [*owner.prefix[:-1], re.sub(r"psql(\.exe)?$", program + r"\1", owner.prefix[-1]), "-h", settings["host"],
                "-p", str(settings["port"]), "-U", settings["user"], *argv]
    folder = Path(args.to)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = owner.sql("SELECT to_char(clock_timestamp(),'YYYYMMDD-HH24MISS');", database)
    partial, final = folder / f"{database}-{stamp}.partial", folder / f"{database}-{stamp}.dump"
    with partial.open("xb") as dump:
        done = subprocess.run(tool("pg_dump", "-Fc", "-d", database), stdout=dump, stderr=subprocess.PIPE, env=owner.env,
                              timeout=3600, creationflags=FLAGS)
    if done.returncode:
        raise RuntimeError("pg_dump failed: " + done.stderr.decode("utf-8", "replace")[-1000:])
    scratch = "agentdb_test_restore_" + secrets.token_hex(5)
    owner.create_database(scratch)
    try:
        with partial.open("rb") as dump:
            done = subprocess.run(tool("pg_restore", "--exit-on-error", "-d", scratch), stdin=dump, stderr=subprocess.PIPE,
                                  stdout=subprocess.PIPE, env=owner.env, timeout=3600, creationflags=FLAGS)
        if done.returncode:
            raise RuntimeError("pg_restore failed: " + done.stderr.decode("utf-8", "replace")[-1000:])
        head = owner.sql("SELECT vc.head();", scratch)
        # At a revision the database really has, with each live table equal to what history says it holds.
        if owner.sql(f"SELECT EXISTS(SELECT 1 FROM vc.changeset WHERE id='{uuid.UUID(head)}');", database) != "t":
            raise RuntimeError(f"The restored head {head} is not a revision of {database}")
        tables = {}
        for line in owner.sql("SELECT string_agg(relation::text||' '||id::text,E'\\n') FROM vc.tracked_table;", scratch).splitlines():
            relation, table_id = line.split()
            rows, differing = owner.sql(f"""SET TimeZone='UTC'; SET IntervalStyle='postgres'; SET extra_float_digits=1; SET bytea_output='hex';
              SELECT (SELECT count(*) FROM {relation})||' '||(
                (SELECT count(*) FROM (SELECT r.doc FROM vc.rows_at(vc.head(),'{table_id}') r EXCEPT SELECT to_jsonb(p) FROM {relation} p) a)+
                (SELECT count(*) FROM (SELECT to_jsonb(p) FROM {relation} p EXCEPT SELECT r.doc FROM vc.rows_at(vc.head(),'{table_id}') r) b));""", scratch).split()
            if differing != "0":
                raise RuntimeError(f"Restored {relation} differs from its history in {differing} rows")
            tables[relation] = int(rows)
    finally:
        owner.sql(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE);', settings["maintenance_database"])
    partial.rename(final)
    removed = sorted(folder.glob(f"{database}-*.dump"))[:-args.keep]
    for old in removed:
        old.unlink()
    print(json.dumps(dict(file=str(final), bytes=final.stat().st_size, head=head, tables=tables, restored_and_checked=True,
                          removed=[old.name for old in removed]), indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    actions = parser.add_subparsers(dest="action", required=True)
    start = actions.add_parser("run", help="run a command under a new agent login")
    start.add_argument("--rows", type=int, default=1000, help="rows it may land on main, for its whole life")
    start.add_argument("--bytes", type=int, default=10_000_000, help="bytes it may store, for its whole life")
    start.add_argument("--hours", type=float, default=8, help="the login expires after this long")
    start.add_argument("--connections", type=int, default=4)
    start.add_argument("--temp-file-limit", default="100MB")
    start.add_argument("--prefix", default="agent", help="the start of the login's name")
    start.add_argument("--max-table-share", type=float, default=0.1,
                       help="beyond its first 20 rows, the share of a table it may change; 1 means no limit")
    start.add_argument("--pass-env", action="append", default=[], help="pass this variable on to the command; repeat as needed")
    start.add_argument("--agent-host", help="the server's address as the command sees it, e.g. host.docker.internal")
    start.add_argument("command", nargs=argparse.REMAINDER, help="-- the agent's command")
    start.set_defaults(once=False)
    actions.add_parser("status", help="report the boundary; changes nothing")
    loop = actions.add_parser("keep", help="run the keeper's rounds alone, for agents started some other way")
    loop.add_argument("--once", action="store_true", help="one round, for a scheduler")
    for action in (start, loop):
        action.add_argument("--interval", type=float, default=5, help="seconds between the keeper's rounds")
        action.add_argument("--idle-seconds", type=int, help="end a session idle in a transaction this long; default 60")
        action.add_argument("--transaction-seconds", type=int, help="end a transaction open this long; default 300")
    back = actions.add_parser("undo", help="stop a login and revert its commits on main")
    back.add_argument("login")
    told = actions.add_parser("report", help="what a login really landed on main, from history")
    told.add_argument("login")
    stop = actions.add_parser("freeze", help="stop every agent's writes now; reads go on")
    stop.add_argument("reason")
    actions.add_parser("unfreeze", help="open writes again; only the database owner can")
    copy = actions.add_parser("backup", help="dump the database, restore it into a scratch database and check it")
    copy.add_argument("--to", required=True, help="a folder agents cannot reach")
    copy.add_argument("--keep", type=int, default=7, help="how many checked dumps of this database to keep")
    for action in actions.choices.values():
        add_connection_arguments(action)
    args = parser.parse_args()
    try:
        if args.action == "status":
            connection = connect(args, KEEPER)
            print(json.dumps(json.loads(connection.sql("SELECT vc.boundary_status();")), indent=2))
            return
        if args.action == "report":
            if not re.fullmatch(LOGIN, args.login):
                raise ValueError("Not a login name")
            print(json.dumps(json.loads(connect(args, KEEPER).sql(f"SELECT vc.agent_report('{args.login}');")), indent=2))
            return
        if args.action == "freeze":
            reason = args.reason.replace("'", "''")
            connect(args, KEEPER).sql(f"SELECT vc.freeze('{reason}');")
            print(json.dumps(dict(frozen=args.reason)))
            return
        if args.action == "unfreeze":
            connect(args, None).sql("SELECT vc.unfreeze();")  # Refused unless this connection is the owner's.
            print(json.dumps(dict(frozen=None)))
            return
        handler = dict(run=run, keep=keep, undo=undo, backup=backup)[args.action]
        sys.exit(handler(args))
    except KeyboardInterrupt:
        sys.exit(130)
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
