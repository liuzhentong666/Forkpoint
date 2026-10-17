from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from agent_state.client import StateClient
from agent_state.config import load_settings
from agent_state.engine import Engine
from agent_state.maintenance import SnapshotMaintenance
from agent_state.oss import OSSStore, credentials_provider
from agent_state.runtime import Runtime
from agent_state.server import make_server
from agent_state.storage import Conflict, Invalid, MemoryStore, Missing, StateError, TooLarge
from deploy.build_ecs_bundle import build, FILES
from deploy.ecs.verify_bundle import verify

ROOT = Path(__file__).resolve().parents[1]


class CredentialsTests(unittest.TestCase):
    def test_role_provider_fetches_current_credential_for_each_signature(self):
        import alibabacloud_oss_v2 as sdk
        with patch.dict(os.environ, {"AGENTDB_OSS_CREDENTIALS": "ecs_ram_role",
                                     "ALIBABA_CLOUD_ECS_METADATA": "test-role"}), \
                patch("alibabacloud_credentials.client.Client") as factory:
            credentials = []
            for number in (1, 2):
                value = Mock()
                value.get_access_key_id.return_value = f"test-id-{number}"
                value.get_access_key_secret.return_value = f"test-secret-{number}"
                value.get_security_token.return_value = f"test-token-{number}"
                credentials.append(value)
            factory.return_value.get_credential.side_effect = credentials
            provider = credentials_provider(sdk)
            config = factory.call_args.args[0]
            self.assertEqual(config.type, "ecs_ram_role")
            self.assertEqual(config.role_name, "test-role")
            self.assertTrue(config.disable_imds_v1)
            first, second = provider.get_credentials(), provider.get_credentials()
            self.assertEqual((first.access_key_id, first.security_token), ("test-id-1", "test-token-1"))
            self.assertEqual((second.access_key_id, second.security_token), ("test-id-2", "test-token-2"))

    def test_existing_environment_credentials_stay_default_and_typo_fails(self):
        import alibabacloud_oss_v2 as sdk
        with patch.dict(os.environ, {"OSS_ACCESS_KEY_ID": "test-only-id",
                                     "OSS_ACCESS_KEY_SECRET": "test-only-secret"}, clear=True):
            self.assertIsInstance(credentials_provider(sdk), sdk.credentials.EnvironmentVariableCredentialsProvider)
        with patch.dict(os.environ, {"AGENTDB_OSS_CREDENTIALS": "ecs_typo"}):
            with self.assertRaises(Invalid):
                credentials_provider(sdk)

    def test_ecs_configuration_and_sdk_wiring(self):
        import alibabacloud_oss_v2 as sdk
        settings = load_settings(ROOT / "deploy/ecs/cloud.ecs.json")
        with patch.dict(os.environ, {"AGENTDB_OSS_CREDENTIALS": "ecs_ram_role"}), \
                patch("alibabacloud_credentials.client.Client"), patch.object(sdk, "Client") as factory:
            OSSStore(settings["bucket"], settings["region"], settings["endpoint"])
            config = factory.call_args.args[0]
            self.assertEqual(config.endpoint, "https://oss-cn-hangzhou-internal.aliyuncs.com")
            self.assertIsInstance(config.credentials_provider, sdk.credentials.CredentialsProviderFunc)
            self.assertEqual(settings["host"], "127.0.0.1")


class HTTPClientTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.runtime = Runtime(Engine(self.store))
        self.server = make_server(self.runtime, port=0, token="test-only-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.client = StateClient(self.url, token="test-only-token")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def test_http_concurrent_publication_old_retry_fresh_and_error_mapping(self):
        state = dict(goal="Use remote state with local database")
        with self.assertRaises(Missing):
            self.client.read("task", fresh=True)
        self.client.publish("task", command_id="create", actor="agent", state=state)
        self.client.read("task")  # Prime server cache.
        def update(number):
            try:
                return self.client.publish("task", command_id=f"write-{number}", actor="agent",
                                           state=dict(state, facts={"winner": number}), expected_version=0)
            except Conflict:
                return None
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(update, range(10)))
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertEqual(self.client.read("task", fresh=True)["version"], 1)
        old = self.client.publish("task", command_id="create", actor="agent", state=state)
        self.assertTrue(old["deduplicated"])
        with self.assertRaises(Conflict):
            self.client.publish("task", command_id="create", actor="agent", state=dict(goal="Changed"))
        with self.assertRaises(Invalid):
            self.client.publish("bad", command_id="bad", actor="agent", state={})
        with self.assertRaises(StateError) as error:
            StateClient(self.url, token="wrong").read("task")
        self.assertNotIsInstance(error.exception, Missing)

    def test_postgres_executor_handoff_after_commit_over_http(self):
        class DatabaseDouble:
            materializations = 0
            def sql(self, query):
                if "SELECT vc.materialize(" in query:
                    self.materializations += 1
                    return ""
                return json.dumps(dict(revision_exists=True, table_exists=True,
                                       snapshot_exists=self.materializations > 0))
        database = DatabaseDouble()
        revision, table_id = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
        def crash():
            raise InterruptedError("after database commit")
        runner = SnapshotMaintenance(self.client, database)
        with self.assertRaises(InterruptedError):
            runner.run("snapshot", "agent-one", revision, table_id, after_apply=crash)
        self.assertEqual(self.client.read("snapshot", fresh=True)["state"]["status"], "running")
        # A fresh client has no persisted state and discovers the committed result.
        resumed = SnapshotMaintenance(StateClient(self.url, token="test-only-token"), database)
        result = resumed.run("snapshot", "agent-two", revision, table_id)
        self.assertTrue(result["verified"])
        self.assertFalse(result["applied"])
        self.assertEqual(database.materializations, 1)
        self.assertEqual(self.client.read("snapshot", fresh=True)["state"]["status"], "completed")

    def test_post_publication_response_loss_does_not_retry_implicitly(self):
        original = self.client.opener.open
        def drop_response(request, **kwargs):
            with original(request, **kwargs):
                pass
            raise TimeoutError("response lost")
        command = dict(command_id="lost", actor="agent", state=dict(goal="Retry identical command"))
        with patch.object(self.client.opener, "open", side_effect=drop_response) as send:
            with self.assertRaises(StateError):
                self.client.publish("lost", **command)
            self.assertEqual(send.call_count, 1)
        self.assertTrue(self.client.publish("lost", **command)["deduplicated"])
        self.assertEqual(self.client.read("lost", fresh=True)["version"], 0)

    def test_restart_runtime_recovers_same_published_state(self):
        self.client.publish("restart", command_id="create", actor="agent", state=dict(goal="Survive process cache loss"))
        recovered = Runtime(Engine(self.store)).read("restart", fresh=True)
        self.assertEqual(recovered["state"], self.client.read("restart", fresh=True)["state"])


class ClientBoundaryTests(unittest.TestCase):
    def test_redirect_not_followed_and_response_bounded(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                requests.append(self.path)
                if self.path.endswith("redirect"):
                    self.send_response(302)
                    self.send_header("Location", "/should-not-receive-token")
                    data = b'{"error":"redirect"}'
                else:
                    self.send_response(200)
                    data = b" " * 262145
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = StateClient(f"http://127.0.0.1:{server.server_address[1]}", token="test")
            with self.assertRaises(StateError):
                client.read("redirect")
            self.assertEqual(requests, ["/v1/tasks/redirect"])
            with self.assertRaises(TooLarge):
                client.read("large")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)

    def test_remote_plaintext_and_embedded_credentials_rejected(self):
        for url in ("http://example.com", "https://u:p@example.com", "https://example.com/path", "https://example.com?token=x"):
            with self.subTest(url=url), self.assertRaises(Invalid):
                StateClient(url)
        StateClient("https://example.com")
        StateClient("http://127.0.0.1:18844")


class BundleTests(unittest.TestCase):
    def test_bundle_allowlist_checksums_lf_and_determinism(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            first, second = temp / "first.tar.gz", temp / "second.tar.gz"
            result = build(ROOT, first)
            self.assertEqual(result["sha256"], build(ROOT, second)["sha256"])
            with tarfile.open(first) as archive:
                names = archive.getnames()
                self.assertEqual(set(names), {"agentdb-state/" + name for name in FILES + ["MANIFEST.json"]})
                self.assertFalse(any("connection.local" in name or ".venv" in name for name in names))
                # Materialize known test payload without unrestricted extractall.
                for name in names:
                    target = temp / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    data = archive.extractfile(name).read()
                    target.write_bytes(data)
                    self.assertNotIn(b"\r", data)
            extracted = temp / "agentdb-state"
            self.assertEqual(verify(extracted), len(FILES))
            (extracted / "agent_state/server.py").write_text("tampered", encoding="utf-8")
            with self.assertRaises(ValueError):
                verify(extracted)

    def test_manifest_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "MANIFEST.json").write_text(json.dumps({"format": 1, "files": {"../outside": "hash"}}))
            with self.assertRaises(ValueError):
                verify(root)


if __name__ == "__main__":
    unittest.main()
