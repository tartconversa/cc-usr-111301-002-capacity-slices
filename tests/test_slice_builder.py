"""容量时间片构建器测试：数值正确性、域隔离、确定性、局部重建、双粒度防复制。"""
import json
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import slice_builder as sb  # noqa: E402


def cap_row(key, resource, rev, s, e, shares, withdrawn="False", line=2):
    return {"capacity_id": key, "resource": resource, "revision": str(rev),
            "start": str(s), "end": str(e), "shares": shares,
            "withdrawn": withdrawn, "_source_line": line}


def res_row(key, resource, job, rev, s, e, shares, withdrawn="False", line=2):
    return {"reservation": key, "resource": resource, "job": job, "revision": str(rev),
            "start": str(s), "end": str(e), "shares": shares,
            "withdrawn": withdrawn, "_source_line": line}


class CanonicalExampleTests(unittest.TestCase):
    def setUp(self):
        self.cap = [cap_row("c1", "card1", 1, 0, 10, "1")]
        self.res = [res_row("a", "card1", "A", 1, 0, 10, "3/5"),
                    res_row("b", "card1", "B", 1, 5, 15, "2/5")]
        self.result = sb.build_from_rows(self.cap, self.res)

    def test_three_atomic_slices_and_domains(self):
        facts = self.result["slice_facts"]
        self.assertEqual([(f["start_epoch"], f["end_epoch"]) for f in facts],
                         [(0, 5), (5, 10), (10, 15)])
        self.assertEqual([f["domain"] for f in facts], ["main", "main", "unknown"])

    def test_idle_full_and_unknown_split(self):
        f0, f1, f2 = self.result["slice_facts"]
        self.assertEqual(f0["idle_shares"], "2/5")          # 闲置 0.4
        self.assertEqual(f1["allocated_shares"], "1")       # 恰满
        self.assertIsNone(f2["capacity_shares"])            # 缺容量 unknown
        self.assertIsNone(f2["idle_shares"])                # 未知域不冒充零闲置
        self.assertEqual(f2["overcommit_shares"], "0")

    def test_share_seconds_match_prompt_numbers(self):
        # 分配事实保留全量：A=0.6×10=6，B=0.4×10=4（含未知域后 5 秒）
        totals = {}
        known_domain = {}
        for a in self.result["allocation_facts"]:
            totals[a["job"]] = totals.get(a["job"], Fraction(0)) + Fraction(a["allocation_seconds"])
            if a["domain"] != "unknown":
                known_domain[a["job"]] = known_domain.get(a["job"], Fraction(0)) + Fraction(
                    a["allocation_seconds"])
        self.assertEqual(totals, {"A": Fraction(6), "B": Fraction(4)})
        # 题面口径：已知容量域内 A=6、B=2
        self.assertEqual(known_domain, {"A": Fraction(6), "B": Fraction(2)})
        cap = sum((Fraction(c["capacity_seconds"]) for c in self.result["capacity_facts"]), Fraction(0))
        idle = sum((Fraction(c["idle_seconds"]) for c in self.result["capacity_facts"]), Fraction(0))
        self.assertEqual(cap, 10)
        self.assertEqual(idle, 2)
        # B 的后 5 秒落在未知域：有分配事实，但不产生容量/闲置秒
        unknown = sum(c["unknown_domain_seconds"] for c in self.result["capacity_facts"])
        self.assertEqual(unknown, 5)

    def test_conservation_holds(self):
        c = self.result["conservation"]
        self.assertTrue(c["reconciled_per_claim"])
        self.assertTrue(c["identity_holds"])
        self.assertEqual(c["identity_residual"], "0")
        self.assertEqual(sum(c["domain_duration_seconds"].values()), 15)


