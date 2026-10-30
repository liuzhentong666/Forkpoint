"""Resident bounded cache and context assembly, independent of model sessions."""
from collections import OrderedDict
import copy
import json
import threading
import time
from .objects import encode
from .storage import BudgetError, Invalid


class Runtime:
    def __init__(self, engine, max_tasks=128, max_bytes=4 * 1024 * 1024, ttl=30,
                 token_counter=None, tokenizer_name=None, clock=time.monotonic):
        if max_tasks < 1 or max_bytes < 1 or ttl < 0:
            raise Invalid("Cache limits must be positive and TTL nonnegative")
        self.engine, self.max_tasks, self.max_bytes, self.ttl = engine, max_tasks, max_bytes, ttl
        self.token_counter, self.tokenizer_name = token_counter, tokenizer_name
        self.clock = clock
        self._cache = OrderedDict()
        self._size = 0
        self._epoch = 0
        self._lock = threading.RLock()
        self.cache_hits = 0

    def read(self, task_id, fresh=False):
        with self._lock:
            epoch = self._epoch
            item = self._cache.get(task_id)
            if item and not fresh and self.clock() - item[0] < self.ttl:
                self.cache_hits += 1
                self._cache.move_to_end(task_id)
                return json.loads(item[1])
        result = self.engine.read(task_id)
        data = encode(result)
        with self._lock:
            existing = self._cache.get(task_id)
            # Sort application versions, never storage tokens.
            if existing and json.loads(existing[1])["version"] > result["version"]:
                return json.loads(existing[1])
            if epoch != self._epoch:
                # An invalidation arrived while this read was in flight. Return
                # its snapshot, but do not cache it as current for another TTL.
                return copy.deepcopy(result)
            old = self._cache.pop(task_id, None)
            if old:
                self._size -= len(old[1])
            if len(data) <= self.max_bytes:
                self._cache[task_id] = (self.clock(), data)
                self._size += len(data)
            while len(self._cache) > self.max_tasks or self._size > self.max_bytes:
                _, old = self._cache.popitem(last=False)
                self._size -= len(old[1])
        return copy.deepcopy(result)

    def invalidate(self, task_id, generation=None, deleted=False):
        with self._lock:
            item = self._cache.get(task_id)
            if item is None:
                self._epoch += 1
                return False
            if generation is None or deleted or generation != json.loads(item[1])["generation"]:
                self._epoch += 1
                self._cache.pop(task_id)
                self._size -= len(item[1])
                return True
            return False

    def publish(self, task_id, **command):
        try:
            return self.engine.publish(task_id, **command)
        finally:
            self.invalidate(task_id)

    def context(self, task_id, max_bytes=8192, max_tokens=None, fact_keys=None, fresh=False):
        if type(max_bytes) is not int or not 256 <= max_bytes <= 65536:
            raise Invalid("max_bytes must be between 256 and 65536")
        if max_tokens is not None:
            if self.token_counter is None:
                raise Invalid("Exact token budgets require a configured tokenizer")
            if type(max_tokens) is not int or max_tokens < 1:
                raise Invalid("max_tokens must be positive")
        record = self.read(task_id, fresh=fresh)
        state = record["state"]
        if fact_keys is not None and (not isinstance(fact_keys, list) or
                                      any(not isinstance(x, str) for x in fact_keys) or len(fact_keys) > 128):
            raise Invalid("fact_keys must be a list of at most 128 strings")
        keys = sorted(state["facts"]) if fact_keys is None else list(dict.fromkeys(fact_keys))
        missing_keys = [key for key in keys if key not in state["facts"]]
        if missing_keys:
            raise Invalid("Unknown facts: " + str(missing_keys))
        packet = dict(task_id=task_id, version=record["version"], state_ref=record["state_ref"],
                      goal=state["goal"], status=state["status"], next_action=state["next_action"],
                      uncertainties=state["uncertainties"], policy_ref=state["policy_ref"],
                      facts={}, evidence_refs=[], omitted_facts=len(state["facts"]),
                      omitted_evidence=len(state["evidence_refs"]),
                      continuation="Read state_ref or request selected facts/evidence; omitted values are not absent facts.")

        def measure():
            packet.pop("usage", None)
            content = encode(packet)
            packet["usage"] = dict(content_utf8_bytes=len(content),
                                    content_tokens=self.token_counter(content.decode("utf-8")) if self.token_counter else None,
                                    tokenizer=self.tokenizer_name,
                                    note="Usage excludes this field; budgets include the entire returned JSON.")
            raw = encode(packet)
            actual_tokens = self.token_counter(raw.decode("utf-8")) if self.token_counter else None
            return len(raw) <= max_bytes and (max_tokens is None or actual_tokens <= max_tokens)

        if not measure():
            raise BudgetError("Essential state exceeds context budget; split the task or increase the budget")
        for key in keys:
            packet["facts"][key] = state["facts"][key]
            packet["omitted_facts"] -= 1
            if not measure():
                del packet["facts"][key]
                packet["omitted_facts"] += 1
        for ref in state["evidence_refs"]:
            packet["evidence_refs"].append(ref)
            packet["omitted_evidence"] -= 1
            if not measure():
                packet["evidence_refs"].pop()
                packet["omitted_evidence"] += 1
        if not measure():
            raise BudgetError("Context exceeds budget")
        return packet

    def metrics(self):
        with self._lock:
            cache = dict(tasks=len(self._cache), bytes=self._size, hits=self.cache_hits,
                         max_tasks=self.max_tasks, max_bytes=self.max_bytes, ttl_seconds=self.ttl)
        return dict(cache=cache, object_cache=self.engine.objects.cache_metrics(),
                    storage=self.engine.store.metrics.snapshot(),
                    note="Storage counters are logical operations and payload bytes.")
