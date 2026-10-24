"""Run one agent_lab.py call sequence twice: per-call processes, then through agent_lab_gateway.py.

Each pass creates its own agentdb_test_gpt_* database with `agent_lab.py init` on the existing
local PostgreSQL (kept for inspection). Outputs are compared after replacing the database
name and UUIDs by placeholders; per-call wall time includes starting the client process.
The calls that merge with a read set (merge --reads), a propose with a stale --expected and forks
of an existing branch and of an unknown base must also give their expected outcome.
Usage: python scripts/compare_agent_lab_gateway.py [--output results/agent-lab-gateway-results.json]
"""
import argparse
from datetime import datetime, timezone
import hashlib, json, os, platform, re, subprocess, sys, tempfile, time
from pathlib import Path
project = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", type=Path, default=project / "results" / "agent-lab-gateway-results.json")
out = parser.parse_args().output
flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

def call(env, *argv):
    t = time.perf_counter()
    p = subprocess.run([sys.executable, str(project / "agent_lab.py"), *argv], cwd=project, env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, creationflags=flags)
    return dict(argv=list(argv), exit=p.returncode, stdout=p.stdout, stderr=p.stderr, ms=(time.perf_counter() - t) * 1000)

def sequence(env, label):
    init = call(env, "init")
    info = json.loads(init["stdout"]); db, base = info["database"], info["base"]
    log = [init]
    def c(*argv):
        r = call(env, argv[0], "--database", db, *argv[1:]); log.append(r); return r
    unexpected = []
    def expect(outcome, *argv):
        r = c(*argv)
        got = json.loads(r["stdout"]) if r["exit"] in (0, 2) and r["stdout"].startswith("{") else {}
        got = "merged" if got.get("merged") else got.get("error", f"exit {r['exit']}")
        if got != outcome:
            unexpected.append(dict(call=len(log) - 1, expected=outcome, got=got))
    c("info", "--revision", base)
    a = json.loads(c("fork", "--branch", "gpt-a-1", "--base", base)["stdout"])["head"]
    b = json.loads(c("fork", "--branch", "gpt-b-1", "--base", base)["stdout"])["head"]
    pa = json.loads(c("propose", "--branch", "gpt-a-1", "--expected", a, "--actor", "gpt-a", "--field", "price", "--value", "11", "--code", "PRICE = 11\n")["stdout"])["revision"]
    pb = json.loads(c("propose", "--branch", "gpt-b-1", "--expected", b, "--actor", "gpt-b", "--field", "stock", "--value", "95", "--code", "STOCK = 95\n")["stdout"])["revision"]
    c("diff", "--base", base, "--revision", pa)
    c("read-code", "--revision", pa, "--path", "experiments/pricing.py")
    expect("stale_branch_head", "propose", "--branch", "gpt-a-1", "--expected", base, "--actor", "gpt-a", "--field", "price", "--value", "11", "--code", "PRICE = 11\n")
    c("merge", "--revision", pa, "--expected", base, "--actor", "gpt-a")
    c("merge", "--revision", pb, "--expected", base, "--actor", "gpt-b")  # stale_head
    main = json.loads(c("info")["stdout"])["current_main"]
    c("merge", "--revision", pb, "--expected", main, "--actor", "gpt-b")  # whole-row conflict expected
    c("verify")  # fails: stock not yet 95
    b2 = json.loads(c("fork", "--branch", "gpt-b-2", "--base", main)["stdout"])["head"]
    pb2 = json.loads(c("propose", "--branch", "gpt-b-2", "--expected", b2, "--actor", "gpt-b", "--field", "stock", "--value", "95", "--code", "STOCK = 95\n")["stdout"])["revision"]
    main2 = json.loads(c("merge", "--revision", pb2, "--expected", main, "--actor", "gpt-b")["stdout"])["main"]
    c("verify")
    c("fork", "--branch", "main", "--base", main)  # ValueError in action
    c("propose", "--branch", "gpt-b-2", "--expected", b2, "--actor", "gpt-b", "--field", "stock", "--value", "9.5", "--code", "X=1")  # parser error
    # merge --reads from a base behind main: the stock decrement again is refused (written row
    # already holds that value), as is a price decided from row 101 as it was; from main it lands.
    reads = '{"app.products": [{"id": 101}]}'
    b3 = json.loads(c("fork", "--branch", "gpt-b-3", "--base", main)["stdout"])["head"]
    pb3 = json.loads(c("propose", "--branch", "gpt-b-3", "--expected", b3, "--actor", "gpt-b", "--field", "stock", "--value", "95", "--code", "STOCK = 95\n")["stdout"])["revision"]
    expect("read_conflict", "merge", "--revision", pb3, "--reads", "{}", "--actor", "gpt-b")
    a3 = json.loads(c("fork", "--branch", "gpt-a-3", "--base", main)["stdout"])["head"]
    pa3 = json.loads(c("propose", "--branch", "gpt-a-3", "--expected", a3, "--actor", "gpt-a", "--field", "price", "--value", "12", "--code", "PRICE = 12\n")["stdout"])["revision"]
    expect("read_conflict", "merge", "--revision", pa3, "--reads", reads, "--actor", "gpt-a")
    a4 = json.loads(c("fork", "--branch", "gpt-a-4", "--base", main2)["stdout"])["head"]
    pa4 = json.loads(c("propose", "--branch", "gpt-a-4", "--expected", a4, "--actor", "gpt-a", "--field", "price", "--value", "11", "--code", "PRICE = 11  # rechecked\n")["stdout"])["revision"]
    expect("merged", "merge", "--revision", pa4, "--reads", reads, "--actor", "gpt-a")
    if c("verify")["exit"]:
        unexpected.append(dict(call=len(log) - 1, expected="verified", got="verify failed"))
    expect("invalid_read_set", "merge", "--revision", pa4, "--reads", '{"app.products": [{"sku": "x"}]}', "--actor", "gpt-a")
    expect("branch_exists", "fork", "--branch", "gpt-a-4", "--base", main2)  # made above
    expect("not_found", "fork", "--branch", "gpt-a-5", "--base", "99999999-9999-9999-9999-999999999999")
    return db, log, unexpected

