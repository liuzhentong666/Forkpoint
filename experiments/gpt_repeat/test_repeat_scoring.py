import json
from pathlib import Path
import tempfile
import unittest

from codex_rpc import wire
from repeat_scoring import peer_overwrites, trace_metrics, wilson
from run_gpt_repeats import constant_matches


class ScoringTests(unittest.TestCase):
    def test_intermediate_overwrite_remains_a_failure_after_restoration(self):
        before = {"row.price": 11, "row.stock": 100, "file.pricing": "PRICE=11"}
        owners = {"row.price": ("gpt-a", 11), "file.pricing": ("gpt-a", "PRICE=11")}
        wrong = {**before, "row.price": 10, "file.pricing": None}
        events = peer_overwrites(before, wrong, "gpt-b", owners)
        events += peer_overwrites(wrong, {**before, "row.stock": 95}, "gpt-b", owners)
        self.assertEqual({e['resource'] for e in events}, {"row.price", "file.pricing"})

    def test_correct_stock_merge_does_not_overwrite_price_or_code(self):
        before = {"row.price": 11, "row.stock": 100, "file.pricing": "PRICE=11"}
        owners = {"row.price": ("gpt-a", 11), "file.pricing": ("gpt-a", "PRICE=11")}
        self.assertEqual(peer_overwrites(before, {**before, "row.stock": 95}, "gpt-b", owners), [])
        self.assertEqual(owners['row.stock'], ('gpt-b',95))

    def test_usage_is_last_cumulative_value_and_each_raw_call_counted_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            usage=lambda n: {'inputTokens':n,'cachedInputTokens':n//2,'outputTokens':5,'reasoningOutputTokens':2,'totalTokens':n+5}
            request={'id':7,'method':'item/tool/call','params':{'threadId':'b','callId':'c','tool':'agentdb','arguments':{'action':'info'}}}
            events=[{'method':'thread/tokenUsage/updated','params':{'threadId':'a','tokenUsage':{'total':usage(10)}}},
                    {'method':'thread/tokenUsage/updated','params':{'threadId':'a','tokenUsage':{'total':usage(20)}}},
                    {'method':'thread/tokenUsage/updated','params':{'threadId':'b','tokenUsage':{'total':usage(30)}}},request,
                    {'method':'item/started','params':{'item':{'type':'dynamicToolCall'}}},
                    {'method':'item/completed','params':{'item':{'type':'dynamicToolCall'}}}]
            (root/'server.jsonl').write_bytes(b''.join(map(wire,events)))
            (root/'tool_calls.jsonl').write_bytes(wire(request)+wire({'id':7,'result':{'success':True}}))
            measured=trace_metrics(root,['a','b'])
            self.assertEqual(measured['tool_calls'],1)
            self.assertEqual(measured['tokens_total']['inputTokens'],50)
            self.assertEqual(measured['tokens_total']['totalTokens'],60)
            self.assertEqual(measured['unanswered_tool_request_ids'],[])
            self.assertIsNone(trace_metrics(root,['a','b','missing'])['tokens_total'])

    def test_policy_constants_and_small_sample_uncertainty(self):
        self.assertTrue(constant_matches('PRICE = 11.0','PRICE',11))
        self.assertFalse(constant_matches('PRICE = 10','PRICE',11))
        self.assertFalse(constant_matches('PRICE = compute()','PRICE',11))
        low,high=wilson(10,10)
        self.assertAlmostEqual(low,0.7224672,places=6)
        self.assertAlmostEqual(high,1,places=6)


if __name__=='__main__':
    unittest.main()
