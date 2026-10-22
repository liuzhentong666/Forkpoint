"""agent_lab.py merge --reads against a scripted worker: the checks, the SQL sent, the RS001 report.

The recorded samples in agent_lab_golden.json cover the same cases on both the local and the
gateway path; these tests state what the samples should show.
"""
import json
import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(Path(__file__).resolve().parent)]

import agent_lab  # noqa: E402
from agent_lab_fake import DB, NEW, FakeWorker, last_line, run_cli  # noqa: E402

MERGE = ["merge", "--database", DB, "--revision", NEW, "--actor", "gpt-a"]
ROW = {"id": 101, "sku": "sku-101", "name": "product-101", "price": 10.0}


class ReadSetMergeTests(unittest.TestCase):
    def lab(self, *argv, **options):
        worker = FakeWorker(**options)
        with patch.dict(os.environ):
            os.environ.pop("AGENTDB_LAB_GATEWAY", None)
            return run_cli(agent_lab, list(argv), worker)

    def test_read_set_takes_the_place_of_the_expected_head(self):
        reads = {"app.products": [{"id": 101}, {"id": "4000"}], "app.orders": []}
        result = self.lab(*MERGE, "--reads", json.dumps(reads))
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(json.loads(result["stdout"]), dict(merged=True, main=NEW))
        [statement] = result["statements"]
        self.assertIn(f"vc.merge_validated('main','{NEW}',", statement)
        self.assertNotIn("vc.merge(", statement)
        self.assertIn("SET LOCAL lock_timeout = '10s';", statement)
        # Sent as given, by table name; the engine maps names to ids and rewrites "4000" to 4000.
        sent = re.search(r"decode\('([0-9a-f]+)','hex'\)", statement).group(1)
        self.assertEqual(json.loads(bytes.fromhex(sent)), reads)
        self.assertIn("vc.table_id(r.key::regclass)", statement)

    def test_read_conflict_reports_each_changed_row_by_table_name(self):
        result = self.lab(*MERGE, "--reads", '{"app.products": [{"id": 101}]}', merge="read_conflict")
        self.assertEqual(result["exit_code"], 2)
        output = json.loads(result["stdout"])
        self.assertEqual((output["error"], output["sqlstate"]), ("read_conflict", "RS001"))
        self.assertEqual(output["changed"], [dict(table="app.products", key={"id": 101}, base=dict(ROW, stock=100),
                                                  now=dict(ROW, stock=95), written=True)])
        self.assertTrue(output["detail"].startswith("ERROR:  RS001: Read conflict"))
        # One script: the table names it prints before BEGIN map the ids back.
        [statement] = result["statements"]
        self.assertIn("SELECT 'tables',json_build_object(vc.table_id('app.products')", statement)

    def test_read_conflict_without_a_readable_detail_is_still_a_read_conflict(self):
        result = self.lab(*MERGE, "--reads", "{}", merge="unreadable_detail")
        output = json.loads(result["stdout"])
        self.assertEqual((result["exit_code"], output["error"], output["changed"]), (2, "read_conflict", None))
        self.assertEqual(len(result["statements"]), 1)

    def test_malformed_read_sets_are_argument_errors_before_any_sql(self):
        cases = {
            "not JSON": '{"app.products": [',
            "NaN": '{"app.products": [{"id": NaN}]}',
            "an array": '[{"id": 101}]',
            "an unqualified table": '{"products": [{"id": 101}]}',
            "keys not in an array": '{"app.products": {"id": 101}}',
            "a key that is not an object": '{"app.products": [101]}',
            "an empty key": '{"app.products": [{}]}',
            "a repeated table": '{"app.products": [{"id": 101}], "app.products": [{"id": 102}]}',
            "a repeated column": '{"app.products": [{"id": 101, "id": 102}]}',
            "deep nesting": "[" * 5000 + "]" * 5000,
            "more than 16384 bytes": json.dumps({"app.products": [{"id": n} for n in range(2000)]}),
        }
        for label, value in cases.items():
            with self.subTest(label):
                result = self.lab(*MERGE, "--reads", value)
                self.assertEqual((result["exit_code"], result["statements"], result["stdout"]), (2, [], ""))
                self.assertNotIn("Traceback", result["stderr"])
                self.assertTrue(last_line(result["stderr"]).startswith("agent_lab.py: error: --reads "), result)

    def test_read_set_text_is_sent_as_given(self):
        # Re-serialized, 1e400 would become Infinity (not JSON) and 12345678901234567890.5 would
        # lose digits; as given, the engine reads each number exactly and judges it as a key.
        value = '{"app.products": [{"id": 1e400}, {"id": 12345678901234567890.5}]}'
        result = self.lab(*MERGE, "--reads", value)
        self.assertEqual(result["exit_code"], 0, result)
        sent = re.search(r"decode\('([0-9a-f]+)','hex'\)", result["statements"][0]).group(1)
        self.assertEqual(bytes.fromhex(sent).decode(), value)

    def test_an_unknown_revision_is_not_found(self):
        result = self.lab(*MERGE, "--reads", "{}", merge="not_found")
        output = json.loads(result["stdout"])
        self.assertEqual((result["exit_code"], output["error"], output["sqlstate"]), (2, "not_found", "P0002"))

    def test_merge_without_expected_or_reads_is_refused_as_before(self):
        result = self.lab(*MERGE)
        self.assertEqual((result["exit_code"], result["statements"]), (2, []))
        self.assertEqual(last_line(result["stderr"]), "agent_lab.py: error: merge requires --expected")


if __name__ == "__main__":
    unittest.main()
