# 历史增长：有界的历史遍历与快照策略

2026-10-18。处理 [S4/S5](STAGING_S4_S5.md) 第 2 节测出的随历史长度增长的成本。预发布虚拟机上 main 有 2,000 个提交时，一次 10 行合入耗时 50.7 s，提交 499 ms，head 读取 1.2 s。改动在分支 `perf/history-bounded-costs`，迁移为 [`sql/005_bounded_history.sql`](../sql/005_bounded_history.sql)。所有数字来自本机现有的 Ubuntu WSL PostgreSQL 16.15，客户端与服务器在同一台笔记本上，只作前后对比。**预发布虚拟机上尚未重跑；未使用任何云服务。**

## 1. 原因（在 1,000 个提交的库上实测）

库为基线增长测试保留的 `agentdb_test_growth_67afbbec40`，含 1,020 个 changeset。

- 所有递归遍历都写成 `lineage l JOIN vc.parent p ON p.child=l.id`。规划器对递归步选择哈希连接，每走一步都要全表扫描一次 `vc.parent`（`EXPLAIN ANALYZE`：Seq Scan on parent，loops=1011）。因此单次遍历与历史长度的平方成正比。对 1,000 个提交走一遍第一父链需要 106 ms；同一遍历若每步按索引查找，只需 2.4 ms。`_ancestors`、`rows_at` 和 `_file_state` 都是这种写法。
- `vc._file_state` 先走到根，再按深度取最近的那条记录，找到路径后也不会提前停止。`write_file` 每次调用一次；`vc.merge` 对历史上出现过的**每个**路径调用三次。
- `vc.merge` 遍历目标的全部祖先两次（一次判断是否已合入，一次在 `merge_base` 内），遍历来源的全部祖先一次。
- `rows_at` 回放自最近快照以来的全部提交，而没有任何机制自动物化快照。

## 2. 改动

全部为 `CREATE OR REPLACE`：签名、返回值、错误文本与 SQLSTATE、所有者、SECURITY DEFINER、search_path 和授权都不变。新安装在 001–004 之后执行 005；旧安装由数据库所有者执行 `python db.py install-bounded-history`，可以重复执行。`001_core.sql` 中的同名函数是旧定义，以 005 为准。

1. **父边逐步走索引**（`_ancestors`、`rows_at`、`_file_state`、`snapshot_due`）：递归步改为 `CROSS JOIN LATERAL (SELECT p.parent FROM vc.parent p WHERE p.child=… OFFSET 0)`。`OFFSET 0` 阻止子查询被展开成普通连接，每步因此是一次参数化的索引探测，不依赖统计信息。遍历的停止条件与输出集合不变。
2. **`vc._file_state` 找到即停**：递归时为每个 changeset 标记“是否记录了该路径”，在第一个记录了该路径的第一父祖先处停止，取该处的状态。从未被任何 changeset 记录的路径不做遍历，直接返回 NULL。返回值与原来一致：删除仍为 NULL，可执行标记仍取自那条记录。提交的成本因此取决于该路径上次改动距今多远，而不是全部历史长度。
3. **`vc.merge` 减少重复工作**（冲突规则与判定顺序不变：先逐表处理行，再逐路径处理文件；每个路径仍依次比较共同祖先与来源、目标与来源、目标与共同祖先）：
   - 目标的祖先只遍历一次，同时用于“来源已合入”的判断和共同祖先计算。新增内部函数 `vc._merge_base(祖先数组, b)`，`vc.merge_base` 委托给它，语义不变：唯一的最大公共祖先，否则报 `A unique merge base is required`。
   - 文件状态沿第一父链求值。合入时把共同祖先与来源的第一父链同步向下走，直到其中一方走到另一方已经走过的 changeset。从该点往下两条链完全相同；在该点之上两条链都没有记录过的路径，两边状态必然相同，原循环的第一项比较也会跳过它。因此只需检查在该点之上记录过的路径。这一点只依赖第一父链的定义，不依赖历史是如何产生的。成本取决于分歧的长度，不取决于历史长度。
   - 路径按路径名排序检查。原循环的顺序来自无序的 `SELECT DISTINCT`（哈希聚合），并无定义。只有同时存在多个文件冲突时，报错中写哪个路径才会受影响：错误码与回滚相同，报出的仍是冲突路径之一。