class UnknownVsZeroTests(unittest.TestCase):
    def test_explicit_zero_is_known_zero(self):
        r = sb.build_from_rows([cap_row("cz", "z", 1, 0, 5, "0")],
                               [res_row("zr", "z", "Z", 1, 0, 5, "1/2")])
        slc = r["slice_facts"][0]
        self.assertEqual(slc["capacity_shares"], "0")       # 字符串 "0"，不是 null
        self.assertEqual(slc["domain"], "anomaly")
        self.assertEqual(slc["overcommit_shares"], "1/2")

    def test_missing_capacity_is_unknown_not_zero(self):
        r = sb.build_from_rows([], [res_row("zr", "z", "Z", 1, 0, 5, "1/2")])
        slc = r["slice_facts"][0]
        self.assertIsNone(slc["capacity_shares"])
        self.assertEqual(slc["domain"], "unknown")
        # 未知域仍保留分配份额秒
        self.assertEqual(slc["allocations"][0]["share_seconds"], "5/2")

    def test_blank_share_parses_as_unknown_only_for_capacity(self):
        self.assertIsNone(sb.parse_optional_share(""))
        with self.assertRaises(ValueError):
            sb.parse_share("")
        with self.assertRaises(ValueError):
            sb.parse_share("-1/2")


class OvercommitTests(unittest.TestCase):
    def test_anomaly_isolated_without_scaling_down(self):
        cap = [cap_row("c", "card", 1, 0, 10, "1")]
        res = [res_row("d", "card", "D", 1, 0, 10, "3/5"),
               res_row("e", "card", "E", 1, 0, 10, "3/5")]
        r = sb.build_from_rows(cap, res)
        slc = r["slice_facts"][0]
        self.assertEqual(slc["domain"], "anomaly")
        self.assertEqual(slc["overcommit_shares"], "1/5")
        # 原始份额秒原封不动，没有按比例缩减
        self.assertEqual([a["share_seconds"] for a in slc["allocations"]], ["6", "6"])
        self.assertTrue(r["conservation"]["identity_holds"])

    def test_partial_overcommit_window(self):
        cap = [cap_row("c", "card", 1, 0, 10, "1")]
        res = [res_row("d", "card", "D", 1, 0, 10, "1/2"),
               res_row("e", "card", "E", 1, 4, 10, "1/2"),
               res_row("f", "card", "F", 1, 4, 10, "1/2")]  # [4,10) 三份合计 1.5
        r = sb.build_from_rows(cap, res)
        domains = {(f["start_epoch"], f["end_epoch"]): f["domain"] for f in r["slice_facts"]}
        self.assertEqual(domains, {(0, 4): "main", (4, 10): "anomaly"})
        anomaly = next(f for f in r["slice_facts"] if f["domain"] == "anomaly")
        self.assertEqual(anomaly["overcommit_shares"], "1/2")
        # 异常域份额秒仍可对回输入，未被缩减
        self.assertTrue(r["conservation"]["reconciled_per_claim"])
        self.assertEqual(
            sum(Fraction(a["share_seconds"]) for a in anomaly["allocations"]),
            Fraction(9),  # 3 个作业 × 0.5 × 6 秒
        )


class RevisionAndWithdrawTests(unittest.TestCase):
    def test_withdrawn_key_contributes_nothing(self):
        cap = [cap_row("c", "card", 1, 0, 10, "1")]
        res = [res_row("w1", "card", "C", 1, 0, 10, "1/2"),
               res_row("w1", "card", "C", 2, 0, 10, "1/2", "True"),
               res_row("w2", "card", "C", 1, 2, 8, "1/4")]
        r = sb.build_from_rows(cap, res)
        self.assertTrue(all("w1" not in f["lineage"]["all_active_claims"]
                            for f in r["slice_facts"]))
        mid = next(f for f in r["slice_facts"] if f["start_epoch"] == 2)
        self.assertEqual(mid["allocations"][0]["reservation"], "w2")
        # 撤回键在守恒对账中输入秒为 0
        self.assertEqual(r["conservation"]["claim_seconds_mismatch"], {})

    def test_same_revision_conflict_rejected(self):
        rows = [cap_row("x", "cardX", 1, 0, 5, "1"),
                cap_row("x", "cardX", 1, 0, 5, "1/2", line=3)]
        with self.assertRaises(ValueError):
            sb.load_claims(rows, [])

    def test_same_revision_identical_replay_is_idempotent(self):
        rows = [cap_row("x", "cardX", 1, 0, 5, "1"),
                cap_row("x", "cardX", 1, 0, 5, "1")]
        caps, _ = sb.load_claims(rows, [])
        self.assertEqual(len(caps), 1)

    def test_higher_revision_wins_regardless_of_order(self):
        rows = [cap_row("x", "cardX", 3, 0, 5, "1/4"),
                cap_row("x", "cardX", 1, 0, 5, "1"),
                cap_row("x", "cardX", 2, 0, 5, "1/2")]
        caps, _ = sb.load_claims(rows, [])
        self.assertEqual(caps["x"].revision, 3)
        self.assertEqual(caps["x"].shares, Fraction(1, 4))

    def test_overlapping_capacity_claims_rejected(self):
        cap = [cap_row("p1", "card", 1, 0, 6, "1"),
               cap_row("p2", "card", 1, 5, 10, "1")]     # [0,6) 与 [5,10) 真交叠
        with self.assertRaises(ValueError):
            sb.build_from_rows(cap, [])


