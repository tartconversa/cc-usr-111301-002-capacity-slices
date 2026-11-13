"""容量时间片构建器：把容量声明与分配区间切成可加总的原子片事实。

只做区间代数与聚合，不运行任何作业调度。

设计要点
- 所有区间为左闭右开；时间在解析层统一归一化为 UTC epoch 秒（int），DST 来源时间带偏移转 UTC。
- 份额为非负有理数（fractions.Fraction），落盘序列化为有理数字符串，另给 Decimal 定点视图。
- 每个键按 revision 取最新声明；withdrawn 撤回使该键不再贡献；同 revision 冲突拒绝、同值重放幂等。
- 以全部端点组成原子片；缺容量是 unknown（None），显式 0 容量是已知 0，二者严格区分。
- 超配片隔离为 anomaly 域，绝不对分配做比例缩减；main/anomaly/unknown 三域的时长与份额秒均可对回输入。
- 片身份是内容哈希，与文件顺序、批处理到达顺序、进程重启无关。
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
_ZERO = Fraction(0)

# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def parse_share(raw: str) -> Fraction:
    """分配份额：非负有理数。空串非法（缺容量只能表现为“没有容量声明”）。"""
    text = (raw or "").strip()
    if text == "":
        raise ValueError("份额不能为空：缺容量应表现为无容量声明，而非空 shares")
    value = Fraction(text)
    if value < 0:
        raise ValueError(f"份额不能为负: {raw!r}")
    return value


def parse_optional_share(raw: Optional[str]) -> Optional[Fraction]:
    """容量份额：空/None => 未知容量 None；'0' => 已知零容量。"""
    if raw is None or raw.strip() == "":
        return None
    return parse_share(raw)


def parse_epoch(raw: str) -> int:
    """整数 epoch 秒，或带偏移的 ISO-8601；无时区按 UTC。DST 时间在源头携带偏移，此处统一转 UTC。"""
    text = raw.strip()
    try:
        return int(text)
    except ValueError:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.astimezone(timezone.utc).timestamp())


def parse_bool(raw: str) -> bool:
    text = (raw or "").strip().lower()
    if text in ("true", "1", "yes", "y", "t"):
        return True
    if text in ("false", "0", "no", "n", "f", ""):
        return False
    raise ValueError(f"无法解析布尔值: {raw!r}")


# ---------------------------------------------------------------------------
# 声明模型与 revision 归并（乱序安全、幂等、同 revision 冲突拒绝）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Claim:
    """一个键在最新 revision 上的声明视图。"""

    key: str
    kind: str  # 'capacity' | 'reservation'
    resource: str
    revision: int
    start: int
    end: int
    shares: Optional[Fraction]  # 容量可为 None（未知）；分配恒为 Fraction
    withdrawn: bool
    job: Optional[str]
    source_line: int  # 血缘：CSV 行号（含表头，1 起）

    def __post_init__(self):
        if self.end <= self.start:
            raise ValueError(f"键 {self.key} 区间非正: [{self.start},{self.end})")


@dataclass
class _Latest:
    revision: int = -1
    claim: Optional[Claim] = None
    first_at_rev: Optional[Claim] = None


def _resolve(rows: list[dict], kind: str) -> dict[str, Claim]:
    key_field = "capacity_id" if kind == "capacity" else "reservation"
    latest: dict[str, _Latest] = {}
    for row in rows:
        key = row[key_field].strip()
        revision = int(row["revision"])
        claim = Claim(
            key=key,
            kind=kind,
            resource=row["resource"].strip(),
            revision=revision,
            start=parse_epoch(row["start"]),
            end=parse_epoch(row["end"]),
            shares=(parse_optional_share(row.get("shares")) if kind == "capacity"
                    else parse_share(row["shares"])),
            withdrawn=parse_bool(row.get("withdrawn", "False")),
            job=(row["job"].strip() if kind == "reservation" else None),
            source_line=int(row.get("_source_line", -1)),
        )
        cur = latest.get(key)
        if cur is None:
            latest[key] = _Latest(revision, claim, claim)
            continue
        if revision < cur.revision:
            continue  # 乱序到达的旧 revision
        if revision == cur.revision:
            ref = cur.first_at_rev
            assert ref is not None
            if (ref.resource, ref.start, ref.end, ref.shares, ref.withdrawn, ref.job) != (
                claim.resource, claim.start, claim.end, claim.shares, claim.withdrawn, claim.job
            ):
                raise ValueError(
                    f"键 {key} 在同一 revision={revision} 出现冲突声明: "
                    f"({ref.resource},{ref.start},{ref.end},{ref.shares},{ref.withdrawn},{ref.job}) "
                    f"!= ({claim.resource},{claim.start},{claim.end},{claim.shares},"
                    f"{claim.withdrawn},{claim.job})"
                )
            continue  # 同值重放，幂等
        latest[key] = _Latest(revision, claim, claim)
    return {k: v.claim for k, v in latest.items() if v.claim is not None}


def merge_claim_sets(*batches: dict[str, Claim]) -> dict[str, Claim]:
    """合并多批已归并声明（模拟乱序分批/跨进程投递）：高 revision 覆盖低 revision，
    同 revision 同键内容不一致即冲突拒绝。结果与一次性全量归并逐键相同。"""
    merged: dict[str, Claim] = {}
    for batch in batches:
        for key, incoming in batch.items():
            cur = merged.get(key)
            if cur is None or incoming.revision > cur.revision:
                merged[key] = incoming
            elif incoming.revision == cur.revision and incoming != cur:
                raise ValueError(f"键 {key} 跨批同 revision={cur.revision} 冲突")
    return merged


def load_claims(capacity_rows: Iterable[dict], reservation_rows: Iterable[dict]):
    cap_rows = list(capacity_rows)
    res_rows = list(reservation_rows)
    for i, row in enumerate(cap_rows, start=2):
        row.setdefault("_source_line", i)
    for i, row in enumerate(res_rows, start=2):
        row.setdefault("_source_line", i)
    return _resolve(cap_rows, "capacity"), _resolve(res_rows, "reservation")


# ---------------------------------------------------------------------------
# 原子片构建（每资源独立扫描线，资源间可并行）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Slice:
    resource: str
    start: int
    end: int
    capacity_shares: Optional[Fraction]  # None = 未知容量域
    allocations: tuple[tuple[str, str, Fraction], ...]  # (job, reservation_key, shares)
    allocated: Fraction
    idle: Optional[Fraction]
    overcommit: Fraction
    domain: str  # main | anomaly | unknown
    lineage_keys: tuple[str, ...]

    @property
    def duration(self) -> int:
        return self.end - self.start


def build_slices_for_resource(
    resource: str,
    capacities: dict[str, Claim],
    reservations: dict[str, Claim],
) -> list[Slice]:
    caps = {k: c for k, c in capacities.items() if c.resource == resource and not c.withdrawn}
    ress = {k: c for k, c in reservations.items() if c.resource == resource and not c.withdrawn}

    endpoints = {p for c in (*caps.values(), *ress.values()) for p in (c.start, c.end)}
    if not endpoints:
        return []

    slices: list[Slice] = []
    for lo, hi in zip(sorted(endpoints), sorted(endpoints)[1:]):
        cap_here = [c for c in caps.values() if c.start < hi and c.end > lo]
        res_here = [c for c in ress.values() if c.start < hi and c.end > lo]

        if len(cap_here) > 1:  # 同资源同时刻容量声明交叠即契约冲突
            raise ValueError(
                f"资源 {resource} 在 [{lo},{hi}) 存在交叠容量声明: "
                f"{sorted(c.key for c in cap_here)}"
            )
        capacity = cap_here[0].shares if cap_here else None

        allocs = tuple(
            sorted(
                ((c.job, c.key, c.shares) for c in res_here),
                key=lambda x: (x[0], x[1]),
            )
        )
        allocated = sum((a[2] for a in allocs), _ZERO)
        lineage = tuple(sorted([*(c.key for c in cap_here), *(c.key for c in res_here)]))

        if capacity is None:
            # 未知容量域：闲置/超配不可判定，绝不冒充零闲置
            domain, idle, overcommit = "unknown", None, _ZERO
        else:
            overcommit = max(_ZERO, allocated - capacity)
            idle = max(_ZERO, capacity - allocated)
            domain = "anomaly" if allocated > capacity else "main"

        slices.append(
            Slice(resource, lo, hi, capacity, allocs, allocated, idle, overcommit, domain, lineage)
        )
    return slices


def all_resources(capacities: dict[str, Claim], reservations: dict[str, Claim]) -> list[str]:
    return sorted(
        {c.resource for c in capacities.values()} | {c.resource for c in reservations.values()}
    )


def build_all_slices(
    capacities: dict[str, Claim],
    reservations: dict[str, Claim],
) -> dict[str, list[Slice]]:
    """纯函数：同样的声明集合必定得到同样的片事实（与输入到达顺序无关）。"""
    return {
        r: build_slices_for_resource(r, capacities, reservations)
        for r in all_resources(capacities, reservations)
    }


def rebuild_overlap(
    previous: dict[str, list[Slice]],
    capacities: dict[str, Claim],
    reservations: dict[str, Claim],
    changed_capacity_keys: Iterable[str],
) -> dict[str, list[Slice]]:
    """容量修订的局部重建：只重跑受影响资源上、与变更声明区间交叠的范围。

    未受影响的片原样保留（其内容哈希身份本就不变）；受影响范围用最新声明全量重切，
    输出与 build_all_slices 全量重建逐片相等，但复杂度只与交叠范围相关。
    键被整体删除时无法从新声明定位旧区间，显式报错并要求全量重建，杜绝静默漏切。
    """
    affected: dict[str, list[tuple[int, int]]] = {}
    for key in changed_capacity_keys:
        claim = capacities.get(key)
        if claim is None:
            raise ValueError(
                f"容量键 {key} 在新声明中不存在，无法定位旧区间；请对该资源执行全量重建"
            )
        affected.setdefault(claim.resource, []).append((claim.start, claim.end))

    result: dict[str, list[Slice]] = {}
    for resource, old_slices in previous.items():
        ranges = affected.get(resource)
        if not ranges:
            result[resource] = old_slices  # 零拷贝保留，片身份不变
            continue
        lo_min = min(lo for lo, _ in ranges)
        hi_max = max(hi for _, hi in ranges)
        untouched = [s for s in old_slices if not (s.start < hi_max and s.end > lo_min)]
        fresh = build_slices_for_resource(resource, capacities, reservations)
        rebuilt = [s for s in fresh if s.start < hi_max and s.end > lo_min]
        result[resource] = sorted(untouched + rebuilt, key=lambda s: (s.start, s.end))
    return result


# ---------------------------------------------------------------------------
# 稳定片身份：内容哈希
# ---------------------------------------------------------------------------


def slice_identity(slc: Slice) -> str:
    payload = {
        "resource": slc.resource,
        "start": slc.start,
        "end": slc.end,
        "capacity": None if slc.capacity_shares is None else str(slc.capacity_shares),
        "allocations": [
            {"job": j, "reservation": k, "shares": str(s)} for (j, k, s) in slc.allocations
        ],
        "domain": slc.domain,
        "lineage": list(slc.lineage_keys),
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"slc_{digest[:16]}"


# ---------------------------------------------------------------------------
# 上海自然日切边界
# ---------------------------------------------------------------------------


def shanghai_day_parts(start: int, end: int, origin_epoch: int = 0) -> list[tuple[int, int, str]]:
    """把 UTC epoch 区间按 Asia/Shanghai 自然日切成 (lo, hi, 'YYYY-MM-DD')。

    合成数据用相对秒（time_origin 起算），origin_epoch 把相对秒锚到真实时刻后再判日；
    生产环境直接给绝对 epoch 时 origin_epoch=0 即可。UTC+8 无 DST，仍走 zoneinfo。
    """
    parts: list[tuple[int, int, str]] = []
    lo = start
    while lo < end:
        midnight = datetime.fromtimestamp(origin_epoch + lo, SHANGHAI).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        next_midnight = midnight + timedelta(days=1)
        # 边界换算回相对秒
        hi = min(end, int(next_midnight.timestamp()) - origin_epoch)
        parts.append((lo, hi, midnight.date().isoformat()))
        lo = hi
    return parts


# ---------------------------------------------------------------------------
# 聚合：作业粒度（分配份额秒）与资源粒度（容量秒）严格分离
# ---------------------------------------------------------------------------


def aggregate(slices_by_resource: dict[str, list[Slice]], origin_epoch: int = 0):
    """allocation_facts: resource x day x job（含 reservation 键与域）
    capacity_facts:   resource x day，容量/闲置/超配/各域时长只记一次，不随作业复制。

    分配事实带 domain：消费方可分别取得“全量份额秒”与“已知容量域份额秒”，
    未知容量域的分配照实保留（不丢、不算闲置、不算零用量）。
    """
    allocation_facts: list[dict] = []
    cap_index: dict[tuple[str, str], dict] = {}

    for resource in sorted(slices_by_resource):
        for slc in slices_by_resource[resource]:
            for lo, hi, day in shanghai_day_parts(slc.start, slc.end, origin_epoch):
                dur = hi - lo
                row = cap_index.get((resource, day))
                if row is None:
                    row = {
                        "resource": resource,
                        "day": day,
                        "capacity_seconds": _ZERO,
                        "allocated_seconds": _ZERO,
                        "allocated_seconds_known_domain": _ZERO,
                        "idle_seconds": _ZERO,
                        "overcommit_seconds": _ZERO,
                        "main_domain_seconds": 0,
                        "anomaly_domain_seconds": 0,
                        "unknown_domain_seconds": 0,
                    }
                    cap_index[(resource, day)] = row
                # 全量分配秒：跨所有域（含未知域），等于同日作业粒度分配秒之和
                row["allocated_seconds"] += slc.allocated * dur
                if slc.capacity_shares is not None:  # 仅已知域产生容量/闲置/超配与恒等式项
                    row["capacity_seconds"] += slc.capacity_shares * dur
                    row["allocated_seconds_known_domain"] += slc.allocated * dur
                    row["idle_seconds"] += slc.idle * dur
                    row["overcommit_seconds"] += slc.overcommit * dur
                row[f"{slc.domain}_domain_seconds"] += dur

                for job, key, shares in slc.allocations:
                    allocation_facts.append(
                        {
                            "resource": resource,
                            "day": day,
                            "job": job,
                            "reservation": key,
                            "domain": slc.domain,
                            "allocation_seconds": shares * dur,
                        }
                    )

    allocation_facts.sort(key=lambda r: (r["resource"], r["day"], r["job"], r["reservation"]))
    capacity_facts = sorted(cap_index.values(), key=lambda r: (r["resource"], r["day"]))
    return allocation_facts, capacity_facts


# ---------------------------------------------------------------------------
# 守恒检查：原始时长、份额秒逐键对回输入，域恒等式残差必须为 0
# ---------------------------------------------------------------------------


def conservation_check(
    slices_by_resource: dict[str, list[Slice]],
    capacities: dict[str, Claim],
    reservations: dict[str, Claim],
) -> dict:
    input_seconds: dict[str, Fraction] = {}
    for key in sorted(reservations):
        c = reservations[key]
        input_seconds[key] = _ZERO if c.withdrawn else c.shares * (c.end - c.start)
    for key in sorted(capacities):
        c = capacities[key]
        input_seconds[key] = (
            _ZERO
            if c.withdrawn or c.shares is None
            else c.shares * (c.end - c.start)
        )

    slice_seconds: dict[str, Fraction] = {k: _ZERO for k in input_seconds}
    domain_duration = {"main": 0, "anomaly": 0, "unknown": 0}
    domain_capacity = {"main": _ZERO, "anomaly": _ZERO, "unknown": _ZERO}
    identity_residual = _ZERO  # 容量 - 分配 - 闲置 + 超配，已知域内恒为 0

    for resource in sorted(slices_by_resource):
        for slc in slices_by_resource[resource]:
            dur = slc.duration
            domain_duration[slc.domain] += dur
            for _job, key, shares in slc.allocations:
                slice_seconds[key] = slice_seconds.get(key, _ZERO) + shares * dur
            if slc.capacity_shares is not None:
                cap_key = next(k for k in slc.lineage_keys if k in capacities)
                slice_seconds[cap_key] = (
                    slice_seconds.get(cap_key, _ZERO) + slc.capacity_shares * dur
                )
                domain_capacity[slc.domain] += slc.capacity_shares * dur
                identity_residual += (
                    slc.capacity_shares * dur
                    - slc.allocated * dur
                    - slc.idle * dur
                    + slc.overcommit * dur
                )

    mismatches = {
        k: {"input_seconds": str(input_seconds[k]), "slice_seconds": str(slice_seconds.get(k, _ZERO))}
        for k in sorted(input_seconds)
        if input_seconds[k] != slice_seconds.get(k, _ZERO)
    }
    return {
        "reconciled_per_claim": not mismatches,
        "claim_seconds_mismatch": mismatches,
        "domain_duration_seconds": domain_duration,
        "domain_capacity_seconds": {k: str(v) for k, v in domain_capacity.items()},
        "identity_residual": str(identity_residual),
        "identity_holds": identity_residual == _ZERO,
    }


# ---------------------------------------------------------------------------
# 序列化：精确有理数字符串 + Decimal 定点视图 + 片级原声明血缘
# ---------------------------------------------------------------------------


def _q(value: Optional[Fraction]) -> Optional[str]:
    return None if value is None else str(value)


def slices_to_records(
    slices_by_resource: dict[str, list[Slice]],
    capacities: dict[str, Claim],
    reservations: dict[str, Claim],
) -> list[dict]:
    records = []
    for resource in sorted(slices_by_resource):
        for slc in slices_by_resource[resource]:
            cap_claim = next(
                (k for k in slc.lineage_keys if k in capacities and not capacities[k].withdrawn),
                None,
            )
            res_keys = [k for (_j, k, _s) in slc.allocations]
            records.append(
                {
                    "slice_id": slice_identity(slc),
                    "resource": slc.resource,
                    "start_epoch": slc.start,
                    "end_epoch": slc.end,
                    "duration_seconds": slc.duration,
                    "domain": slc.domain,
                    "capacity_shares": _q(slc.capacity_shares),
                    "allocated_shares": str(slc.allocated),
                    "idle_shares": _q(slc.idle),
                    "overcommit_shares": str(slc.overcommit),
                    "allocations": [
                        {
                            "job": job,
                            "reservation": key,
                            "shares": str(shares),
                            "share_seconds": str(shares * slc.duration),
                        }
                        for (job, key, shares) in slc.allocations
                    ],
                    "lineage": {
                        "capacity_claim": cap_claim,
                        "capacity_revision": capacities[cap_claim].revision if cap_claim else None,
                        "capacity_source_line": (
                            capacities[cap_claim].source_line if cap_claim else None
                        ),
                        "reservation_claims": res_keys,
                        "reservation_revisions": {
                            k: reservations[k].revision for k in res_keys
                        },
                        "reservation_source_lines": {
                            k: reservations[k].source_line for k in res_keys
                        },
                        "all_active_claims": list(slc.lineage_keys),
                    },
                }
            )
    return records


def decimal_view(records: list[dict], quant: str = "0.000001") -> list[dict]:
    """数仓 DECIMAL 列的定点视图，仅用于展示/落库；守恒判定一律以有理数原文为准。"""
    from decimal import Decimal, getcontext

    getcontext().prec = 40
    unit = Decimal(quant)

    def d(raw):
        # 有理数字符串（含 "3/5"）Decimal 不能直接解析，先经 Fraction 精确转换再定点化
        if raw is None:
            return None
        frac = Fraction(raw)
        return (Decimal(frac.numerator) / Decimal(frac.denominator)).quantize(unit)

    view = []
    for r in records:
        view.append(
            {
                **r,
                "capacity_shares_decimal": str(d(r["capacity_shares"])),
                "allocated_shares_decimal": str(d(r["allocated_shares"])),
                "idle_shares_decimal": str(d(r["idle_shares"])),
                "overcommit_shares_decimal": str(d(r["overcommit_shares"])),
                "allocations": [
                    {
                        **a,
                        "shares_decimal": str(d(a["shares"])),
                        "share_seconds_decimal": str(d(a["share_seconds"])),
                    }
                    for a in r["allocations"]
                ],
            }
        )
    return view


def _stringify_facts(allocation_facts: list[dict], capacity_facts: list[dict]):
    a = [{**r, "allocation_seconds": str(r["allocation_seconds"])} for r in allocation_facts]
    c = [
        {
            **r,
            "capacity_seconds": str(r["capacity_seconds"]),
            "allocated_seconds": str(r["allocated_seconds"]),
            "allocated_seconds_known_domain": str(r["allocated_seconds_known_domain"]),
            "idle_seconds": str(r["idle_seconds"]),
            "overcommit_seconds": str(r["overcommit_seconds"]),
        }
        for r in capacity_facts
    ]
    return a, c


# ---------------------------------------------------------------------------
# 分区存储与多资源并行发布边界
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, payload: str) -> str:
    """同目录临时文件 + os.replace：发布边界是单次原子 rename，读者绝不会看到半文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp") as f:
        tmp = Path(f.name)
        f.write(payload)
    os.replace(tmp, path)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _partition_worker(task: tuple) -> tuple[str, str, int]:
    """模块级 worker：ProcessPoolExecutor 要求可 pickle。每资源独立、无共享写状态。"""
    resource, records, partition_root = task
    payload = json.dumps(records, ensure_ascii=False, indent=2, sort_keys=True)
    digest = _atomic_write(
        Path(partition_root) / f"resource={resource}" / "slice_facts.json", payload
    )
    return resource, digest, len(records)


