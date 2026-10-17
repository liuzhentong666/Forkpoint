from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from agent_state.oss import BLOCK, OSSStore
from agent_state.storage import Conflict, Corrupt, Invalid, Missing, TooLarge
from oss_fake import FakeOSS, error


class OSSProtocolTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeOSS()
        self.store = OSSStore("test-bucket", "cn-hangzhou", client=self.client)
        self.heads = self.store.heads
        self.key = "prefix/tasks/task/head.json"

    def test_create_and_update_competitions_have_one_winner(self):
        for token in (0, f"oss-append-v1:{BLOCK}"):
            barrier = threading.Barrier(10)
            def publish(n):
                barrier.wait(timeout=5)
                try:
                    self.heads.publish(self.key, str(n).encode(), token)
                    return True
                except Conflict:
                    return False
            with ThreadPoolExecutor(max_workers=10) as pool:
                self.assertEqual(sum(pool.map(publish, range(10))), 1)
        self.assertEqual(self.heads.get(self.key).generation, f"oss-append-v1:{2*BLOCK}")

    def test_tail_read_is_bounded_after_1001_records(self):
        token = 0
        for n in range(1001):
            token = self.heads.publish(self.key, str(n).encode(), token)
        before = self.store.metrics.snapshot()
        self.assertEqual(self.heads.get(self.key).data, b"1000")
        after = self.store.metrics.snapshot()
        self.assertEqual(after["reads"]-before["reads"], 1)
        self.assertEqual(after["sdk_calls"]-before["sdk_calls"], 2)
        self.assertEqual(after["downloaded_bytes"]-before["downloaded_bytes"], BLOCK)
        self.assertTrue(self.client.last_body.closed)

    def test_append_between_metadata_and_range_keeps_valid_old_record(self):
        token = self.heads.publish(self.key, b"old", 0)
        self.client.after_head = lambda: self.heads.publish(self.key, b"new", token)
        old = self.heads.get(self.key)
        self.assertEqual(old.data, b"old")
        self.assertEqual(old.generation, token)
        with self.assertRaises(Conflict):
            self.heads.publish(self.key, b"stale", old.generation)
        self.assertEqual(self.heads.get(self.key).data, b"new")

    def test_cap_refuses_write_without_replacing_existing_log(self):
        self.heads.max_log_bytes = 2 * BLOCK
        token = self.heads.publish(self.key, b"one", 0)
        token = self.heads.publish(self.key, b"two", token)
        with self.assertRaises(TooLarge):
            self.heads.publish(self.key, b"three", token)
        self.assertEqual(self.heads.get(self.key).data, b"two")

    def test_damage_misalignment_and_nonappendable_fail_closed(self):
        self.heads.publish(self.key, b"one", 0)
        entry = self.client.entries[self.heads.physical_key(self.key)]
        entry["data"][44] ^= 1
        with self.assertRaises(Corrupt):
            self.heads.get(self.key)
        entry["data"].append(0)
        with self.assertRaises(Corrupt):
            self.heads.get(self.key)
        entry["data"] = entry["data"][:-1]
        entry["kind"] = "Normal"
        with self.assertRaises(Corrupt):
            self.heads.get(self.key)

    def test_full_download_instead_of_range_is_rejected_and_closed(self):
        self.heads.publish(self.key, b"one", 0)
        original = self.client.get_object
        def wrong(request):
            result = original(request)
            result.status_code = 200
            return result
        self.client.get_object = wrong
        with self.assertRaises(Corrupt):
            self.heads.get(self.key)
        self.assertTrue(self.client.last_body.closed)

    def test_bucket_versioning_retention_and_permission_checks(self):
        self.assertEqual(self.store.check()["provider"], "oss")
        for version in ("Enabled", "Suspended"):
            self.client.version_status = version
            with self.assertRaises(Invalid):
                self.store.check()
        self.client.version_status = None
        for field in ("bucket_worm", "object_worm"):
            setattr(self.client, field, True)
            with self.assertRaises(Invalid):
                self.store.check()
            setattr(self.client, field, False)
        def denied(request):
            raise error("AccessDenied", 403)
        self.client.get_bucket_worm = denied
        with self.assertRaises(self.store.sdk.exceptions.ServiceError):
            self.store.check()

    def test_objects_create_only_bounded_reads_and_missing(self):
        self.store.put("object", b"abc", 0)
        with self.assertRaises(Conflict):
            self.store.put("object", b"xyz", 0)
        with self.assertRaises(Invalid):
            self.store.put("object", b"xyz", "etag")
        with self.assertRaises(TooLarge):
            self.store.get("object", max_bytes=2)
        self.assertTrue(self.client.last_body.closed)
        self.assertEqual(self.store.get("object").data, b"abc")
        with self.assertRaises(Missing):
            self.heads.get("absent")

    def test_before_and_after_response_failures_have_distinct_durable_states(self):
        self.client.fail = "before"
        with self.assertRaises(TimeoutError):
            self.heads.publish(self.key, b"one", 0)
        with self.assertRaises(Missing):
            self.heads.get(self.key)
        self.client.fail = "after"
        with self.assertRaises(TimeoutError):
            self.heads.publish(self.key, b"one", 0)
        self.assertEqual(self.heads.get(self.key).data, b"one")
        with self.assertRaises(Conflict):
            self.heads.publish(self.key, b"one", 0)


if __name__ == "__main__":
    unittest.main()