class DeterminismTests(unittest.TestCase):
    def setUp(self):
        self.cap = [cap_row("c1", "card1", 1, 0, 10, "1")]
        self.res = [res_row("a", "card1", "A", 1, 0, 10, "3/5"),
                    res_row("b", "card1", "B", 1, 5, 15, "2/5")]
        self.base = sb.build_from_rows(self.cap, self.res)
        self.ids = [f["slice_id"] for f in self.base["slice_facts"]]

    def test_shuffled_rows_identical_identity(self):
        import random
        rng = random.Random(7)
        c2, r2 = list(self.cap), list(self.res)
        rng.shuffle(c2)
        rng.shuffle(r2)
        again = sb.build_from_rows(c2, r2)
        self.assertEqual([f["slice_id"] for f in again["slice_facts"]], self.ids)

    def test_batched_merge_equals_one_shot(self):
        c1, r1 = sb.load_claims([], [self.res[1]])           # 先到 B
        c2, r2 = sb.load_claims(self.cap, [self.res[0]])     # 再到容量与 A
        caps, ress = sb.merge_claim_sets(c1, c2), sb.merge_claim_sets(r1, r2)
        sliced = sb.build_all_slices(caps, ress)
        got = [sb.slice_identity(s) for sl in sliced.values() for s in sl]
        self.assertEqual(got, self.ids)

    def test_cross_process_restart_identical(self):
        code = (
            "import sys; sys.path.insert(0, %r);"
            "from src import slice_builder as sb;"
            "r=sb.run('fixtures/capacity.csv','fixtures/reservations.csv','fixtures/slices.json');"
            "print('|'.join(x['slice_id'] for x in r['slice_facts']))" % str(ROOT)
        )
        proc = subprocess.run([sys.executable, "-I", "-c", code], cwd=ROOT,
                              capture_output=True, text=True, check=True)
        self.assertEqual(proc.stdout.strip(), "|".join(self.ids))

    def test_local_rebuild_matches_full_rebuild(self):
        caps_old, ress_old = sb.load_claims(self.cap, self.res)
        previous = sb.build_all_slices(caps_old, ress_old)
        caps_new, ress_new = sb.load_claims(
            [cap_row("c1", "card1", 2, 0, 10, "2")], self.res)
        rebuilt = sb.rebuild_overlap(previous, caps_new, ress_new, ["c1"])
        full = sb.build_all_slices(caps_new, ress_new)
        self.assertEqual(
            [sb.slice_identity(s) for sl in rebuilt.values() for s in sl],
            [sb.slice_identity(s) for sl in full.values() for s in sl],
        )
        # 修订区间之外的资源/片零改动
        untouched = sb.rebuild_overlap(previous, caps_new, ress_new, ["c1"])
        self.assertEqual(
            [sb.slice_identity(s) for s in untouched["card1"] if s.start >= 10],
            [sb.slice_identity(s) for s in previous["card1"] if s.start >= 10],
        )

    def test_deleted_key_requires_full_rebuild(self):
        caps_old, ress_old = sb.load_claims(self.cap, self.res)
        previous = sb.build_all_slices(caps_old, ress_old)
        empty_caps, _ = sb.load_claims([], self.res)
        with self.assertRaises(ValueError):
            sb.rebuild_overlap(previous, empty_caps, ress_old, ["c1"])

    def test_conflicting_cross_batch_merge_rejected(self):
        a = {"x": sb.Claim("x", "capacity", "r", 1, 0, 5, Fraction(1), False, None, 2)}
        b = {"x": sb.Claim("x", "capacity", "r", 1, 0, 5, Fraction(1, 2), False, None, 3)}
        with self.assertRaises(ValueError):
            sb.merge_claim_sets(a, b)