4. **快照策略** `vc.snapshot_due(branch DEFAULT 'main', interval DEFAULT 100)`：只读，最多回看 interval 个提交。如果某张已登记表在分支 head 上读取需要回放至少 interval 个提交，就返回（head, table_id）。只授权给 `agentdb_manager`，`agentdb_client` 与 PUBLIC 不可执行。执行器 `SnapshotMaintenance.run_due(actor, branch, interval, policy_ref)` 对每个目标调用已有的可恢复 `run()`。任务号为 `snapshot-<revision>-<table_id>`，重复执行只会核验，不会重复物化。快照仍是派生数据：
   - 输入版本：策略执行时的分支 head；
   - 完成条件：`vc.snapshot_status(...).snapshot_exists`，即 `snapshot_manifest` 中有该行；
   - 重建方式：由历史经 `vc.materialize`（即 `rows_at` 回放）重新生成；
   - 读取结果不依赖快照是否存在（第 4 节核验）。

   按每 10 个提交执行一次计算，head 读取最多回放约 interval + 10 个提交。代价是每次物化 4,000 行（本机约 50–65 ms，在提交之外进行），快照行增加存储：1,000 个提交、interval 100 时库从 21.7 MB 增至 33.4 MB。快照表与历史表一样不可修改，尚无回收机制。
5. 文件清单式快照（按快照点保存完整文件树）没有实现。找到即停之后，单次 `_file_state` 沿第一父链每步约 2 µs：例如对只在 seed 中写过、1,000 个提交未改的 `src/app.py`，读取一次 1.9 ms。若以后出现大量长期不改的文件和很长的历史，再考虑这一方案。

## 3. 前后对比

### 增长测试（`staging_drills.py growth --config connection.local.json --levels 0 100 1000`）

每个增长提交改 10 行商品，并写 50 个文件之一；合入为 3 次的中位，提交为最后 10 个提交的中位，读取为 `EXPLAIN ANALYZE` 5 次的中位。原始结果在 [local-results/history-growth/](../local-results/history-growth/)。测试复制到本地时，staging 的 `growth.json` 未被覆盖。结果文件中关于“TLS 连接虚拟机”的说明是脚本的固定文字，本次实际连接的是本机。

| 提交数 | 合入 前 → 后（无策略）→ 后（策略 100） | 提交 前 → 后 → 后（策略） | head 全表读取 前 → 后 → 后（策略） |
|---|---|---|---|
| 0 | 36.7 → 45.2 → 46.5 ms | — | 7.0 → 6.4 → 6.2 ms |
| 100 | 137.7 → 62.8 → 33.5 ms | 4.8 → 5.1 → 5.1 ms | 11.9 → 11.9 → 5.1 ms |
| 1,000 | **6,521 → 160 → 42.6 ms** | **58.4 → 5.5 → 6.3 ms** | **113.6 → 44.4 → 6.3 ms** |

- 提交不再随历史增长（改动 1、2）。
- 不执行策略时，合入与 head 读取仍随历史增长，但已是线性且廉价：`rows_at` 回放自 seed 快照以来的全部提交，合入要调用 3 次 `rows_at`。执行策略后，二者在 0 到 1,000 个提交之间保持平稳（合入 33–47 ms 之间的差异属于单机计时波动）。100 到 1,000 个提交之间策略运行 9 次，每次物化两张表（商品与订单），中位 65 ms。
- 仍与历史长度成正比的部分：合入时目标与来源的全部祖先遍历。现在每个提交约 1–2 µs，1,000 个提交时整个 `merge_base` 约 5 ms（原来 104 ms）。若要完全消除，需要代数（generation number）这类派生索引，目前没有负载依据，暂不实现。`vc.history` 仍是旧的连接写法，但它最多遍历 limit 步，本次未改。

### 函数级（1,000 个提交的基线库，在事务中替换后回滚）

