"""Tests for long_task.py's parts that need no database: token fit, layout and evaluation.

  python -m unittest discover -s experiments/context_archive -v
"""
import json
import unittest

import archive
import long_task

REVISIONS = [f"{i:08x}-0000-4000-8000-000000000000" for i in range(1, 9)]
OK = {"exit_code": 0, "stdout": "", "stderr": "", "result": {"product": {"id": 101, "stock": 100}, "pad": "p" * 2000}}


def calls(audits=6):
    return ([("open", "info", {}, OK)] +
            [("audit", "info", {"revision": r}, OK) for r in REVISIONS[:audits]] +
            [("close", "fork", {"branch": "gpt-b-audit-6", "base": REVISIONS[0]}, OK),
             ("close", "propose", {"branch": "gpt-b-audit-6", "expected": REVISIONS[0], "field": "stock",
                                   "value": "95", "code": "STOCK = 95\n"}, {"exit_code": 0, "result": {"revision": REVISIONS[1]}})])


class FitTests(unittest.TestCase):
    def test_recovers_an_exact_line(self):
        result = long_task.fit([(c, 2500 + 0.5 * c) for c in range(0, 20000, 1000)])
        self.assertEqual((result["intercept"], result["tokens_per_byte"], result["rmse"], result["r2"]), (2500, 0.5, 0, 1))


class LayoutTests(unittest.TestCase):
    def test_batches_only_audit_reads(self):
        groups = long_task.responses_for(calls(6), "batched")
        self.assertEqual([[c[0] for c in g] for g in groups],
                         [["open"], ["audit"] * 4, ["audit"] * 2, ["close"], ["close"]])
        self.assertEqual(len(long_task.responses_for(calls(6), "sequential")), 9)

    def test_lines_carry_one_block_each_and_every_response_has_an_estimate(self):
        prompt_line, lines = long_task.layout("task", calls(6), "report", "batched", lambda size: 100 + size, False)
        events = [json.loads(line) for line in lines]
        self.assertTrue(all(len(e["message"]["content"]) == 1 for e in events))
        self.assertTrue(all(isinstance(archive.request_tokens(e), int) for e in events if e["type"] == "assistant"))
        kinds = [e["message"]["content"][0]["type"] for e in events]
        self.assertEqual(kinds.count("tool_use"), 9)
        self.assertEqual(kinds.count("tool_result"), 9)
        self.assertEqual(kinds[-1], "text")
        # thinking, the opening call and its result, then four audit calls in one response and their results
        self.assertEqual(kinds[:11], ["thinking", "tool_use", "tool_result"] + ["tool_use"] * 4 + ["tool_result"] * 4)

    def test_switch_lands_where_the_estimate_crosses_the_limit(self):
        sizes = []
        long_task.layout("task", calls(6), "report", "sequential", lambda size: sizes.append(size) or 0, False)
        threshold = sizes[6]  # response 6 is the sixth audit read
        prompt_line, lines = long_task.layout("task", calls(6), "report", "sequential",
                                              lambda size: 20000 if size >= threshold else 0, False)
        result = long_task.evaluate(prompt_line, lines, calls(6), REVISIONS, 6)
        self.assertEqual((result["switch"]["after_action"], result["audited_before_switch"]), ("info", 6))
        self.assertEqual(result["revisions_unread"], 0)
        self.assertEqual(result["audit_rule"], "absent")  # this prompt has no rule, so the check says so
        self.assertFalse(result["passed"])
        self.assertIn("audit rule is absent", result["reasons"])
        self.assertGreaterEqual(result["audited_values_fetch_only"], 3)

    def test_no_crossing_means_no_switch(self):
        prompt_line, lines = long_task.layout("task", calls(3), "report", "sequential", lambda size: 0, False)
        result = long_task.evaluate(prompt_line, lines, calls(3), REVISIONS, 3)
        self.assertEqual((result["switch"], result["passed"]), (None, False))


if __name__ == "__main__":
    unittest.main()
