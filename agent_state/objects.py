"""Content-addressed objects and a persistent, bounded-node command index."""
from collections import OrderedDict
import hashlib
import json
import re
import threading
from .storage import Conflict, Corrupt, Invalid, TooLarge


def encode(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as error:
        raise Invalid("Value must be finite JSON") from error


def digest(data):
    return hashlib.sha256(data).hexdigest()


def check_hash(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise Invalid("Expected a lowercase SHA-256 object ID")
    return value


class Objects:
    def __init__(self, store, prefix="agentdb-state", cache_bytes=4 * 1024 * 1024):
        if not re.fullmatch(r"[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*", prefix):
            raise Invalid("Invalid object prefix")
        if cache_bytes < 0:
            raise Invalid("Object cache byte limit cannot be negative")
        self.store, self.prefix = store, prefix
        self.cache_bytes = cache_bytes
        self.max_entries = 512
        self._cache = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def _remember(self, key, data):
        with self._lock:
            old = self._cache.pop(key, None)
            if old is not None:
                self._bytes -= len(old)
            if len(data) <= self.cache_bytes:
                self._cache[key] = data
                self._bytes += len(data)
            while self._bytes > self.cache_bytes or len(self._cache) > self.max_entries:
                _, old = self._cache.popitem(last=False)
                self._bytes -= len(old)

    def cache_metrics(self):
        with self._lock:
            return dict(objects=len(self._cache), bytes=self._bytes,
                        max_objects=self.max_entries, max_bytes=self.cache_bytes)

    def put(self, value):
        data = encode(value)
        if len(data) > 32768:
            raise TooLarge("JSON object exceeds 32 KiB; split it or store an artifact reference")
        return self.put_bytes(data, "objects")

    def put_bytes(self, data, namespace="artifacts"):
        if len(data) > 1048576:
            raise TooLarge("Artifact exceeds 1 MiB; split into referenced chunks")
        object_id = digest(data)
        key = f"{self.prefix}/{namespace}/{object_id}"
        try:
            self.store.put(key, data, 0)
        except Conflict:
            existing = self.store.get(key, max_bytes=max(len(data), 1)).data
            if existing != data:
                raise Corrupt("Content-addressed object was replaced: " + object_id)
        self._remember(key, bytes(data))
        return object_id

    def get_bytes(self, object_id, namespace="artifacts"):
        check_hash(object_id)
        key = f"{self.prefix}/{namespace}/{object_id}"
        with self._lock:
            data = self._cache.get(key)
            if data is not None:
                self._cache.move_to_end(key)
        if data is None:
            data = self.store.get(key, max_bytes=32768 if namespace == "objects" else 1048576).data
            if digest(data) != object_id:
                raise Corrupt("Object hash mismatch: " + object_id)
            self._remember(key, data)
        return data

    def get(self, object_id):
        try:
            return json.loads(self.get_bytes(object_id, "objects"))
        except (ValueError, UnicodeError) as error:
            raise Corrupt("Invalid stored JSON") from error


class CommandIndex:
    LEAF_SIZE = 8

    def __init__(self, objects):
        self.objects = objects

    def lookup(self, root, command_id):
        key = digest(command_id.encode("utf-8"))
        depth = 0
        while root:
            node = self.objects.get(root)
            if node["kind"] == "leaf":
                return node["items"].get(key)
            if depth >= 64:
                raise Corrupt("Command index depth exceeded")
            root = node["children"].get(key[depth])
            depth += 1
        return None

    def insert(self, root, command_id, value):
        key = digest(command_id.encode("utf-8"))
        return self._insert(root, key, value, 0)

    def _build(self, items, depth):
        if len(items) <= self.LEAF_SIZE:
            return self.objects.put({"kind": "leaf", "items": items})
        if depth >= 64:
            raise Corrupt("Command hash collision")
        groups = {}
        for key, value in items.items():
            groups.setdefault(key[depth], {})[key] = value
        children = {letter: self._build(group, depth + 1) for letter, group in groups.items()}
        return self.objects.put({"kind": "branch", "children": children})

    def _insert(self, root, key, value, depth):
        node = self.objects.get(root) if root else {"kind": "leaf", "items": {}}
        if node["kind"] == "leaf":
            if key in node["items"]:
                raise Conflict("Command already indexed")
            return self._build({**node["items"], key: value}, depth)
        if depth >= 64:
            raise Corrupt("Command index depth exceeded")
        children = dict(node["children"])
        letter = key[depth]
        children[letter] = self._insert(children.get(letter), key, value, depth + 1)
        return self.objects.put({"kind": "branch", "children": children})