| 测量 | 旧 | 新 |
|---|---|---|
| 25 个随机版本 × 52 个路径的 `_file_state`（1,300 次） | 27,379 ms | 90 ms |
| 25 个版本的 `_ancestors` | 643 ms | 24 ms |
| 25 个版本 × 2 张表的 `rows_at` | 1,365 ms | 657 ms |
| `merge_base(head, head 的父)` | 104 ms | 5.1 ms |
| `vc.snapshot_due('main',100)` | — | 7.5 ms |

三组读取的结果逐项相同（1,300 个文件状态、25 个祖先集合、50 个表状态哈希）。

### 原有套件（迁移前后都在本机新建测试库上运行）

| 套件 | 前 | 后 |
|---|---|---|
| `python run_tests.py`：18 项原断言 + 并发、会话、合入语义 | 全部通过（`agentdb_test_06f5945e3a67`） | 全部通过（`agentdb_test_ff98e378fcd4`） |
| `review_local.py`：受限角色、两个中断窗口、十个分支全部合入 | 通过；合入阶段 575 ms，45 次重试 | 通过；合入阶段 531 ms，45 次重试 |
| `check_merge_migration.py`（004 的迁移核验） | 通过，13 个情形相同 | 通过，13 个情形相同 |
| `unittest discover -s tests` | — | 75 项通过（含新增的快照策略测试） |

## 4. 迁移核验：`check_history_migration.py`

[`check_history_migration.py`](../check_history_migration.py)，结果见 [`history-migration-results.json`](../history-migration-results.json)（库 `agentdb_test_histmig_2b0e3f493e`、`agentdb_test_histreg_2b0e3f493e`、`agentdb_test_histnew_2b0e3f493e`）。

- 用提交 0bf7309 的 001–004 建两个旧版库，安装方式与当时的 `Connection.install` 相同。
  - `histreg` 库运行 18 项原断言和 13 个行合入情形。这些断言假定库中只有 seed（例如只有 1 个 blob），所以单独建库。
  - `histmig` 库提交一段真实形态的历史：main 上 24 个提交，改动、删除、恢复文件并切换可执行标记，同时改行；表快照建在中途；分支有文件和行；main 合入分支后再合回 main；有分支的分支；还有 criss-cross（两个合并基）。
- 迁移前记录 1,991 项读取结果：每个版本 × 每个路径的 `_file_state` 和 `read_file`；每条父边及各分支 head 两两之间的 `diff_files`；每条父边的 `diff_rows`；每个版本的 `_ancestors` 与 `rows_at`；各 head 与每个版本之间的 `merge_base`，包括报错。另在回滚的事务中运行 17 个文件合入情形：单边修改、双边修改不同或相同、一边删除一边修改、双边删除、仅改可执行标记、多个文件冲突、行已应用后文件冲突整体回滚、合入分支、长期分支先合入 main 再合回、已合入、criss-cross、来源不存在、来源是目标的后代。
- 执行 `install_bounded_history` 两次后：
  - 业务数据与历史表的哈希不变；被替换函数的所有者、SECURITY DEFINER、配置、ACL 和执行权限不变；
  - 迁移后的函数与新安装（`histnew`）逐项相同，包括函数体哈希；
  - 1,991 项读取与 30 个合入结果（17 个文件、13 个行）与旧函数相同；
  - 18 项原断言再次通过；`agentdb_client` 仍能合入。
  - 多文件冲突的报错，新旧均为 `File conflict: hist/f0.txt`。比较时仍按“冲突路径之一”规范化，因为旧顺序没有定义。
- 快照策略以 `agentdb_manager` 身份、经 `SnapshotMaintenance.run_due`（MemoryStore，仅测试用）运行，interval 为 5：两张表各物化一次；第二次执行时没有目标。之后再比较全部读取与文件合入情形，结果仍相同。

## 5. 剩余限制与后续

- 预发布虚拟机上的 S4 增长测试尚未用新函数重跑：需要先在虚拟机的测试库上执行迁移，再运行 `staging_drills.py growth --snapshot-interval 100`。
- 策略需要有执行器定期运行；当前 `maintain_snapshot.py` 只处理单个目标，`run_due` 还没有命令行入口，也没有常驻调度。云端状态仍未经真实验证。
- 祖先遍历仍与历史长度线性相关（见第 3 节）；快照没有回收机制。
