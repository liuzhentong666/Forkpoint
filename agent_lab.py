"""Small real-agent exercise against a dedicated local PostgreSQL test database.

All worker commands use agentdb_client. This wrapper narrows the exercise surface;
it is not an OS sandbox or a substitute for an independently authenticated login.
"""
import argparse
import ast
import copy
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import sys
import uuid

from db import Connection, add_connection_arguments, sqlstate

# A merge waits at most this long for main's branch lock, then fails with
# lock_timeout instead of queueing behind a vanished client's open transaction
# (docs/STAGING_S4_S5.md section 4).
MERGE_LOCK_TIMEOUT = "10s"
# Merge failures by SQLSTATE. merge_conflict and read_conflict are semantic: a row
# the proposal wrote, or one its decision read (merge --reads), changed on main, so
# the decision is made again from current main. After stale_head or lock_timeout
# main is unchanged and the same proposal may be tried again. invalid_read_set is a
# read-set key the engine refused: other columns, a null, or a value the column
# cannot take (it rewrites the rest, such as "101" to 101). not_found is a revision
# vc.merge_validated does not know (vc.merge still reports that as P0001).
MERGE_ERRORS = {"40001": "stale_head", "P0001": "merge_conflict", "55P03": "lock_timeout",
                "RS001": "read_conflict", "22023": "invalid_read_set", "P0002": "not_found"}
# Fork and propose failures reported the same way; in each, nothing changed. vc.fork
# inserts one vc.branch row: 23505 (branch_pkey) is a branch of that name that already
# exists, perhaps made by an earlier process (the reply carries its head, which a propose
# on it needs as --expected), and 23503 a base revision the engine does not know.
# propose's 40001 is a branch head other than --expected, from vc.begin_change (or
# vc.finish_change, if the branch moved meanwhile); it is not merge's stale_head, whose
# head is main's. P0002 is a branch that does not exist (sql/010_unknown_branch_code.sql).
# In all three actions any other failure is database_error, with its SQLSTATE and report.
FORK_ERRORS = {"23505": "branch_exists", "23503": "not_found"}
PROPOSE_ERRORS = {"40001": "stale_branch_head", "P0002": "not_found"}
# merge --reads names rows by table; every lab database registers these two
# (sql/002_demo_app.sql), so vc.table_id never sees an unregistered name.
READ_TABLES = ("app.products", "app.orders")
READS_LIMIT = 16384


def connect(args, database, role=None):
    # Same as review_local.connect; importing review_local (state engine, test
    # suite) would add hundreds of milliseconds to every tool call.
    options = copy.copy(args)
    options.database, options.role = database, role
    return Connection(options)


def literal(value):
    return "'" + value.replace("'", "''") + "'"


def revision(value):
    return str(uuid.UUID(value))


def branch(value):
    if not value or not re.fullmatch(r"gpt-[a-z0-9-]{1,50}", value):
        raise ValueError("Use a gpt- branch name")
    return value


class Repeated(ValueError):
    pass


def read_set(value):
    """The --reads JSON, checked for shape; the engine checks each key against the primary key."""
    shape = ("--reads must be a JSON object mapping app.products or app.orders to arrays of "
             'primary keys, such as {"app.products": [{"id": 101}]}')
    if len(value.encode()) > READS_LIMIT:
        raise ValueError(f"--reads exceeds {READS_LIMIT} bytes")

    def constant(name):
        raise ValueError(name)

    def unique(pairs):
        # json.loads would keep only the last of repeated names, silently dropping rows.
        names = [name for name, _ in pairs]
        if len(set(names)) < len(names):
            raise Repeated("--reads repeats a table or column name; give each once")
        return dict(pairs)
    try:
        reads = json.loads(value, parse_constant=constant, object_pairs_hook=unique)
    except Repeated:
        raise
    except (ValueError, RecursionError):
        raise ValueError(shape) from None
    if not isinstance(reads, dict) or not all(
            table in READ_TABLES and isinstance(keys, list) and all(isinstance(key, dict) and key for key in keys)
            for table, keys in reads.items()):
        raise ValueError(shape)
    return reads


