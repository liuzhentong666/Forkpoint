"""Publish bounded state snapshots; only a task head is mutable."""
from datetime import datetime, timezone
import json
import re
from .objects import Objects, CommandIndex, check_hash, digest, encode
from .storage import Conflict, Corrupt, Invalid, Missing, TooLarge
from .heads import CASHeads

STATUSES = {"pending", "running", "needs_recovery", "completed", "failed", "cancelled"}


def identifier(value, label="ID"):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise Invalid(label + " must be 1-128 letters, digits, underscores or hyphens")
    return value


def state_value(value):
    if not isinstance(value, dict):
        raise Invalid("state must be an object")
    allowed = {"goal", "status", "next_action", "uncertainties", "facts", "policy_ref", "evidence_refs"}
    if set(value) - allowed:
        raise Invalid("Unknown state fields: " + str(sorted(set(value) - allowed)))
    result = dict(goal="", status="pending", next_action="", uncertainties=[],
                  facts={}, policy_ref=None, evidence_refs=[])
    result.update(value)
    for key in ("goal", "next_action"):
        if not isinstance(result[key], str) or len(result[key]) > 2048:
            raise Invalid(key + " must be text of at most 2048 characters")
    if not result["goal"].strip() or not isinstance(result["status"], str) or result["status"] not in STATUSES:
        raise Invalid("Nonempty goal and valid status required")
    if not isinstance(result["facts"], dict) or len(result["facts"]) > 128:
        raise Invalid("facts must contain at most 128 named facts")
    if not isinstance(result["uncertainties"], list) or len(result["uncertainties"]) > 16 or any(
            not isinstance(x, str) or len(x) > 1024 for x in result["uncertainties"]):
        raise Invalid("uncertainties must be at most 16 short strings")
    if not isinstance(result["evidence_refs"], list) or len(result["evidence_refs"]) > 64:
        raise Invalid("At most 64 evidence references are allowed")
    for ref in result["evidence_refs"]:
        check_hash(ref)
    if result["policy_ref"] is not None:
        check_hash(result["policy_ref"])
    encoded = encode(result)
    if len(encoded) > 32768:
        raise TooLarge("State exceeds 32 KiB")
    return json.loads(encoded)


class Engine:
    def __init__(self, store, prefix="agentdb-state", object_cache_bytes=4 * 1024 * 1024,
                 head_store=None):
        self.store = store
        self.heads = head_store or getattr(store, "heads", None) or CASHeads(store)
        self.objects = Objects(store, prefix, object_cache_bytes)
        self.index = CommandIndex(self.objects)

    def head_key(self, task_id):
        return f"{self.objects.prefix}/tasks/{identifier(task_id, 'task_id')}/head.json"

    def head(self, task_id):
        obj = self.heads.get(self.head_key(task_id), max_bytes=4096)
        try:
            value = json.loads(obj.data)
            if value["protocol"] != 1 or value["task_id"] != task_id or value["version"] < 0:
                raise Corrupt("Invalid task head")
            for key in ("state_ref", "event_ref", "commands_ref"):
                check_hash(value[key])
            return value, obj.generation
        except (ValueError, KeyError, TypeError) as error:
            raise Corrupt("Invalid task head") from error

    def read(self, task_id):
        head, generation = self.head(task_id)
        state = state_value(self.objects.get(head["state_ref"]))
        return dict(task_id=task_id, version=head["version"], generation=generation,
                    state_ref=head["state_ref"], state=state)

    def _receipt(self, head, command_id, fingerprint):
        found = self.index.lookup(head["commands_ref"], command_id)
        if found is None:
            return None
        if found["command_id"] != command_id or found["request_hash"] != fingerprint:
            raise Conflict("command_id was already used for a different request")
        return {**found["result"], "deduplicated": True}

    def publish(self, task_id, command_id, actor, state, expected_version=None, event=""):
        identifier(task_id, "task_id")
        identifier(command_id, "command_id")
        identifier(actor, "actor")
        if expected_version is not None and (type(expected_version) is not int or expected_version < 0):
            raise Invalid("expected_version must be a nonnegative integer; omit only when creating")
        if not isinstance(event, str) or len(event.encode("utf-8")) > 4096:
            raise TooLarge("Event description must be text of at most 4096 bytes")
        state = state_value(state)
        fingerprint = digest(encode(dict(task_id=task_id, actor=actor, state=state,
                                         expected_version=expected_version, event=event)))
        try:
            old, generation = self.head(task_id)
        except Missing:
            old, generation = None, 0
        if old:
            previous = self._receipt(old, command_id, fingerprint)
            if previous:
                return previous
        if old is None and expected_version is not None:
            raise Missing(task_id)
        if old and old["version"] != expected_version:
            raise Conflict(f"Expected version {expected_version}; current version is {old['version']}")
        # A published state must not point to missing evidence. Verify only new
        # references; unchanged references were verified when first published.
        old_refs = set()
        if old:
            old_state = self.objects.get(old["state_ref"])
            old_refs.update(old_state["evidence_refs"])
            old_refs.add(old_state["policy_ref"])
        refs = set(state["evidence_refs"])
        refs.add(state["policy_ref"])
        for ref in refs - old_refs - {None}:
            self.objects.get_bytes(ref)
        version = old["version"] + 1 if old else 0
        state_ref = self.objects.put(state)
        event_ref = self.objects.put(dict(task_id=task_id, version=version, command_id=command_id,
                                         actor=actor, description=event,
                                         recorded_at=datetime.now(timezone.utc).isoformat(),
                                         state_ref=state_ref, previous=old["event_ref"] if old else None))
        result = dict(task_id=task_id, version=version, state_ref=state_ref, event_ref=event_ref)
        receipt = dict(command_id=command_id, request_hash=fingerprint, result=result)
        commands_ref = self.index.insert(old["commands_ref"] if old else None, command_id, receipt)
        head = dict(protocol=1, task_id=task_id, version=version, state_ref=state_ref,
                    event_ref=event_ref, commands_ref=commands_ref)
        try:
            self.heads.publish(self.head_key(task_id), encode(head), generation)
        except Conflict:
            current, _ = self.head(task_id)
            previous = self._receipt(current, command_id, fingerprint)
            if previous:
                return previous
            raise Conflict("Another writer published first; reread the task before deciding to retry")
        return {**result, "deduplicated": False}

    def history(self, task_id, limit=20, cursor=None):
        identifier(task_id, "task_id")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise Invalid("History page limit must be between 1 and 100")
        if cursor:
            check_hash(cursor)
        else:
            head, _ = self.head(task_id)
            cursor = head["event_ref"]
        events = []
        size = 0
        while cursor and len(events) < limit:
            item = self.objects.get(cursor)
            if item.get("task_id") != task_id:
                raise Invalid("History cursor belongs to another task")
            entry = dict(event_ref=cursor, **item)
            next_size = len(encode(entry))
            if size + next_size > 131072:
                break
            size += next_size
            events.append(entry)
            cursor = item["previous"]
        return dict(events=events, next_cursor=cursor)
