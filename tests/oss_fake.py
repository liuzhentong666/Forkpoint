"""In-memory OSS service model, using the installed official SDK models."""
import hashlib
import io
import re
import threading
import alibabacloud_oss_v2 as sdk


def error(code, status=409):
    return sdk.exceptions.ServiceError(code=code, status_code=status, message="injected",
                                       request_id="test", ec="test", timestamp="test", request_target="test")


class FakeOSS:
    def __init__(self):
        self.entries = {}
        self.lock = threading.RLock()
        self.calls = []
        self.fail = None
        self.version_status = None
        self.bucket_worm = self.object_worm = False
        self.after_head = None
        self.last_body = None

    def put_object(self, request):
        assert isinstance(request, sdk.PutObjectRequest)
        assert request.forbid_overwrite is True
        assert request.content_length == len(request.body)
        with self.lock:
            self.calls.append(("put", request.key))
            if request.key in self.entries:
                raise error("FileAlreadyExists")
            data = bytes(request.body)
            self.entries[request.key] = dict(data=bytearray(data), kind="Normal")
            return sdk.PutObjectResult(etag=hashlib.md5(data).hexdigest(), status_code=200)

    def append_object(self, request):
        assert isinstance(request, sdk.AppendObjectRequest)
        assert request.content_length == len(request.body)
        with self.lock:
            self.calls.append(("append", request.key, request.position))
            if self.fail == "before":
                self.fail = None
                raise TimeoutError("before append")
            entry = self.entries.get(request.key)
            if entry and entry["kind"] != "Appendable":
                raise error("ObjectNotAppendable")
            size = len(entry["data"]) if entry else 0
            if request.position != size:
                raise error("PositionNotEqualToLength")
            if entry is None:
                entry = self.entries[request.key] = dict(data=bytearray(), kind="Appendable")
            entry["data"].extend(request.body)
            if self.fail == "after":
                self.fail = None
                raise TimeoutError("lost append response")
            return sdk.AppendObjectResult(next_position=len(entry["data"]), status_code=200)

    def head_object(self, request):
        with self.lock:
            self.calls.append(("head", request.key))
            if request.key not in self.entries:
                raise error("NoSuchKey", 404)
            entry = self.entries[request.key]
            result = sdk.HeadObjectResult(content_length=len(entry["data"]), object_type=entry["kind"], status_code=200)
        if self.after_head:
            callback, self.after_head = self.after_head, None
            callback()
        return result

    def get_object(self, request):
        assert isinstance(request, sdk.GetObjectRequest)
        with self.lock:
            self.calls.append(("get", request.key, request.range_header))
            if request.key not in self.entries:
                raise error("NoSuchKey", 404)
            entry = self.entries[request.key]
            data = entry["data"]
            size = len(data)
            content_range, status = None, 200
            if request.range_header:
                assert request.range_behavior == "standard"
                match = re.fullmatch(r"bytes=(\d+)-(\d+)", request.range_header)
                start, end = map(int, match.groups())
                data = data[start:end+1]
                content_range, status = f"bytes {start}-{end}/{size}", 206
            raw = bytes(data)
            self.last_body = io.BytesIO(raw)
            result = sdk.GetObjectResult(body=self.last_body, content_length=len(raw),
                                        content_range=content_range, etag=hashlib.md5(raw).hexdigest())
            result.status_code = status
            return result

    def get_bucket_versioning(self, request):
        return sdk.GetBucketVersioningResult(version_status=self.version_status)

    def get_bucket_worm(self, request):
        if self.bucket_worm:
            return sdk.GetBucketWormResult(worm_configuration=sdk.WormConfiguration(state="Locked"))
        raise error("NoSuchWORMConfiguration", 404)

    def get_bucket_object_worm_configuration(self, request):
        if self.object_worm:
            return sdk.GetBucketObjectWormConfigurationResult(
                object_worm_configuration=sdk.ObjectWormConfiguration(object_worm_enabled="Enabled"))
        raise error("NoSuchObjectWormConfiguration", 404)
