"""OSS immutable objects and position-conditional append-only task heads.

The SDK is loaded only when this backend is selected. No ordinary PUT is used
to update a head. See docs/STORAGE_BACKENDS.md for namespace ownership and cap.
"""
import hashlib
import os
import re
import struct
import threading
from urllib.parse import urlparse
from .storage import Object, Metrics, Conflict, Corrupt, Invalid, Missing, TooLarge

BLOCK = 8192
MAGIC = b"ADBH001\n"
HEADER = 44
MAX_LOG = 4 * 1024**3


def credentials_provider(sdk):
    mode = os.environ.get("AGENTDB_OSS_CREDENTIALS", "environment")
    if mode == "environment":
        return sdk.credentials.EnvironmentVariableCredentialsProvider()
    if mode != "ecs_ram_role":
        raise Invalid("AGENTDB_OSS_CREDENTIALS must be environment or ecs_ram_role")
    from alibabacloud_credentials.client import Client
    from alibabacloud_credentials.models import Config
    client = Client(Config(type="ecs_ram_role", disable_imds_v1=True,
                           role_name=os.environ.get("ALIBABA_CLOUD_ECS_METADATA")))
    lock = threading.Lock()

    def current():
        # The credentials SDK owns refresh/expiry. Never freeze the first STS
        # token in a static OSS credentials provider for a resident process.
        with lock:
            value = client.get_credential()
            return sdk.credentials.Credentials(value.get_access_key_id(),
                                               value.get_access_key_secret(),
                                               value.get_security_token())

    return sdk.credentials.CredentialsProviderFunc(current)


def frame(data):
    if not isinstance(data, bytes) or not 0 < len(data) <= 4096:
        raise TooLarge("Head payload must be 1..4096 bytes")
    raw = MAGIC + struct.pack(">I", len(data)) + hashlib.sha256(data).digest() + data
    return raw.ljust(BLOCK, b"\0")


def unframe(raw, max_bytes):
    if len(raw) != BLOCK or raw[:8] != MAGIC:
        raise Corrupt("Invalid OSS head record")
    size = struct.unpack(">I", raw[8:12])[0]
    if not 0 < size <= 4096 or any(raw[HEADER + size:]):
        raise Corrupt("Invalid OSS record length or padding")
    data = raw[HEADER:HEADER + size]
    if hashlib.sha256(data).digest() != raw[12:44]:
        raise Corrupt("OSS head checksum mismatch")
    if size > max_bytes:
        raise TooLarge("Head exceeds requested read size")
    return data


class OSSStore:
    provider = "oss"

    def __init__(self, bucket, region, endpoint=None, client=None, max_log_bytes=MAX_LOG):
        if not bucket or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket):
            raise Invalid("Supply an OSS bucket name without a URL")
        if not region or not re.fullmatch(r"[a-z0-9-]+", region):
            raise Invalid("OSS region is required, for example cn-hangzhou")
        if endpoint and (urlparse(endpoint).scheme != "https" or not urlparse(endpoint).netloc):
            raise Invalid("OSS endpoint must be an HTTPS URL")
        import alibabacloud_oss_v2 as sdk
        self.sdk, self.bucket, self.region = sdk, bucket, region
        self.metrics = Metrics()
        if client is None:
            config = sdk.config.load_default()
            config.region, config.endpoint = region, endpoint
            config.credentials_provider = credentials_provider(sdk)
            config.connect_timeout, config.readwrite_timeout = 10, 30
            # Do not let an upload helper reinterpret an uncertain append.
            config.retry_max_attempts = 1
            client = sdk.Client(config)
        self.client = client
        self.heads = OSSHeads(self, max_log_bytes)

    def _call(self, operation, request):
        self.metrics.add(sdk_calls=1)
        try:
            return getattr(self.client, operation)(request)
        except self.sdk.exceptions.ServiceError as error:
            if error.code == "NoSuchKey":
                raise Missing(request.key) from error
            if error.code in {"PositionNotEqualToLength", "FileAlreadyExists", "PreconditionFailed"}:
                self.metrics.add(cas_conflicts=1)
                raise Conflict("OSS conditional request failed") from error
            if error.code == "ObjectNotAppendable":
                raise Corrupt("OSS head is not an appendable object") from error
            raise  # Authentication, bucket absence and network errors are not missing tasks.

    @staticmethod
    def _body(result, limit):
        try:
            size = result.content_length
            if type(size) is not int or size < 0 or size > limit:
                raise TooLarge("OSS response length is missing or exceeds limit")
            data = result.body.read(limit + 1)
            if len(data) != size:
                raise Corrupt("OSS response length mismatch")
            return data
        finally:
            if result.body is not None:
                result.body.close()

    def get(self, key, max_bytes=1048576):
        self.metrics.add(reads=1)
        result = self._call("get_object", self.sdk.GetObjectRequest(bucket=self.bucket, key=key))
        data = self._body(result, max_bytes)
        self.metrics.add(downloaded_bytes=len(data))
        return Object(data, result.etag)

    def put(self, key, data, expected_generation):
        if expected_generation != 0:
            raise Invalid("OSS normal objects only support create; publish heads through HeadStore")
        self.metrics.add(writes=1)
        result = self._call("put_object", self.sdk.PutObjectRequest(
            bucket=self.bucket, key=key, body=data, content_length=len(data),
            content_type="application/octet-stream", forbid_overwrite=True))
        self.metrics.add(uploaded_bytes=len(data))
        return result.etag

    def check(self):
        sdk = self.sdk
        result = self._call("get_bucket_versioning", sdk.GetBucketVersioningRequest(bucket=self.bucket))
        if result.version_status not in (None, ""):
            raise Invalid("OSS backend requires a bucket with versioning never enabled")
        for method, request_type, absent_code in (
            ("get_bucket_worm", sdk.GetBucketWormRequest, "NoSuchWORMConfiguration"),
            ("get_bucket_object_worm_configuration", sdk.GetBucketObjectWormConfigurationRequest,
             "NoSuchObjectWormConfiguration"),
        ):
            try:
                self._call(method, request_type(bucket=self.bucket))
            except sdk.exceptions.ServiceError as error:
                if error.status_code != 404 or error.code != absent_code:
                    raise
            else:
                raise Invalid("OSS backend requires retention policies disabled")
        return dict(provider="oss", bucket=self.bucket, region=self.region,
                    note="Bucket configuration checked; live append/put smoke test still required")