def build_parser(parser_class=argparse.ArgumentParser, **options):
    parser = parser_class(description=__doc__, **options)
    add_connection_arguments(parser)
    parser.add_argument("action", choices=["init", "info", "read-code", "fork", "propose", "diff", "merge", "verify"])
    parser.add_argument("--branch")
    parser.add_argument("--base")
    parser.add_argument("--revision")
    parser.add_argument("--expected")
    parser.add_argument("--actor", choices=["gpt-a", "gpt-b"])
    parser.add_argument("--field", choices=["price", "stock"])
    parser.add_argument("--value")
    parser.add_argument("--code", help="Python policy code, at most 4096 UTF-8 bytes; stored, never executed")
    parser.add_argument("--path", choices=["experiments/pricing.py", "experiments/inventory.py"],
                        help="read-code: inspect a policy at an explicit fixed revision before merging")
    parser.add_argument("--reads", help="merge, instead of --expected: the rows the decision read, as JSON, "
                        'e.g. {"app.products": [{"id": 101}]}. Lands even if main moved, unless one of '
                        "them changed since the proposal's base (read_conflict); rows it wrote are checked too")
    return parser


def check_database(parser, args):
    if not args.database or not re.fullmatch(r"agentdb_test_gpt_[0-9a-f]{10}", args.database):
        parser.error("Explicit dedicated agentdb_test_gpt_<10 hex> database is required")


def validate(parser, args):
    """Argument checks a worker action makes before its first SQL statement."""
    for option in ("revision", "base", "expected"):
        value = getattr(args, option, None)
        if value is not None:
            try:
                uuid.UUID(value)
            except ValueError:
                parser.error(f"--{option} must be a revision UUID")
    if args.reads is not None:
        if args.action != "merge":
            parser.error("--reads is only for merge")
        if args.expected is not None:
            parser.error("merge takes --expected or --reads, not both")
        try:
            read_set(args.reads)
        except ValueError as error:
            parser.error(str(error))
    needs = {"fork": ("branch", "base"), "propose": ("branch", "expected"), "diff": ("base", "revision"),
             "merge": ("revision", "expected") if args.reads is None else ("revision",)}.get(args.action, ())
    missing = [f"--{name}" for name in needs if getattr(args, name) is None]
    if missing:
        parser.error(f"{args.action} requires {' and '.join(missing)}")
    if "branch" in needs:
        try:
            branch(args.branch)
        except ValueError as error:
            parser.error(str(error))
    if args.action == "read-code":
        if not args.path or not args.revision:
            parser.error("read-code requires path and revision")
    elif args.action == "propose":
        if not args.actor or not args.field or args.value is None or args.code is None:
            parser.error("propose requires actor, field, value and code")
        if len(args.code.encode()) > 4096:
            parser.error("Policy code exceeds 4096 bytes")
        try:
            ast.parse(args.code)
        except (SyntaxError, ValueError) as error:
            parser.error(f"Policy code is not valid Python: {getattr(error, 'msg', error)}")
        try:
            value = Decimal(args.value)
        except InvalidOperation:
            value = Decimal("NaN")
        if not value.is_finite() or not 0 <= value <= 100000:
            parser.error("Finite nonnegative value up to 100000 required")
        if args.field == "stock" and value != value.to_integral_value():
            parser.error("Stock must be an integer")
    elif args.action == "merge":
        if not args.actor:
            parser.error("merge requires actor")