def publish_partitions(
    slices_by_resource: dict[str, list[Slice]],
    capacities: dict[str, Claim],
    reservations: dict[str, Claim],
    out_dir: str | Path,
    max_workers: int = 1,
    origin_epoch: int = 0,
) -> dict:
    """按资源分区发布。

    并行语义：资源之间无共享状态，worker 只写自己的 resource=<r>/slice_facts.json 临时文件；
    每个分区独立 os.replace 原子发布；manifest 最后原子写入，读者以 manifest 为一致性快照边界，
    看不到 manifest 即视为本次发布未完成，继续读上一版 manifest。
    """
    out = Path(out_dir)
    partition_root = out / "slice_facts"
    jobs = sorted(slices_by_resource)
    tasks = [
        (
            r,
            slices_to_records({r: slices_by_resource[r]}, capacities, reservations),
            str(partition_root),
        )
        for r in jobs
    ]

    if max_workers > 1 and len(tasks) > 1:
        with ProcessPoolExecutor(max_workers=max_workers) as pool:
            done = list(pool.map(_partition_worker, tasks))
    else:
        done = [_partition_worker(t) for t in tasks]

    allocation_facts, capacity_facts = aggregate(slices_by_resource, origin_epoch)
    a_str, c_str = _stringify_facts(allocation_facts, capacity_facts)
    files = {
        "allocation_facts.json": _atomic_write(
            out / "allocation_facts.json",
            json.dumps(a_str, ensure_ascii=False, indent=2, sort_keys=True),
        ),
        "capacity_facts.json": _atomic_write(
            out / "capacity_facts.json",
            json.dumps(c_str, ensure_ascii=False, indent=2, sort_keys=True),
        ),
    }
    manifest = {
        "partitions": {
            r: {
                "path": f"slice_facts/resource={r}/slice_facts.json",
                "sha256": digest,
                "slice_count": count,
            }
            for (r, digest, count) in sorted(done)
        },
        "files": {k: {"sha256": v} for k, v in sorted(files.items())},
    }
    # manifest 最后发布：它的 rename 是整次发布的提交点
    manifest["manifest_sha256"] = _atomic_write(
        out / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
    )
    return manifest


