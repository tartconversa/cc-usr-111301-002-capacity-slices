"""容量时间片构建器演示：主示例 + 超配 + 撤回 + 跨午夜 + 恰好相接。

运行（从仓库根目录，隔离模式避免任何被植入的本地模块）：
    python -I demo/run_demo.py
输出写入 .workspace-output/demo/，原始 fixtures 不被触碰。
"""
import json
import random
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import slice_builder as sb  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / ".workspace-output" / "demo"


def rows(capacity, reservation):
    """把内部三元组转成 DictReader 风格行。"""
    cap_rows = [
        {
            "capacity_id": k, "resource": r, "revision": str(rev), "start": str(s),
            "end": str(e), "shares": sh, "withdrawn": wd,
        }
        for (k, r, rev, s, e, sh, wd) in capacity
    ]
    res_rows = [
        {
            "reservation": k, "resource": r, "job": j, "revision": str(rev),
            "start": str(s), "end": str(e), "shares": sh, "withdrawn": wd,
        }
        for (k, r, j, rev, s, e, sh, wd) in reservation
    ]
    return cap_rows, res_rows


def show(title, result):
    print("=" * 78)
    print(title)
    print("-" * 78)
    for rec in result["slice_facts"]:
        cap = rec["capacity_shares"]
        cap_txt = "unknown" if cap is None else cap
        print(
            f"  {rec['resource']} [{rec['start_epoch']:>2},{rec['end_epoch']:>2}) "
            f"dur={rec['duration_seconds']:>2} domain={rec['domain']:<7} "
            f"cap={cap_txt:<7} alloc={rec['allocated_shares']:<5} "
            f"idle={str(rec['idle_shares']):<7} over={rec['overcommit_shares']}"
        )
        for a in rec["allocations"]:
            print(f"      └ job={a['job']} key={a['reservation']} shares={a['shares']} "
                  f"share_seconds={a['share_seconds']}")
        lin = rec["lineage"]
        print(f"      血缘: cap={lin['capacity_claim']}@r{lin['capacity_revision']} "
              f"res={lin['reservation_revisions']} 片ID={rec['slice_id']}")
    cons = result["conservation"]
    print("  守恒: 逐键对账=%s 域恒等式=%s(残差=%s)"
          % (cons["reconciled_per_claim"], cons["identity_holds"], cons["identity_residual"]))
    print("  域时长(秒):", cons["domain_duration_seconds"],
          " 域容量秒:", cons["domain_capacity_seconds"])
    print("  资源×日容量事实:")
    for c in result["capacity_facts"]:
        print("   ", c)
    print("  资源×日×作业分配事实:")
    for a in result["allocation_facts"]:
        print("   ", a)


