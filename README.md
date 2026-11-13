# 共享芯片容量的原子时间片事实建模

本项目保存虚构算力设施的仓库源表、湖文件与Python读取工具。现有工具仅加载输入并显示记录数，不执行事实加工、目录提交、汇总或联接。所有设备、组织和金额均为合成。

## 字段与单位

start/end为UTC秒数左闭右开区间，shares字段用有理数字符串表示。rev按reservation或capacity键比较，withdrawn撤回整条声明，缺容量与0容量不同。

material-index.json记录全部输入内容哈希；fixtures保存原始资料与配置，计算输出可放.workspace-output，原始资料不应被覆盖。

## 运行已有读取器

Python 3.11以上，标准库即可，不需安装第三方依赖。

    python -m src.capacity_reader fixtures/capacity.csv
    python -m compileall -q src tests
    python -m unittest discover -s tests -v

处理不可信输入目录时用隔离模式（脚本路径方式，`-I` 不把 cwd 放进 sys.path）：

    python -I src/slice_builder.py fixtures/capacity.csv fixtures/reservations.csv fixtures/slices.json --out .workspace-output/run
    python -I demo/run_demo.py

## 容量时间片构建器（src/slice_builder.py）

只做区间代数与聚合，不运行任何作业调度。

### 语义契约
- 区间左闭右开；时间在解析层统一转 UTC epoch 秒，带偏移的 ISO-8601（含 DST 来源）先归一化。合成数据的相对秒由 `slices.json.time_origin` 锚到真实时刻后再判上海自然日。
- 份额用 `fractions.Fraction` 精确运算，落盘为有理数字符串（如 `3/5`），另给 `decimal_view` 的 DECIMAL 定点视图；守恒判定只认有理数原文。
- 每个键按最高 revision 取最新声明，`withdrawn=True` 撤回后该键不再贡献；同 revision 内容冲突拒绝、同值重放幂等；乱序到达与分批 `merge_claim_sets` 等价于一次到齐。
- 原子片由全部端点切出。**缺容量是 `unknown` 域（capacity_shares=null，闲置/超配不可计算）；显式 `0` 容量是已知 0**，二者严格区分。
- 分配总和超过容量的片隔离为 `anomaly` 域，原始份额秒原样保留，绝不成比例缩减；`main`/`anomaly`/`unknown` 三域时长与份额秒由 `conservation_check` 逐键对回输入，域恒等式 `容量秒 = 分配秒 + 闲置秒 − 超配秒` 残差必须为 0。
- 片身份 `slice_id` 是片内容的 sha256 前缀，不依赖文件顺序；乱序、分批、跨进程重启产生同一组片身份。
- 容量修订用 `rebuild_overlap` 只重建交叠范围，非交叠片零改动且与全量重建逐片相等；键被删除无法定位旧区间时显式报错要求全量重建。

### 两种事实粒度（防止容量被复制相加）
- `allocation_facts`：resource × day × job（带 reservation 键与 domain），承载**分配份额秒**；同一卡在多个作业下只出现各自的份额，不含容量。
- `capacity_facts`：resource × day 一行，承载**资源可用容量秒**、闲置秒、超配秒与各域时长；`allocated_seconds` 为跨域全量分配秒（等于同日作业粒度之和），`allocated_seconds_known_domain` 仅统计已知容量域。容量秒绝不按作业复制。

### 片级血缘与落盘
每片 `lineage` 记录容量/分配声明键、revision 与 CSV 源行号。`publish_partitions` 按 `resource=<r>/` 分区：

- 资源间无共享状态，可用 `ProcessPoolExecutor`（`--workers`）并行，worker 只写自己分区的临时文件；
- 每个分区 `os.replace` 原子发布，`manifest.json` 最后原子写入并作为整次发布的提交点——读者看不到新 manifest 即视为未完成，继续读上一版；
- manifest 记录各分区 sha256，同输入重发得到相同 manifest 哈希。

### 演示场景
`demo/run_demo.py` 覆盖题面主示例（闲置/恰满/未知三段，A=6、B 已知域 2、闲置 2、容量 10）、超配隔离、撤回与同作业多 reservation 键、跨上海午夜、恰好相接，并断言乱序/分批/局部重建/跨进程的确定性。

## 固定容器

    docker build -t capacity-slices:local .
    docker run --rm capacity-slices:local

容器镜像以完整digest固定，默认仅检查输入契约；实现新增分析功能所需依赖由开发者明确登记。