class TimeBoundaryTests(unittest.TestCase):
    def test_shanghai_midnight_split(self):
        # origin=2026-11-10T23:59:55+08:00 => 相对秒 5 恰为上海次日 00:00:00
        config = {"time_origin": "2026-11-10T23:59:55+08:00"}
        r = sb.build_from_rows([cap_row("c", "card", 1, 0, 10, "1")],
                               [res_row("g", "card", "G", 1, 2, 8, "1/2")], config)
        per_day = {a["day"]: a["allocation_seconds"] for a in r["allocation_facts"]}
        self.assertEqual(per_day, {"2026-11-10": "3/2", "2026-11-11": "3/2"})
        cap_days = {c["day"]: c["capacity_seconds"] for c in r["capacity_facts"]}
        self.assertEqual(cap_days, {"2026-11-10": "5", "2026-11-11": "5"})

    def test_shanghai_parts_anchor_math(self):
        origin = sb.parse_epoch("2026-11-10T23:59:55+08:00")
        parts = sb.shanghai_day_parts(2, 8, origin)
        self.assertEqual([p[2] for p in parts], ["2026-11-10", "2026-11-11"])
        self.assertEqual([(p[0], p[1]) for p in parts], [(2, 5), (5, 8)])

    def test_dst_aware_iso_normalized_to_utc(self):
        # 带不同偏移的 ISO 串表示同一绝对时刻时归一化结果相同
        self.assertEqual(sb.parse_epoch("2026-11-10T23:59:57+08:00"),
                         sb.parse_epoch("2026-11-10T15:59:57+00:00"))
        # 相对秒锚点：origin + 2 == 同一绝对时刻
        origin = sb.parse_epoch("2026-11-10T23:59:55+08:00")
        self.assertEqual(origin + sb.parse_epoch("2"),
                         sb.parse_epoch("2026-11-10T23:59:57+08:00"))
        # 无时区的 ISO 按 UTC 处理
        self.assertEqual(sb.parse_epoch("2026-11-10T15:59:57"),
                         sb.parse_epoch("2026-11-10T15:59:57+00:00"))

    def test_abutting_intervals_no_overlap_no_zero_slice(self):
        cap = [cap_row("p1", "card", 1, 0, 5, "1"),
               cap_row("p2", "card", 2, 5, 10, "1")]
        r = sb.build_from_rows(cap, [res_row("h", "card", "H", 1, 0, 10, "1/2")])
        self.assertEqual([f["start_epoch"] for f in r["slice_facts"]], [0, 5])
        self.assertTrue(all(f["duration_seconds"] > 0 for f in r["slice_facts"]))
        self.assertTrue(all(f["domain"] == "main" for f in r["slice_facts"]))

    def test_non_positive_interval_rejected(self):
        with self.assertRaises(ValueError):
            sb.build_from_rows([cap_row("c", "card", 1, 5, 5, "1")], [])


