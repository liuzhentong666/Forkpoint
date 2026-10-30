"""Object storage contract and its in-memory backend. Nothing here persists across a restart."""
from dataclasses import dataclass
import threading


class StateError(Exception):
    code = "state_error"


class Missing(StateError):
    code = "not_found"


class Conflict(StateError):
    code = "conflict"


class Invalid(StateError):
    code = "invalid_request"


class TooLarge(Invalid):
    code = "size_limit"


class Corrupt(StateError):
    code = "integrity_error"


class BudgetError(Invalid):
    code = "context_budget"


@dataclass(frozen=True)
class Object:
    data: bytes
    generation: int | str  # Compatibility field name; opaque outside its backend.


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._values = dict(reads=0, writes=0, downloaded_bytes=0,
                            uploaded_bytes=0, cas_conflicts=0)

    def add(self, **values):
        with self._lock:
            for key, value in values.items():
                self._values[key] = self._values.get(key, 0) + value

    def snapshot(self):
        with self._lock:
            return dict(self._values)


class MemoryStore:
    """In-memory backend with counters for fault injection; never a production persistence option."""
    def __init__(self):
        self.metrics = Metrics()
        self._objects = {}
        self._generation = 0
        self._lock = threading.Lock()

    def get(self, key, max_bytes=1048576):
        self.metrics.add(reads=1)
        with self._lock:
            if key not in self._objects:
                raise Missing(key)
            obj = self._objects[key]
            if len(obj.data) > max_bytes:
                raise TooLarge(key)
            self.metrics.add(downloaded_bytes=len(obj.data))
            return obj

    def put(self, key, data, expected_generation):
        self.metrics.add(writes=1)
        with self._lock:
            old = self._objects.get(key)
            actual = old.generation if old else 0
            if actual != expected_generation:
                self.metrics.add(cas_conflicts=1)
                raise Conflict(key)
            self._generation += 1
            self._objects[key] = Object(bytes(data), self._generation)
            self.metrics.add(uploaded_bytes=len(data))
            return self._generation