def main():
    # --- 场景 1：题面主示例（容量1，A 0.6@[0,10)，B 0.4@[5,15)）-----------------
    s1_cap, s1_res = rows(
        [("c1", "card1", 1, 0, 10, "1", "False")],
        [("a", "card1", "A", 1, 0, 10, "3/5", "False"),
         ("b", "card1", "B", 1, 5, 15, "2/5", "False")],
    )
    config1 = {"time_origin": "2026-11-10T23:59:55+08:00"}
    r1 = sb.build_from_rows(s1_cap, s1_res, config1)
    show("场景1 主示例：[0,5)闲置0.4 / [5,10)恰满 / [10,15)容量未知", r1)
    assert [d["domain"] for d in r1["slice_facts"]] == ["main", "main", "unknown"]
    total, known = {}, {}
    for a in r1["allocation_facts"]:
        total[a["job"]] = total.get(a["job"], Fraction(0)) + Fraction(a["allocation_seconds"])
        if a["domain"] != "unknown":
            known[a["job"]] = known.get(a["job"], Fraction(0)) + Fraction(a["allocation_seconds"])
    assert total == {"A": Fraction(6), "B": Fraction(4)}, total       # 全量份额秒
    assert known == {"A": Fraction(6), "B": Fraction(2)}, known       # 题面已知容量域口径
    cap_sec = sum((Fraction(c["capacity_seconds"]) for c in r1["capacity_facts"]), Fraction(0))
    idle_sec = sum((Fraction(c["idle_seconds"]) for c in r1["capacity_facts"]), Fraction(0))
    assert cap_sec == 10 and idle_sec == 2  # B 的后 5 秒在未知域，不算闲置、不算零用量

    # --- 场景 2：超配隔离异常域，不缩减任何分配 ----------------------------------
    s2_cap, s2_res = rows(
        [("c2", "card2", 1, 0, 10, "1", "False")],
        [("d", "card2", "D", 1, 0, 10, "3/5", "False"),
         ("e", "card2", "E", 1, 0, 10, "3/5", "False")],  # 合计 1.2 > 1
    )
    r2 = sb.build_from_rows(s2_cap, s2_res, {"case": "overcommit"})
    show("场景2 超配：分配 1.2 > 容量 1，整段隔离 anomaly，原始份额秒保留", r2)
    slc = r2["slice_facts"][0]
    assert slc["domain"] == "anomaly" and slc["overcommit_shares"] == "1/5"
    assert Fraction(slc["allocations"][0]["share_seconds"]) == Fraction(6)  # 0.6*10 未被缩减
    assert Fraction(slc["allocations"][1]["share_seconds"]) == Fraction(6)
    # 显式 0 容量：任何正分配都是异常，且 0 与 unknown 不同
    z_cap, z_res = rows(
        [("cz", "cardZ", 1, 0, 5, "0", "False")],
        [("z", "cardZ", "Z", 1, 0, 5, "1/2", "False")],
    )
    rz = sb.build_from_rows(z_cap, z_res, {"case": "explicit_zero"})
    assert rz["slice_facts"][0]["capacity_shares"] == "0"
    assert rz["slice_facts"][0]["domain"] == "anomaly"

    # --- 场景 3：撤回 + 同作业多个 reservation 键 + 同 revision 冲突拒绝 ---------
    w_cap, w_res = rows(
        [("c3", "card3", 1, 0, 10, "1", "False")],
        [("w1", "card3", "C", 1, 0, 10, "1/2", "False"),
         ("w1", "card3", "C", 2, 0, 10, "1/2", "True"),   # 撤回 w1
         ("w2", "card3", "C", 1, 2, 8, "1/4", "False")],  # 同作业的第二个键，仍有效
    )
    r3 = sb.build_from_rows(w_cap, w_res, {"case": "withdraw"})
    show("场景3 撤回：w1@r2 撤回后不贡献，同作业 C 的 w2 继续贡献", r3)
    assert all("w1" not in rec["lineage"]["all_active_claims"] for rec in r3["slice_facts"])
    mid = next(x for x in r3["slice_facts"] if x["start_epoch"] == 2)
    assert mid["allocations"][0]["reservation"] == "w2"
    assert mid["allocated_shares"] == "1/4" and mid["idle_shares"] == "3/4"
    conflict_rows = [
        {"capacity_id": "x", "resource": "cardX", "revision": "1", "start": "0",
         "end": "5", "shares": "1", "withdrawn": "False", "_source_line": 2},
        {"capacity_id": "x", "resource": "cardX", "revision": "1", "start": "0",
         "end": "5", "shares": "1/2", "withdrawn": "False", "_source_line": 3},
    ]
    try:
        sb.load_claims(conflict_rows, [])
        raise AssertionError("同 revision 冲突必须拒绝")
    except ValueError as exc:
        print("  同revision冲突拒绝 OK:", str(exc)[:60])

    # --- 场景 4：跨上海午夜（origin 2026-11-10T23:59:55+08:00，t=5 即午夜）-------
    x_cap, x_res = rows(
        [("c4", "card4", 1, 0, 10, "1", "False")],
        [("g", "card4", "G", 1, 2, 8, "1/2", "False")],
    )
    r4 = sb.build_from_rows(x_cap, x_res,
                           {"case": "cross_midnight", "time_origin": "2026-11-10T23:59:55+08:00"})
    show("场景4 跨午夜：片 [2,8) 按上海自然日切成 2026-11-10(3s)+2026-11-11(3s)", r4)
    days = sorted({a["day"] for a in r4["allocation_facts"]})
    assert days == ["2026-11-10", "2026-11-11"], days
    per_day = {a["day"]: a["allocation_seconds"] for a in r4["allocation_facts"]}
    assert per_day == {"2026-11-10": "3/2", "2026-11-11": "3/2"}, per_day  # 0.5×3 各一天
    # DST 来源时间先统一 UTC：+08:00 与 Z 表示同一绝对时刻；相对秒 2 = origin+2
    origin = sb.parse_epoch("2026-11-10T23:59:55+08:00")
    assert sb.parse_epoch("2026-11-10T23:59:57+08:00") == \
           sb.parse_epoch("2026-11-10T15:59:57+00:00")
    assert origin + sb.parse_epoch("2") == sb.parse_epoch("2026-11-10T23:59:57+08:00")

    # --- 场景 5：恰好相接（端点相等不相交，不产生冲突/零长片）---------------------
    t_cap, t_res = rows(
        [("p1", "card5", 1, 0, 5, "1", "False"),
         ("p2", "card5", 2, 5, 10, "1", "False")],   # [0,5) 与 [5,10) 恰好相接
        [("h", "card5", "H", 1, 0, 10, "1/2", "False")],
    )
    r5 = sb.build_from_rows(t_cap, t_res, {"case": "abutting"})
    show("场景5 恰好相接：两条容量声明在 t=5 相接，无交叠冲突、无零长片", r5)
    starts = [x["start_epoch"] for x in r5["slice_facts"]]
    assert starts == [0, 5] and all(x["duration_seconds"] > 0 for x in r5["slice_facts"])
    assert all(x["domain"] == "main" for x in r5["slice_facts"])

    # --- 确定性：乱序行、分批投递、局部重建都得到同一组片身份 ---------------------
    rng = random.Random(20261113)
    shuffled_cap, shuffled_res = list(s1_cap), list(s1_res)
    rng.shuffle(shuffled_cap)
    rng.shuffle(shuffled_res)
    r1_shuf = sb.build_from_rows(shuffled_cap, shuffled_res)
    ids_a = [x["slice_id"] for x in r1["slice_facts"]]
    ids_b = [x["slice_id"] for x in r1_shuf["slice_facts"]]
    assert ids_a == ids_b, "片身份依赖了文件顺序"

    # 分批：先到 B，再到容量与 A，用 merge_claim_sets 合并
    cap_b1, res_b1 = sb.load_claims([], [s1_res[1]])
    cap_b2, res_b2 = sb.load_claims(s1_cap, [s1_res[0]])
    cap_merge = sb.merge_claim_sets(cap_b1, cap_b2)
    res_merge = sb.merge_claim_sets(res_b1, res_b2)
    slices_batch = sb.build_all_slices(cap_merge, res_merge)
    assert [sb.slice_identity(s) for sl in slices_batch.values() for s in sl] == ids_a
    print("=" * 78)
    print("确定性 OK: 乱序行 / 分批 merge / 内容哈希片身份 全部一致")

    # 局部重建：容量 c1 修订（r2，[0,10) 容量改 2），只重建交叠范围
    caps2, ress2 = sb.load_claims(
        [{**s1_cap[0], "revision": "2", "shares": "2"}], s1_res
    )
    prev = sb.build_all_slices(*sb.load_claims(s1_cap, s1_res))
    rebuilt = sb.rebuild_overlap(prev, caps2, ress2, ["c1"])
    full = sb.build_all_slices(caps2, ress2)
    assert [sb.slice_identity(s) for sl in rebuilt.values() for s in sl] == \
           [sb.slice_identity(s) for sl in full.values() for s in sl]
    print("局部重建 OK: 与全量重建片身份逐片相等，非交叠片零改动")

    # 落盘（含分区发布 + manifest 提交点 + Decimal 视图）
    manifest = sb.publish_partitions(
        r1["slices_by_resource"], r1["claims"]["capacity"], r1["claims"]["reservation"],
        OUT / "canonical", max_workers=1, origin_epoch=r1["origin_epoch"],
    )
    dec = sb.decimal_view(r1["slice_facts"])
    (OUT / "canonical" / "slice_facts_decimal.json").write_text(
        json.dumps(dec, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT / "canonical" / "conservation.json").write_text(
        json.dumps(r1["conservation"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("落盘 OK:", OUT / "canonical", "manifest sha:", manifest["manifest_sha256"][:16])

    # 跨进程重启等价：子进程独立解释器重算，比较片身份序列
    code = (
        "import sys; sys.path.insert(0, %r);"
        "from src import slice_builder as sb;"
        "r=sb.run('fixtures/capacity.csv','fixtures/reservations.csv','fixtures/slices.json');"
        "print('|'.join(x['slice_id'] for x in r['slice_facts']))"
        % str(Path(__file__).resolve().parents[1])
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=True,
    )
    assert proc.stdout.strip() == "|".join(ids_a)
    print("跨进程重启 OK: 独立解释器输出相同片身份序列")
    print("全部断言通过。")


if __name__ == "__main__":
    main()