def main():
    parser = build_parser()
    args = parser.parse_args()
    if args.action == "init":
        database = "agentdb_test_gpt_" + uuid.uuid4().hex[:10]
        owner = connect(args, database)
        owner.check()
        owner.create_database(database)
        owner.install()
        from review_local import seed
        seed(owner)
        result = dict(database=database, base=owner.sql("SELECT vc.head();"), product_id=101,
                      goal="price +10%, stock -5, preserve both code policies and both fields", real_cloud_verified=False)
        print(json.dumps(result, indent=2))
        return
    check_database(parser, args)
    worker = connect(args, args.database, "agentdb_client")
    validate(parser, args)
    gateway = os.environ.get("AGENTDB_LAB_GATEWAY")
    if gateway and not args.password:
        # Imported only when configured: without a gateway nothing below changes.
        import agent_lab_gateway
        outcome = agent_lab_gateway.run(Path(gateway), sys.argv[1:], worker)
        if outcome is not None:
            stdout, stderr, code = outcome
            sys.stdout.write(stdout)
            sys.stderr.write(stderr)
            if code:
                raise SystemExit(code)
            return
    stdout, code = perform(args, worker)
    sys.stdout.write(stdout)
    if code:
        raise SystemExit(code)


def perform(args, worker):
    """Run one validated worker action; return its stdout text and exit code."""
    if args.action == "info":
        head = worker.sql("SELECT vc.head();")
        fixed = revision(args.revision) if args.revision else head
        product = json.loads(worker.sql(f"SELECT doc FROM vc.rows_at('{fixed}',vc.table_id('app.products')) WHERE pk='{{\"id\":101}}';"))
        result = dict(database=args.database, effective_role=worker.sql("SELECT current_user;"),
                      current_main=head, read_revision=fixed, product=product,
                      conflict_model="whole row; different columns on the same row may conflict")
    elif args.action == "read-code":
        fixed = revision(args.revision)
        result = json.loads(worker.sql(f"""
          SELECT json_build_object('revision','{fixed}','path',{literal(args.path)},
            'content',convert_from(vc.read_file('{fixed}',{literal(args.path)}),'UTF8'));
        """))
    elif args.action == "fork":
        name = literal(branch(args.branch))

        def existing_head(response, state):
            # branch_exists also gives the head a propose on that branch needs as --expected. The
            # refusal is already certain; if this second read fails (a timeout, a lost session),
            # it goes out without head rather than as a traceback.
            if state != "23505":
                return {}
            try:
                return dict(head=worker.sql(f"SELECT vc.head({name});"))
            except Exception:
                return {}
        created, refusal = checked(worker, f"SELECT vc.fork({name},'{revision(args.base)}');", FORK_ERRORS,
                                   existing_head)
        if refusal:
            return refusal, 2
        result = dict(branch=args.branch, head=created)
    elif args.action == "propose":
        value = Decimal(args.value)
        path = "experiments/" + ("pricing.py" if args.field == "price" else "inventory.py")
        content = args.code.encode().hex()
        created, refusal = checked(worker, f"""
          BEGIN;
          SELECT vc.begin_change({literal(branch(args.branch))},'{revision(args.expected)}',{literal(args.actor)},'real GPT proposal') AS draft \gset
          UPDATE agent.products SET {args.field}={value} WHERE id=101;
          SELECT vc.write_file('{path}',decode('{content}','hex')) AS blob \gset
          SELECT vc.finish_change() AS revision \gset
          COMMIT;
          SELECT :'revision';
        """, PROPOSE_ERRORS)
        if refusal:
            return refusal, 2
        result = dict(branch=args.branch, revision=created, code_path=path)
    elif args.action == "diff":
        base, proposed = revision(args.base), revision(args.revision)
        result = json.loads(worker.sql(f"""
          SELECT json_build_object(
            'rows',(SELECT coalesce(json_agg(d),'[]') FROM vc.diff_rows('{base}','{proposed}',vc.table_id('app.products')) d),
            'files',(SELECT coalesce(json_agg(d),'[]') FROM vc.diff_files('{base}','{proposed}') d));
        """))
    elif args.action == "merge":
        names = ""
        if args.reads is None:
            merge = f"vc.merge('main','{revision(args.revision)}','{revision(args.expected)}',{literal(args.actor)})"
        else:
            # The text goes as given, so PostgreSQL reads every number exactly (Python
            # would turn 1e400 into infinity); hex keeps it clear of psql's own syntax,
            # as propose does with policy code. Table names become the table ids
            # vc.merge_validated takes, and the line printed first maps them back if
            # the merge is refused, without a second round trip.
            read_set(args.reads)
            reads = args.reads.encode().hex()
            names = ("SELECT 'tables',json_build_object(" + ",".join(
                f"vc.table_id('{table}')::text,'{table}'" for table in READ_TABLES) + ");\n          ")
            merge = (f"vc.merge_validated('main','{revision(args.revision)}',"
                     f"(SELECT coalesce(jsonb_object_agg(vc.table_id(r.key::regclass)::text,r.value),'{{}}') "
                     f"FROM jsonb_each(convert_from(decode('{reads}','hex'),'UTF8')::jsonb) r),{literal(args.actor)})")
        landed, refusal = checked(worker, f"""
          {names}BEGIN;
          SET LOCAL lock_timeout = '{MERGE_LOCK_TIMEOUT}';
          SELECT {merge} AS landed \gset
          COMMIT;
          SELECT :'landed';
        """, MERGE_ERRORS, lambda response, state: dict(changed=changed_rows(response.stdout, response.stderr))
                                if state == "RS001" else {})
        if refusal:
            return refusal, 2
        result = dict(merged=True, main=landed.splitlines()[-1])
    else:
        head = worker.sql("SELECT vc.head();")
        product = json.loads(worker.sql(f"SELECT doc FROM vc.rows_at('{head}',vc.table_id('app.products')) WHERE pk='{{\"id\":101}}';"))
        codes = {}
        for name, expected in (("pricing", 11), ("inventory", 95)):
            code = worker.sql(f"SELECT convert_from(vc.read_file('{head}','experiments/{name}.py'),'UTF8');")
            constant = "PRICE" if name == "pricing" else "STOCK"
            values = [ast.literal_eval(node.value) for node in ast.parse(code).body
                      if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == constant for t in node.targets)]
            assert values == [expected], (name, code)
            codes[name] = code
        assert Decimal(str(product["price"])) == 11 and product["stock"] == 95, product
        result = dict(database=args.database, main=head, product=product, policies=codes,
                      effective_role=worker.sql("SELECT current_user;"), outcome_verified=True,
                      real_cloud_verified=False)
    return json.dumps(result, ensure_ascii=False, indent=2) + "\n", 0


