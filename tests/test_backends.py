from concurrent.futures import ThreadPoolExecutor
import io
import json
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch
from agent_state.config import load_settings, create_store
from agent_state.engine import Engine
from agent_state.oss import OSSStore, BLOCK
from agent_state.runtime import Runtime
from agent_state.s3 import S3Store
from agent_state.storage import Object, Conflict, Corrupt, Invalid, Missing, TooLarge
from oss_fake import FakeOSS
from smoke_cloud import exercise


class BackendWorkflowTests(unittest.TestCase):
    def test_full_oss_workflow_concurrency_interruptions_and_old_retry(self):
        store = OSSStore("test-bucket", "cn-hangzhou", client=FakeOSS())
        result = exercise(store, "contract-test")
        self.assertTrue(result["old_duplicate_verified"])
        self.assertTrue(result["interruption_recovery_verified"])
        self.assertEqual(result["cold_logical_reads"], 2)

    def test_oss_old_command_after_200_versions_and_capacity_failure(self):
        store = OSSStore("test-bucket", "cn-hangzhou", client=FakeOSS())
        engine = Engine(store)
        value = dict(goal="Verify old commands")
        engine.publish("task", "initial", "agent", value)
        for n in range(1, 201):
            engine.publish("task", f"step-{n}", "agent", dict(value, facts={"n": n}), n-1)
        self.assertTrue(Engine(store).publish("task", "initial", "agent", value)["deduplicated"])
        with self.assertRaises(Conflict):
            engine.publish("task", "initial", "agent", dict(goal="changed"))
        store.heads.max_log_bytes = 201 * BLOCK
        with self.assertRaises(TooLarge):
            engine.publish("task", "over-cap", "agent", value, 200)
        self.assertEqual(Engine(store).read("task")["version"], 200)

    def test_slow_read_never_displaces_newer_state_with_opaque_tokens(self):
        entered, release = threading.Event(), threading.Event()
        class EngineDouble:
            calls = 0
            def read(self, task):
                self.calls += 1
                if self.calls == 1:
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError()
                    return dict(version=1, generation='"z-old"')
                return dict(version=2, generation='"a-new"')
        runtime = Runtime(EngineDouble())
        with ThreadPoolExecutor(max_workers=1) as pool:
            old = pool.submit(runtime.read, "task")
            self.assertTrue(entered.wait(5))
            self.assertEqual(runtime.read("task", fresh=True)["version"], 2)
            release.set()
            self.assertEqual(old.result(5)["version"], 2)
        self.assertEqual(runtime.read("task")["generation"], '"a-new"')

    def test_normalized_notification_checks_scope_and_invalidates(self):
        store = OSSStore("test-bucket", "cn-hangzhou", client=FakeOSS())
        engine = Engine(store)
        engine.publish("task", "create", "agent", dict(goal="Check cache"))
        runtime = Runtime(engine)
        runtime.read("task")
        event = dict(kind="changed", task_id="task", bucket="test-bucket", prefix="agentdb-state")
        self.assertFalse(runtime.notify_event(event, bucket="other"))
        self.assertTrue(runtime.notify_event(event, bucket="test-bucket"))
        self.assertEqual(runtime.metrics()["cache"]["tasks"], 0)
        self.assertFalse(runtime.notify_event(event, bucket="test-bucket"))


