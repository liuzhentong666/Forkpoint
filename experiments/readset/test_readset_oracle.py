import unittest

import oracle
from run_readset import (BROWSE_ROWS, browse_rows, bundles, decide, parse_conflicts, read_set, schedule,
                         workload_writers)


def doc(row, stock, price=10):
    return {"id": row, "sku": f"sku-{row}", "name": f"product-{row}", "price": price, "stock": stock}


def run(arm, workload, **counts):
    base = dict(merged=10, abstained=0, failed=0, resubmits=0, lock_timeouts=0, rethinks=0, useful_rethinks=0,
                wasted_rethinks=0, stale_commits=0, rule_violations=0)
    return dict(arm=arm, workload=workload, counts={**base, **counts}, verification=dict(ok=True),
                timing=dict(validation_ms=[5.0], vc_merge_ms=[40.0]))


class StaleCommitTests(unittest.TestCase):
    # A constructed W2 bundle: sellers of 3001 and 3002 both read 60/60 at the base.
    # A lands first, so its merge commit's first parent still has 60/60: fresh.
    # B lands second; its first parent has 3001 at 45, not the 60 B decided on: stale.
    used = {3001: doc(3001, 60), 3002: doc(3002, 60)}
    landed = [dict(writer=0, merge_commit="m-a", used=used), dict(writer=1, merge_commit="m-b", used=used)]
    parents = {"m-a": {3001: doc(3001, 60), 3002: doc(3002, 60)},
               "m-b": {3001: doc(3001, 45), 3002: doc(3002, 60)}}

    def test_second_sale_is_stale_and_first_is_fresh(self):
        stale = oracle.stale_commits(self.landed, self.parents)
        self.assertEqual([s["writer"] for s in stale], [1])
        self.assertEqual(stale[0]["dependencies"], [dict(key=3001, used=doc(3001, 60), at_parent=doc(3001, 45))])

    def test_writer_without_dependencies_is_never_stale(self):
        self.assertEqual(oracle.stale_commits([dict(writer=0, merge_commit="m-x", used={})], {}), [])

    def test_missing_first_parent_is_an_error_not_a_pass(self):
        with self.assertRaises(ValueError):
            oracle.stale_commits(self.landed, {"m-a": self.parents["m-a"]})

    def test_rule_violation_only_below_minimum(self):
        stock = {3001: 45, 3002: 45, 3003: 45, 3004: 60}
        self.assertEqual(oracle.rule_violations(stock, [(3001, 3002), (3003, 3004)], 100),
                         [dict(bundle=[3001, 3002], total=90)])


class RethinkTests(unittest.TestCase):
    sell = dict(rows={3001: {"stock": 45}}, file=None)

    def test_same_decision_again_is_wasted(self):
        self.assertEqual(oracle.classify_rethink(self.sell, dict(rows={3001: {"stock": 45}}, file=None)), "wasted")

    def test_abstaining_or_changing_the_decision_is_useful(self):
        self.assertEqual(oracle.classify_rethink(self.sell, None), "useful")
        self.assertEqual(oracle.classify_rethink(self.sell, dict(rows={3001: {"stock": 30}}, file=None)), "useful")