def checked(worker, statement, errors, extra=None):
    """(stdout, None) for a statement that ran, as worker.sql returns it, or (None, JSON refusal).

    The refusal names the error errors gives the SQLSTATE, database_error for any other, the
    SQLSTATE and psql's report; extra(response, state) may add fields.
    """
    response = worker.execute(statement)
    if not response.returncode:
        return response.stdout.strip(), None
    state = sqlstate(response.stderr) or "unknown"
    fields = dict(error=errors.get(state, "database_error"), sqlstate=state,
                  **(extra(response, state) if extra else {}))
    return None, json.dumps(dict(fields, detail=response.stderr.strip())) + "\n"


def changed_rows(stdout, stderr):
    """RS001's DETAIL rows with table names for table ids; None if DETAIL is unreadable."""
    match = re.search(r"^DETAIL:\s+(\[.*\])\s*$", stderr, re.M)
    try:
        rows = json.loads(match.group(1)) if match else None
    except ValueError:
        rows = None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return None
    line = next((line for line in stdout.splitlines() if line.startswith("tables|")), "")
    try:
        names = json.loads(line.split("|", 1)[1]) if line else {}
    except ValueError:
        names = {}  # The refusal stands; only the names are missing.
    return [{key: names.get(row[key], row[key]) if key == "table" else row[key]
             for key in ("table", "key", "base", "now", "written") if key in row} for row in rows]


if __name__ == "__main__":
    main()
