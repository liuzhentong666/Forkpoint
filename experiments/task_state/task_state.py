"""Checkable task state: facts that say which rows they were read from.

A task's state stays in agent_state's Engine as it is (goal, status, next_action,
uncertainties, facts, evidence_refs). This module adds one kind of evidence object,
{"kind": "reads", "fact": key, "revision": uuid, "rows": {table: [primary keys]}}, and
a check against the version engine: a fact is fresh while those rows are the same on
current main as at its revision, stale once one differs or is gone, and unknown when it
has no such evidence or a row cannot be found at its revision. The engine is unchanged,
so states written before this module load as before; their facts are simply unknown.

Each task has one writer (its agent), so checkpoint() publishes against the version it
has just read. The checks call vc._rows_at_keys (sql/008), which only the database
owner may run; agents reach all of this through tool_reply, never through SQL.
"""
import json
import sys
from pathlib import Path
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent_state.objects import digest, encode  # noqa: E402
from agent_state.runtime import Runtime  # noqa: E402
from agent_state.storage import BudgetError, Conflict, Invalid, Missing, StateError  # noqa: E402

READ_TABLES = ("app.products", "app.orders")  # the tables every lab database registers
MAX_KEYS = 16          # rows one fact may depend on
MAX_CHANGED = 3        # changed rows shown per stale fact
CHANGE_FIELDS = {"goal", "status", "next_action", "open_items", "facts", "drop"}


def hex_text(value):
    return f"convert_from(decode('{value.encode('utf-8').hex()}','hex'),'UTF8')"


def check_rows(rows):
    """A based_on.rows value: {table: [primary-key objects]}, at most MAX_KEYS keys in all."""
    if not isinstance(rows, dict) or not rows or set(rows) - set(READ_TABLES):
        raise Invalid("rows must map " + " or ".join(READ_TABLES) + " to lists of primary keys")
    total = 0
    for keys in rows.values():
        if not isinstance(keys, list) or not keys or not all(isinstance(k, dict) and k for k in keys):
            raise Invalid('each table needs a list of primary keys, such as [{"id": 101}]')
        total += len(keys)
    if total > MAX_KEYS:
        raise Invalid(f"a fact may depend on at most {MAX_KEYS} rows")
    return rows


def evidence_of(engine, state):
    """{fact: (ref, evidence)} for the state's reads evidence; other evidence is left alone."""
    found = {}
    for ref in state["evidence_refs"]:
        try:  # evidence is an artifact (Engine.publish checks that namespace); not all of it is JSON
            item = json.loads(engine.objects.get_bytes(ref))
        except (ValueError, UnicodeError):
            continue
        if isinstance(item, dict) and item.get("kind") == "reads" and item.get("fact") in state["facts"]:
            found[item["fact"]] = (ref, item)
    return found


def checkpoint(engine, task_id, command_id, actor, change):
    """Merge an agent's checkpoint into its task's state and publish it; return the engine's result.

    change may set goal, status, next_action and open_items (the state's uncertainties),
    set facts as {key: {"value": v, "based_on": {"revision": r, "rows": {...}}}} (based_on is
    optional), and drop facts by key. A fact that is set again or dropped loses its old reads.
    """
    if not isinstance(change, dict) or not change or set(change) - CHANGE_FIELDS:
        raise Invalid("change may only contain " + ", ".join(sorted(CHANGE_FIELDS)))
    if not isinstance(change.get("facts", {}), dict) or not isinstance(change.get("drop", []), list):
        raise Invalid("facts must be an object and drop a list of fact keys")
    # The merged state depends on the version read below, so a retry would reach the
    # engine as a different request. Recognise it by the checkpoint itself instead.
    marker = "checkpoint " + digest(encode(change))
    try:
        head, _ = engine.head(task_id)
    except Missing:
        head = None
    found = head and engine.index.lookup(head["commands_ref"], command_id)
    if found:
        if engine.objects.get(found["result"]["event_ref"]).get("description") != marker:
            raise Conflict("command_id was already used for a different checkpoint")
        return {**found["result"], "deduplicated": True}
    try:
        current = engine.read(task_id)
        state, version = current["state"], current["version"]
    except Missing:
        state, version = {"goal": "", "status": "running", "next_action": "", "uncertainties": [],
                          "facts": {}, "policy_ref": None, "evidence_refs": []}, None
    facts, refs = dict(state["facts"]), list(state["evidence_refs"])
    reads = evidence_of(engine, state)
    for key in change.get("drop", []):
        if not isinstance(key, str):
            raise Invalid("drop lists fact keys")
        facts.pop(key, None)
        if key in reads:
            refs.remove(reads.pop(key)[0])
    for key, item in (change.get("facts") or {}).items():
        if not isinstance(key, str) or not key or len(key) > 128 or not isinstance(item, dict) or "value" not in item:
            raise Invalid('each fact is {"value": ..., "based_on": {"revision": ..., "rows": {...}}}')
        facts[key] = item["value"]
        if key in reads:
            refs.remove(reads.pop(key)[0])
        based_on = item.get("based_on")
        if based_on is not None:
            if not isinstance(based_on, dict) or set(based_on) != {"revision", "rows"}:
                raise Invalid("based_on needs exactly revision and rows")
            try:
                revision = str(uuid.UUID(based_on["revision"]))
            except (ValueError, TypeError, AttributeError):
                raise Invalid("based_on.revision must be a revision UUID") from None
            refs.append(engine.objects.put_bytes(encode({"kind": "reads", "fact": key, "revision": revision,
                                                         "rows": check_rows(based_on["rows"])})))
    new = dict(state, facts=facts, evidence_refs=refs)
    for field, target in (("goal", "goal"), ("status", "status"), ("next_action", "next_action"),
                          ("open_items", "uncertainties")):
        if field in change:
            new[target] = change[field]
    return engine.publish(task_id, command_id, actor, new, expected_version=version, event=marker)