class WorkloadTests(unittest.TestCase):
    def test_bundle_seller_sells_alone_and_abstains_after_partner_sale(self):
        spec = workload_writers("W2")[0]
        self.assertEqual(decide(spec, {3001: doc(3001, 60), 3002: doc(3002, 60)}),
                         dict(rows={3001: {"stock": 45}}, file=None))
        self.assertIsNone(decide(spec, {3001: doc(3001, 60), 3002: doc(3002, 45)}))

    def test_w3_mixes_six_disjoint_writers_and_two_bundles(self):
        specs = workload_writers("W3")
        self.assertEqual([s["kind"] for s in specs], ["disjoint"] * 6 + ["bundle"] * 4)
        self.assertEqual(bundles(specs), [(3001, 3002), (3003, 3004)])
        self.assertEqual(specs[5]["writes"], list(range(51, 61)))

    def test_read_sets_per_arm(self):
        spec = workload_writers("W2")[3]  # sells 3004, depends on 3003
        browse = browse_rows(spec, 1, "W2")
        self.assertIsNone(read_set("head_blind", spec, browse, 1, "W2"))
        self.assertEqual(read_set("rs_declared", spec, browse, 1, "W2"), [3003])
        self.assertEqual(read_set("rs_tracked", spec, browse, 1, "W2"), sorted({3003, *browse}))
        dropped = [read_set("rs_undercite", spec, browse, s, "W2") for s in range(1, 41)]
        self.assertEqual(dropped, [read_set("rs_undercite", spec, browse, s, "W2") for s in range(1, 41)])
        self.assertTrue([] in dropped and [3003] in dropped)

    def test_browse_rows_are_fixed_by_seed_and_avoid_own_writes(self):
        spec = workload_writers("W1")[2]
        rows = browse_rows(spec, 4, "W1")
        self.assertEqual(rows, browse_rows(spec, 4, "W1"))
        self.assertEqual(len(rows), BROWSE_ROWS)
        self.assertFalse(set(rows) & set(spec["writes"]))
        self.assertTrue(all(1 <= r <= 1000 for r in rows))

    def test_schedule_rotates_arms_by_seed(self):
        arms = ("a", "b", "c")
        order = schedule([1, 2], ["W1"], arms)
        self.assertEqual([a for _, _, a in order], ["a", "b", "c", "b", "c", "a"])


class ProtocolTests(unittest.TestCase):
    def test_rs001_detail_and_hint(self):
        stderr = ('ERROR:  RS001: Read conflict: 1 key(s) changed on main since 5e1\n'
                  'DETAIL:  [{"key": {"id": 3002}, "now": {"stock": 45}, "base": {"stock": 60}, "table": "t"}]\n'
                  'HINT:  validated_at=1729512345.123456\n'
                  'CONTEXT:  PL/pgSQL function xr.merge_validated(text,uuid,jsonb,text) line 27 at RAISE\n')
        self.assertEqual(parse_conflicts(stderr), ([{"id": 3002}], 1729512345.123456))


class CriteriaTests(unittest.TestCase):
    def passing(self):
        runs = [run("rs_declared", "W1"), run("rs_declared", "W2", rethinks=5, useful_rethinks=5),
                run("rs_declared", "W3", rethinks=2, useful_rethinks=2),
                run("rs_tracked", "W2", rethinks=5, useful_rethinks=5), run("rs_tracked", "W3", rethinks=2),
                run("head_blind", "W2", stale_commits=5, rule_violations=5),
                run("head_blind", "W3", stale_commits=2, rule_violations=2)]
        return runs

    def test_all_criteria_pass(self):
        self.assertTrue(oracle.criteria(self.passing())["adopt"])

    def test_one_stale_commit_in_a_readset_arm_fails_safety(self):
        runs = self.passing()
        runs[1]["counts"].update(stale_commits=1, rule_violations=1)
        self.assertFalse(oracle.criteria(runs)["safety"])

    def test_blind_retry_not_matching_prediction_fails_safety(self):
        runs = self.passing()
        runs[5]["counts"]["rule_violations"] = 4
        self.assertFalse(oracle.criteria(runs)["safety"])

    def test_wasted_rethink_in_w3_fails_cost(self):
        runs = self.passing()
        runs[2]["counts"].update(rethinks=3, wasted_rethinks=1)
        self.assertFalse(oracle.criteria(runs)["cost"])

    def test_validation_slower_than_merge_fails_overhead(self):
        runs = self.passing()
        for r in runs:
            r["timing"]["validation_ms"] = [60.0]
        self.assertFalse(oracle.criteria(runs)["overhead"]["passed"])


if __name__ == "__main__":
    unittest.main()