class LineageAndGrainTests(unittest.TestCase):
    def test_slice_level_lineage_points_to_original_claims(self):
        cap = [cap_row("c1", "card1", 1, 0, 10, "1", line=2)]
        res = [res_row("a", "card1", "A", 1, 0, 10, "3/5", line=2),
               res_row("b", "card1", "B", 2, 5, 15, "2/5", line=3)]
        r = sb.build_from_rows(cap, res)
        mid = next(f for f in r["slice_facts"] if f["start_epoch"] == 5)
        self.assertEqual(mid["lineage"]["capacity_claim"], "c1")
        self.assertEqual(mid["lineage"]["capacity_revision"], 1)
        self.assertEqual(mid["lineage"]["reservation_revisions"], {"a": 1, "b": 2})
        self.assertEqual(mid["lineage"]["reservation_source_lines"], {"a": 2, "b": 3})

    def test_capacity_not_duplicated_per_job(self):
        cap = [cap_row("c", "card", 1, 0, 10, "1")]
        res = [res_row("a", "card", "A", 1, 0, 10, "1/4"),
               res_row("b", "card", "B", 1, 0, 10, "1/4")]
        r = sb.build_from_rows(cap, res)
        # 资源粒度只有 1 行/天，容量秒=10，而不是按两个作业复制成 20
        self.assertEqual(len(r["capacity_facts"]), 1)
        self.assertEqual(r["capacity_facts"][0]["capacity_seconds"], "10")
        # 全量分配秒（资源粒度）== 作业粒度两行之和：不复制、不遗漏
        alloc_jobs = sum(Fraction(a["allocation_seconds"]) for a in r["allocation_facts"])
        self.assertEqual(alloc_jobs, Fraction(5))
        self.assertEqual(Fraction(r["capacity_facts"][0]["allocated_seconds"]), alloc_jobs)
        self.assertEqual(len(r["allocation_facts"]), 2)

    def test_allocated_seconds_unknown_domain_not_in_known_field(self):
        cap = [cap_row("c", "card", 1, 0, 5, "1")]
        res = [res_row("b", "card", "B", 1, 0, 10, "1/2")]  # 后 5 秒落入未知域
        r = sb.build_from_rows(cap, res)
        row = r["capacity_facts"][0]
        self.assertEqual(Fraction(row["allocated_seconds"]), Fraction(5))             # 全量 0.5×10
        self.assertEqual(Fraction(row["allocated_seconds_known_domain"]), Fraction("5/2"))  # 仅已知域
        self.assertEqual(Fraction(row["capacity_seconds"]), Fraction(5))

    def test_multi_resource_partition_publish_and_manifest(self):
        cap = [cap_row("c1", "card1", 1, 0, 5, "1"),
               cap_row("c2", "card2", 1, 0, 5, "2")]
        res = [res_row("a", "card1", "A", 1, 0, 5, "1/2"),
               res_row("b", "card2", "B", 1, 0, 5, "1")]
        caps, ress = sb.load_claims(cap, res)
        sliced = sb.build_all_slices(caps, ress)
        with tempfile.TemporaryDirectory() as tmp:
            manifest = sb.publish_partitions(sliced, caps, ress, tmp, max_workers=2)
            base = Path(tmp)
            self.assertTrue((base / "slice_facts/resource=card1/slice_facts.json").exists())
            self.assertTrue((base / "slice_facts/resource=card2/slice_facts.json").exists())
            self.assertTrue((base / "manifest.json").exists())
            # 分区内容与内存事实一致
            p1 = json.loads((base / manifest["partitions"]["card1"]["path"]).read_text())
            self.assertEqual(p1[0]["capacity_shares"], "1")
            p2 = json.loads((base / manifest["partitions"]["card2"]["path"]).read_text())
            self.assertEqual(p2[0]["capacity_shares"], "2")
            # 重发产生相同 manifest 哈希（确定性）
            again = sb.publish_partitions(sliced, caps, ress, tmp, max_workers=1)
            self.assertEqual(again["manifest_sha256"], manifest["manifest_sha256"])

    def test_decimal_view_keeps_rational_original(self):
        r = sb.build_from_rows([cap_row("c", "card", 1, 0, 3, "1")],
                               [res_row("a", "card", "A", 1, 0, 3, "1/3")])
        view = sb.decimal_view(r["slice_facts"])[0]
        self.assertEqual(view["allocations"][0]["shares"], "1/3")          # 原文保留
        self.assertEqual(view["allocations"][0]["shares_decimal"], "0.333333")  # 定点视图
        self.assertEqual(view["allocations"][0]["share_seconds"], "1")


class FixtureContractTests(unittest.TestCase):
    def test_end_to_end_on_shipped_fixtures(self):
        r = sb.run(ROOT / "fixtures/capacity.csv",
                   ROOT / "fixtures/reservations.csv",
                   ROOT / "fixtures/slices.json")
        self.assertTrue(r["conservation"]["reconciled_per_claim"])
        self.assertTrue(r["conservation"]["identity_holds"])
        self.assertEqual(len(r["slice_facts"]), 3)
        self.assertEqual(r["config"]["oversubscription_policy"], "separate_anomaly_domain")
        self.assertEqual(r["config"]["share_encoding"], "rational_string")


if __name__ == "__main__":
    unittest.main(verbosity=2)