# ---------------------------------------------------------------------------
# 端到端入口
# ---------------------------------------------------------------------------


def read_csv_dicts(path: str | Path) -> list[dict]:
    with Path(path).open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def build_from_rows(cap_rows: Iterable[dict], res_rows: Iterable[dict], config: Optional[dict] = None):
    origin_epoch = 0
    if config and config.get("time_origin"):
        origin_epoch = parse_epoch(config["time_origin"])
    capacities, reservations = load_claims(cap_rows, res_rows)
    slices_by_resource = build_all_slices(capacities, reservations)
    records = slices_to_records(slices_by_resource, capacities, reservations)
    allocation_facts, capacity_facts = aggregate(slices_by_resource, origin_epoch)
    a_str, c_str = _stringify_facts(allocation_facts, capacity_facts)
    report = conservation_check(slices_by_resource, capacities, reservations)
    return {
        "config": config,
        "origin_epoch": origin_epoch,
        "claims": {"capacity": capacities, "reservation": reservations},
        "slices_by_resource": slices_by_resource,
        "slice_facts": records,
        "allocation_facts": a_str,
        "capacity_facts": c_str,
        "conservation": report,
    }


def run(capacity_csv: str | Path, reservation_csv: str | Path, config_json: str | Path):
    config = json.loads(Path(config_json).read_text(encoding="utf-8"))
    return build_from_rows(read_csv_dicts(capacity_csv), read_csv_dicts(reservation_csv), config)