class S3SDKTests(unittest.TestCase):
    def setUp(self):
        import boto3
        from botocore.stub import Stubber
        self.client = boto3.client("s3", region_name="cn-north-1",
                                    aws_access_key_id="test", aws_secret_access_key="test")
        self.stub = Stubber(self.client)
        self.stub.activate()
        self.store = S3Store("test-bucket", client=self.client)

    def tearDown(self):
        self.stub.assert_no_pending_responses()
        self.stub.deactivate()
        self.client.close()

    def test_official_sdk_serializes_create_and_conditional_update(self):
        base = dict(Bucket="test-bucket", Key="head", Body=b"value", ContentLength=5,
                    ContentType="application/octet-stream")
        self.stub.add_response("put_object", {"ETag": '"opaque-z"'}, dict(base, IfNoneMatch="*"))
        token = self.store.put("head", b"value", 0)
        self.assertEqual(token, '"opaque-z"')
        self.stub.add_response("put_object", {"ETag": '"opaque-a"'}, dict(base, IfMatch=token))
        self.assertEqual(self.store.put("head", b"value", token), '"opaque-a"')
        self.stub.add_client_error("put_object", service_error_code="PreconditionFailed", http_status_code=412,
                                   expected_params=dict(base, IfMatch=token))
        with self.assertRaises(Conflict):
            self.store.put("head", b"value", token)

    def test_read_size_stream_closure_and_error_mapping(self):
        body = io.BytesIO(b"value")
        self.stub.add_response("get_object", dict(Body=body, ContentLength=5, ETag='"opaque"'),
                               dict(Bucket="test-bucket", Key="object"))
        with self.assertRaises(TooLarge):
            self.store.get("object", max_bytes=2)
        self.assertTrue(body.closed)
        body = io.BytesIO(b"value")
        self.stub.add_response("get_object", dict(Body=body, ContentLength=5, ETag='"opaque"'),
                               dict(Bucket="test-bucket", Key="object"))
        self.assertEqual(self.store.get("object"), Object(b"value", '"opaque"'))
        self.assertTrue(body.closed)
        self.stub.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
        with self.assertRaises(Missing):
            self.store.get("absent")
        self.stub.add_client_error("get_object", service_error_code="AccessDenied", http_status_code=403)
        with self.assertRaises(self.store.error_type):
            self.store.get("forbidden")


class OSSSDKWireTests(unittest.TestCase):
    def test_actual_sdk_constructs_conditional_and_range_headers(self):
        import alibabacloud_oss_v2 as oss
        calls = []
        class CaptureTransport:
            def open(self):
                pass
            def close(self):
                pass
            def send(self, request, **kwargs):
                calls.append((request.method, request.url, dict(request.headers)))
                raise ConnectionError("Offline transport: request captured, no network")
        config = oss.Config(region="cn-hangzhou", retry_max_attempts=1, http_client=CaptureTransport(),
                            credentials_provider=oss.credentials.StaticCredentialsProvider("test", "test"))
        client = oss.Client(config)
        store = OSSStore("test-bucket", "cn-hangzhou", client=client)
        try:
            with self.assertRaises(Exception):
                store.put("object", b"abc", 0)
            self.assertEqual(len(calls), 1)
            headers = {k.lower(): v for k, v in calls[-1][2].items()}
            self.assertEqual(headers["x-oss-forbid-overwrite"], "true")
            with self.assertRaises(Exception):
                store.heads.publish("task", b"head", 0)
            self.assertEqual(len(calls), 2)
            self.assertIn("position=0", calls[-1][1])
            self.assertIn("append", calls[-1][1])
            with self.assertRaises(Exception):
                client.get_object(oss.GetObjectRequest(bucket="test-bucket", key="task",
                                                       range_header="bytes=0-8191", range_behavior="standard"))
            self.assertEqual(len(calls), 3)
            headers = {k.lower(): v for k, v in calls[-1][2].items()}
            self.assertEqual(headers["range"], "bytes=0-8191")
            self.assertEqual(headers["x-oss-range-behavior"], "standard")
        finally:
            config.http_client.close()


class ConfigurationTests(unittest.TestCase):
    def test_old_config_defaults_to_gcs_and_invalid_combinations_fail(self):
        self.assertEqual(load_settings(bucket="test")["provider"], "gcs")
        with self.assertRaises(Invalid):
            load_settings(provider="oss", bucket="test")
        with self.assertRaises(Invalid):
            load_settings(provider="oss", bucket="test", region="cn-hangzhou", subscription="google-sub")
        with self.assertRaises(Invalid):
            load_settings(provider="memory", bucket="test")
        settings = load_settings(provider="oss", bucket="test-bucket", region="cn-hangzhou")
        with patch("agent_state.oss.OSSStore") as constructor:
            create_store(settings)
            constructor.assert_called_once_with("test-bucket", "cn-hangzhou", None)

    def test_cli_help_and_core_imports_do_not_require_provider_sdks(self):
        code = '''
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'google', 'boto3', 'botocore', 'alibabacloud_oss_v2'}:
            raise ImportError('SDK intentionally unavailable')
sys.meta_path.insert(0, Block())
from agent_state.server import main
sys.argv = ['server', '--help']
main()
'''
        result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--provider", result.stdout)


if __name__ == "__main__":
    unittest.main()