def normalize(text, db):
    names = {}
    text = text.replace(db, "<db>")
    text = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", lambda m: names.setdefault(m.group(0), f"<uuid{len(names)}>"), text)
    return text

def sources():
    files = [project / name for name in ("agent_lab.py", "agent_lab_gateway.py", "db.py", "review_local.py")]
    return {str(f.relative_to(project)): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in files + sorted((project / "sql").glob("*.sql"))}
before = sources()
base_env = dict(os.environ); base_env.pop("AGENTDB_LAB_GATEWAY", None)
spawn_db, spawn, spawn_unexpected = sequence(base_env, "spawn")
rendezvous = Path(tempfile.mkdtemp(prefix="agentdb-gateway-")) / "gateway.json"
gw_env = dict(base_env, AGENTDB_LAB_GATEWAY=str(rendezvous))
gateway = subprocess.Popen([sys.executable, str(project / "agent_lab_gateway.py"), "--idle-timeout", "10"], cwd=project, env=gw_env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=flags)
while not rendezvous.exists():
    if gateway.poll() is not None:
        raise SystemExit("gateway did not start: " + gateway.stderr.read())
    time.sleep(0.05)
gw_db, gw, gw_unexpected = sequence(gw_env, "gateway")
idle_started = time.perf_counter()
gw_out, gw_err = gateway.communicate(timeout=120)  # Exits by itself after 10 s without requests.
idle_exit = dict(exit_code=gateway.returncode, seconds_after_last_call=round(time.perf_counter() - idle_started, 1),
                 rendezvous_removed=not rendezvous.exists())

def joined(log, db):
    return [normalize(json.dumps(dict(argv=r["argv"], exit=r["exit"], stdout=r["stdout"], stderr=r["stderr"] if r["exit"] != 1 else r["stderr"].strip().splitlines()[-1])), db) for r in log]
js, jg = joined(spawn, spawn_db), joined(gw, gw_db)
diffs = [(i, a, b) for i, (a, b) in enumerate(zip(js, jg)) if a != b]
def med(xs): xs = sorted(xs); return xs[len(xs)//2]
summary = dict(recorded_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    environment=dict(python=platform.python_version(), platform=platform.platform()),
    sources_sha256=before, sources_unchanged_during_run=sources() == before,
    note="Local WSL PostgreSQL only; ms include client process start. Not a cloud result.", real_cloud_verified=False,
    spawn_db=spawn_db, gateway_db=gw_db, calls=len(spawn), identical_normalized=not diffs and len(js) == len(jg), diffs=diffs,
    exits=[r["exit"] for r in spawn], unexpected_outcomes=dict(spawn=spawn_unexpected, gateway=gw_unexpected),
    spawn_ms=[round(r["ms"]) for r in spawn], gateway_ms=[round(r["ms"]) for r in gw],
    spawn_worker_median_ms=round(med([r["ms"] for r in spawn[1:]])), gateway_worker_median_ms=round(med([r["ms"] for r in gw[1:]])),
    spawn_worker_total_s=round(sum(r["ms"] for r in spawn[1:]) / 1000, 2), gateway_worker_total_s=round(sum(r["ms"] for r in gw[1:]) / 1000, 2),
    gateway_log=gw_err.splitlines(), databases_retained=[spawn_db, gw_db], gateway_idle_exit=idle_exit)
out.write_text(json.dumps(summary, indent=1), encoding="utf-8")
print(json.dumps({k: summary[k] for k in ("identical_normalized", "unexpected_outcomes", "spawn_worker_median_ms", "gateway_worker_median_ms",
                                         "spawn_worker_total_s", "gateway_worker_total_s", "databases_retained", "gateway_idle_exit", "sources_unchanged_during_run")}, indent=1))
if diffs:
    raise SystemExit(f"{len(diffs)} outputs differ; see {out}")
if spawn_unexpected or gw_unexpected:
    raise SystemExit(f"calls with an expected outcome gave another; see {out}")