class OSSHeads:
    def __init__(self, store, max_log_bytes=MAX_LOG):
        if type(max_log_bytes) is not int or not BLOCK <= max_log_bytes <= MAX_LOG or max_log_bytes % BLOCK:
            raise Invalid("OSS log cap must be whole records, from 8 KiB to 4 GiB")
        self.store, self.max_log_bytes = store, max_log_bytes

    @staticmethod
    def physical_key(key):
        return key + ".append-v1"

    def get(self, key, max_bytes=4096):
        store, sdk = self.store, self.store.sdk
        physical = self.physical_key(key)
        store.metrics.add(reads=1)
        meta = store._call("head_object", sdk.HeadObjectRequest(bucket=store.bucket, key=physical))
        size = meta.content_length
        if meta.object_type != "Appendable" or type(size) is not int or size <= 0 or size % BLOCK:
            raise Corrupt("OSS head type/length violates append protocol")
        if size > self.max_log_bytes:
            raise TooLarge("Existing OSS head log exceeds configured cap")
        start = size - BLOCK
        result = store._call("get_object", sdk.GetObjectRequest(
            bucket=store.bucket, key=physical, range_header=f"bytes={start}-{size-1}",
            range_behavior="standard"))
        try:
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", result.content_range or "")
            if result.status_code != 206 or not match or (
                    int(match[1]), int(match[2])) != (start, size - 1) or int(match[3]) < size:
                raise Corrupt("OSS did not return the requested bounded range")
        except Exception:
            if result.body is not None:
                result.body.close()
            raise
        raw = store._body(result, BLOCK)
        store.metrics.add(downloaded_bytes=len(raw))
        return Object(unframe(raw, max_bytes), f"oss-append-v1:{size}")

    def publish(self, key, data, token):
        if token == 0:
            position = 0
        elif isinstance(token, str) and re.fullmatch(r"oss-append-v1:[1-9][0-9]*", token):
            position = int(token.split(":")[1])
        else:
            raise Invalid("Invalid OSS head token")
        if position % BLOCK:
            raise Invalid("Misaligned OSS head token")
        if position + BLOCK > self.max_log_bytes:
            raise TooLarge("OSS task publication cap reached; create a linked successor task")
        raw = frame(data)
        store = self.store
        store.metrics.add(writes=1)
        result = store._call("append_object", store.sdk.AppendObjectRequest(
            bucket=store.bucket, key=self.physical_key(key), position=position,
            content_type="application/octet-stream", content_length=BLOCK, body=raw))
        if result.next_position != position + BLOCK:
            raise Corrupt("Unexpected OSS append receipt; publication outcome requires verification")
        store.metrics.add(uploaded_bytes=BLOCK)
        return f"oss-append-v1:{position + BLOCK}"
