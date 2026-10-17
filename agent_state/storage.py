"""Object storage contract and original backends. MemoryStore is test-only."""
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
                            uploaded_bytes=0, cas_conflicts=0, sdk_calls=0)

    def add(self, **values):
        with self._lock:
            for key, value in values.items():
                self._values[key] = self._values.get(key, 0) + value

    def snapshot(self):
        with self._lock:
            return dict(self._values)


class MemoryStore:
    """In-memory fault-injection backend; never a production persistence option."""
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


class GCSStore:
    provider = "gcs"

    def check(self):
        self.bucket.reload(timeout=20)
        return dict(provider="gcs", bucket=self.bucket.name, location=self.bucket.location,
                    note="Metadata access verified; object write/CAS still requires smoke test")

    def __init__(self, bucket, project=None, client=None):
        from google.cloud import storage
        from google.api_core import exceptions
        from google.cloud.storage.retry import DEFAULT_RETRY_IF_GENERATION_SPECIFIED
        if not bucket or "/" in bucket:
            raise Invalid("Supply an existing GCS bucket name, without gs:// or a prefix")
        self.client = client or storage.Client(project=project or None)
        self.bucket = self.client.bucket(bucket)
        self.metrics = Metrics()
        self.errors = exceptions
        self.conditional_retry = DEFAULT_RETRY_IF_GENERATION_SPECIFIED

    def get(self, key, max_bytes=1048576):
        self.metrics.add(reads=1)
        # A head can change between metadata and download. Pin a generation,
        # and retry discovery when the old generation is no longer available.
        for _ in range(3):
            self.metrics.add(sdk_calls=1)
            blob = self.bucket.get_blob(key, timeout=20)
            if blob is None:
                raise Missing(key)
            if blob.size is None or int(blob.size) > max_bytes:
                raise TooLarge(key)
            generation = int(blob.generation)
            try:
                self.metrics.add(sdk_calls=1)
                data = blob.download_as_bytes(if_generation_match=generation,
                                              timeout=20, checksum="auto")
            except (self.errors.NotFound, self.errors.PreconditionFailed):
                continue
            if len(data) > max_bytes:
                raise TooLarge(key)
            self.metrics.add(downloaded_bytes=len(data))
            return Object(data, generation)
        raise Conflict("Object changed repeatedly while reading " + key)

    def put(self, key, data, expected_generation):
        self.metrics.add(writes=1, sdk_calls=1)
        blob = self.bucket.blob(key)
        try:
            blob.upload_from_string(data, content_type="application/octet-stream",
                                    if_generation_match=expected_generation,
                                    retry=self.conditional_retry, timeout=30, checksum="auto")
        except self.errors.PreconditionFailed as error:
            self.metrics.add(cas_conflicts=1)
            raise Conflict(key) from error
        self.metrics.add(uploaded_bytes=len(data))
        return int(blob.generation)