def compare(sql, revision, head, rows):
    """For each key in rows: its row document at revision and at head (null if not found)."""
    query = f"""WITH k AS (
      SELECT r.key AS tbl, vc.table_id(r.key::regclass) AS tid, x AS pk
      FROM jsonb_each({hex_text(json.dumps(rows))}::jsonb) r, jsonb_array_elements(r.value) x)
    SELECT coalesce(json_agg(json_build_object('table',k.tbl,'key',k.pk,
      'then',(SELECT doc FROM vc._rows_at_keys('{revision}',k.tid,ARRAY[k.pk])),
      'now',(SELECT doc FROM vc._rows_at_keys('{head}',k.tid,ARRAY[k.pk])))),'[]') FROM k;"""
    return json.loads(sql(query))


def freshness(engine, state, sql, head=None):
    """{fact: mark}: fresh, stale (with up to MAX_CHANGED changed rows) or unknown (with why)."""
    head = head or sql("SELECT vc.head();")
    reads, marks = evidence_of(engine, state), {}
    for key in state["facts"]:
        if key not in reads:
            marks[key] = {"status": "unknown", "why": "no recorded reads"}
            continue
        evidence = reads[key][1]
        rows = compare(sql, evidence["revision"], head, evidence["rows"])
        if any(r["then"] is None for r in rows):
            marks[key] = {"status": "unknown", "read_at": evidence["revision"],
                          "why": "a row was not found at that revision; check its key"}
            continue
        changed = [r for r in rows if r["now"] != r["then"]]
        marks[key] = ({"status": "stale", "read_at": evidence["revision"], "changed": changed[:MAX_CHANGED]}
                      if changed else {"status": "fresh", "read_at": evidence["revision"]})
    return marks


def resume(engine, task_id, sql, max_bytes=8192, check=True):
    """The task's context packet (Runtime.context) with a freshness mark for every fact it shows.

    Stale facts left out by the budget are counted in omitted_stale. The whole packet,
    marks included, stays within max_bytes. check=False gives the same packet without any
    marks (the experiment's baseline): only main is added.
    """
    record = engine.read(task_id)
    head = sql("SELECT vc.head();")
    if not check:
        packet = Runtime(engine, ttl=0).context(task_id, max_bytes=max_bytes - 64, fresh=True)
        packet.pop("usage", None)
        packet["main"] = head
        return packet
    marks = freshness(engine, record["state"], sql, head)
    room = len(encode({"freshness": marks, "omitted_stale": 0, "main": head})) + 64
    if max_bytes - room < 256:
        raise BudgetError("Freshness marks leave no room for the context; increase the budget")
    packet = Runtime(engine, ttl=0).context(task_id, max_bytes=max_bytes - room, fresh=True)
    packet.pop("usage", None)
    packet.update(main=head, freshness={k: marks[k] for k in packet["facts"]},
                  omitted_stale=sum(1 for k, m in marks.items() if k not in packet["facts"] and m["status"] == "stale"))
    if len(encode(packet)) > max_bytes:
        raise BudgetError("Context exceeds budget")
    return packet


def tool_reply(engine, sql, task_id, actor, call_id, values, check=True, max_bytes=8192):
    """The agent-facing actions: checkpoint (values["change"]) and resume. Errors come back as data."""
    action = values.get("action")
    try:
        if action == "checkpoint":
            result = checkpoint(engine, task_id, call_id, actor, values.get("change"))
            return {"exit_code": 0, "result": {"version": result["version"], "deduplicated": result["deduplicated"]}}
        if action == "resume":
            return {"exit_code": 0, "result": resume(engine, task_id, sql, max_bytes, check)}
    except Missing:
        return {"error": "no_task_state", "detail": "Nothing has been checkpointed for this task yet."}
    except StateError as error:  # Invalid, TooLarge, Conflict, BudgetError
        return {"error": "invalid_checkpoint" if action == "checkpoint" else "resume_failed", "detail": str(error)}
    return {"error": "unknown_action"}