def write_outputs(result: dict, out_dir: str | Path) -> dict[str, Path]:
    out = Path(out_dir)
    paths = {}
    for name in ("slice_facts", "allocation_facts", "capacity_facts", "conservation"):
        payload = json.dumps(result[name], ensure_ascii=False, indent=2, sort_keys=True)
        p = out / f"{name}.json"
        _atomic_write(p, payload)
        paths[name] = p
    return paths


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="容量时间片构建器：切片、聚合、守恒检查并原子落盘")
    parser.add_argument("capacity_csv")
    parser.add_argument("reservation_csv")
    parser.add_argument("config_json")
    parser.add_argument("--out", default=".workspace-output/run", help="输出目录")
    parser.add_argument("--workers", type=int, default=1, help="多资源并行 worker 数")
    args = parser.parse_args(argv)

    result = run(args.capacity_csv, args.reservation_csv, args.config_json)
    paths = write_outputs(result, args.out)
    manifest = publish_partitions(
        result["slices_by_resource"],
        result["claims"]["capacity"],
        result["claims"]["reservation"],
        Path(args.out) / "partitions",
        max_workers=args.workers,
        origin_epoch=result["origin_epoch"],
    )
    summary = {
        "resources": len(result["slices_by_resource"]),
        "slices": len(result["slice_facts"]),
        "reconciled_per_claim": result["conservation"]["reconciled_per_claim"],
        "identity_holds": result["conservation"]["identity_holds"],
        "domain_duration_seconds": result["conservation"]["domain_duration_seconds"],
        "outputs": {k: str(v) for k, v in paths.items()},
        "manifest_sha256": manifest["manifest_sha256"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not (summary["reconciled_per_claim"] and summary["identity_holds"]):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
