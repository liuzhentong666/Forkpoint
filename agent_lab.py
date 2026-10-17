"""Small real-agent exercise against a dedicated local PostgreSQL test database.

All worker commands use agentdb_client. This wrapper narrows the exercise surface;
it is not an OS sandbox or a substitute for an independently authenticated login.
"""
import argparse
import ast
from decimal import Decimal
import json
from pathlib import Path
import re
import uuid

from db import add_connection_arguments
from review_local import connect, seed


def literal(value):
    return "'" + value.replace("'", "''") + "'"


def revision(value):
    return str(uuid.UUID(value))


def branch(value):
    if not value or not re.fullmatch(r"gpt-[a-z0-9-]{1,50}", value):
        raise ValueError("Use a gpt- branch name")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
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
    args = parser.parse_args()
    if args.action == "init":
        database = "agentdb_test_gpt_" + uuid.uuid4().hex[:10]
        owner = connect(args, database)
        owner.check()
        owner.create_database(database)
        owner.install()
        seed(owner)
        result = dict(database=database, base=owner.sql("SELECT vc.head();"), product_id=101,
                      goal="price +10%, stock -5, preserve both code policies and both fields", real_cloud_verified=False)
        print(json.dumps(result, indent=2))
        return
    if not args.database or not re.fullmatch(r"agentdb_test_gpt_[0-9a-f]{10}", args.database):
        parser.error("Explicit dedicated agentdb_test_gpt_<10 hex> database is required")
    worker = connect(args, args.database, "agentdb_client")
    if args.action == "info":
        head = worker.sql("SELECT vc.head();")
        fixed = revision(args.revision) if args.revision else head
        product = json.loads(worker.sql(f"SELECT doc FROM vc.rows_at('{fixed}',vc.table_id('app.products')) WHERE pk='{{\"id\":101}}';"))
        result = dict(database=args.database, effective_role=worker.sql("SELECT current_user;"),
                      current_main=head, read_revision=fixed, product=product,
                      conflict_model="whole row; different columns on the same row may conflict")
    elif args.action == "read-code":
        if not args.path or not args.revision:
            parser.error("read-code requires path and revision")
        fixed = revision(args.revision)
        result = json.loads(worker.sql(f"""
          SELECT json_build_object('revision','{fixed}','path',{literal(args.path)},
            'content',convert_from(vc.read_file('{fixed}',{literal(args.path)}),'UTF8'));
        """))
    elif args.action == "fork":
        created = worker.sql(f"SELECT vc.fork({literal(branch(args.branch))},'{revision(args.base)}');")
        result = dict(branch=args.branch, head=created)
    elif args.action == "propose":
        if not args.actor or not args.field or args.value is None or args.code is None:
            parser.error("propose requires actor, field, value and code")
        if len(args.code.encode()) > 4096:
            parser.error("Policy code exceeds 4096 bytes")
        ast.parse(args.code)
        value = Decimal(args.value)
        if not value.is_finite() or not 0 <= value <= 100000:
            parser.error("Finite nonnegative value up to 100000 required")
        if args.field == "stock" and value != value.to_integral_value():
            parser.error("Stock must be an integer")
        path = "experiments/" + ("pricing.py" if args.field == "price" else "inventory.py")
        content = args.code.encode().hex()
        created = worker.sql(f"""
          BEGIN;
          SELECT vc.begin_change({literal(branch(args.branch))},'{revision(args.expected)}',{literal(args.actor)},'real GPT proposal') AS draft \gset
          UPDATE agent.products SET {args.field}={value} WHERE id=101;
          SELECT vc.write_file('{path}',decode('{content}','hex')) AS blob \gset
          SELECT vc.finish_change() AS revision \gset
          COMMIT;
          SELECT :'revision';
        """)
        result = dict(branch=args.branch, revision=created, code_path=path)
    elif args.action == "diff":
        base, proposed = revision(args.base), revision(args.revision)
        result = json.loads(worker.sql(f"""
          SELECT json_build_object(
            'rows',(SELECT coalesce(json_agg(d),'[]') FROM vc.diff_rows('{base}','{proposed}',vc.table_id('app.products')) d),
            'files',(SELECT coalesce(json_agg(d),'[]') FROM vc.diff_files('{base}','{proposed}') d));
        """))
    elif args.action == "merge":
        if not args.actor:
            parser.error("merge requires actor")
        response = worker.execute(f"""
          BEGIN;
          SELECT vc.merge('main','{revision(args.revision)}','{revision(args.expected)}',{literal(args.actor)}) AS landed \gset
          COMMIT;
          SELECT :'landed';
        """)
        if response.returncode:
            match = re.search(r"ERROR:\s+([A-Z0-9]{5}):", response.stderr)
            state = match.group(1) if match else "unknown"
            print(json.dumps(dict(error="stale_head" if state == "40001" else "merge_conflict" if state == "P0001" else "database_error",
                                  sqlstate=state, detail=response.stderr.strip())))
            raise SystemExit(2)
        result = dict(merged=True, main=response.stdout.strip())
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
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
