中文 | [English](#english-version)

# 历史增长：有界的历史遍历与快照策略

> 注（2026-10-30）：云存储（GCS、OSS、S3）、HTTP 状态服务与 ECS 部署已移除，代码和专门文档留在 git 历史中（最后存在于提交 `d342ccf`）；状态引擎只保留内存后端。下文按当时情况保留，其中提到的这些文件已不存在。

2026-10-18。处理 [S4/S5](STAGING_S4_S5.md) 第 2 节测出的随历史长度增长的成本。预发布虚拟机上 main 有 2,000 个提交时，一次 10 行合入耗时 50.7 s，提交 499 ms，head 读取 1.2 s。改动在分支 `perf/history-bounded-costs`，迁移为 [`sql/005_bounded_history.sql`](../sql/005_bounded_history.sql)。所有数字来自本机现有的 Ubuntu WSL PostgreSQL 16.15，客户端与服务器在同一台笔记本上，只作前后对比。**预发布虚拟机上尚未重跑；未使用任何云服务。**

## 1. 原因（在 1,000 个提交的库上实测）

库为基线增长测试保留的 `agentdb_test_growth_67afbbec40`，含 1,020 个 changeset。

- 所有递归遍历都写成 `lineage l JOIN vc.parent p ON p.child=l.id`。规划器对递归步选择哈希连接，每走一步都要全表扫描一次 `vc.parent`（`EXPLAIN ANALYZE`：Seq Scan on parent，loops=1011）。因此单次遍历与历史长度的平方成正比。对 1,000 个提交走一遍第一父链需要 106 ms；同一遍历若每步按索引查找，只需 2.4 ms。`_ancestors`、`rows_at` 和 `_file_state` 都是这种写法。
- `vc._file_state` 先走到根，再按深度取最近的那条记录，找到路径后也不会提前停止。`write_file` 每次调用一次；`vc.merge` 对历史上出现过的**每个**路径调用三次。
- `vc.merge` 遍历目标的全部祖先两次（一次判断是否已合入，一次在 `merge_base` 内），遍历来源的全部祖先一次。
- `rows_at` 回放自最近快照以来的全部提交，而没有任何机制自动物化快照。

## 2. 改动

全部为 `CREATE OR REPLACE`：签名、返回值、错误文本与 SQLSTATE、所有者、SECURITY DEFINER 和授权都不变；search_path 由 `pg_catalog, vc` 改为 `pg_catalog, vc, pg_temp`（与 007 相同，见 [SETUP](SETUP.md)）。新安装在 001–004 之后执行 005；旧安装由数据库所有者执行 `python db.py install-bounded-history`，可以重复执行。`001_core.sql` 中的同名函数是旧定义，以 005 为准。

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

### 增长测试（`scripts/staging_drills.py growth --config connection.local.json --levels 0 100 1000`）

每个增长提交改 10 行商品，并写 50 个文件之一；合入为 3 次的中位，提交为最后 10 个提交的中位，读取为 `EXPLAIN ANALYZE` 5 次的中位。原始结果在 [results/history-growth/](../results/history-growth/)。测试复制到本地时，staging 的 `growth.json` 未被覆盖。结果文件中关于“TLS 连接虚拟机”的说明是脚本的固定文字，本次实际连接的是本机。

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
| `scripts/check_merge_migration.py`（004 的迁移核验） | 通过，13 个情形相同 | 通过，13 个情形相同 |
| `unittest discover -s tests` | — | 75 项通过（含新增的快照策略测试） |

## 4. 迁移核验：`scripts/check_history_migration.py`

[`scripts/check_history_migration.py`](../scripts/check_history_migration.py)，结果见 [`results/history-migration-results.json`](../results/history-migration-results.json)（库 `agentdb_test_histmig_c82e056feb`、`agentdb_test_histreg_c82e056feb`、`agentdb_test_histnew_c82e056feb`；2026-10-21 在 005 的定义加入 `pg_temp` 后重跑，结论除 search_path 外与首次运行（库后缀 `2b0e3f493e`）相同，首次运行的结果文件已被覆盖）。

- 用提交 0bf7309 的 001–004 建两个旧版库，安装方式与当时的 `Connection.install` 相同。
  - `histreg` 库运行 18 项原断言和 13 个行合入情形。这些断言假定库中只有 seed（例如只有 1 个 blob），所以单独建库。
  - `histmig` 库提交一段真实形态的历史：main 上 24 个提交，改动、删除、恢复文件并切换可执行标记，同时改行；表快照建在中途；分支有文件和行；main 合入分支后再合回 main；有分支的分支；还有 criss-cross（两个合并基）。
- 迁移前记录 1,991 项读取结果：每个版本 × 每个路径的 `_file_state` 和 `read_file`；每条父边及各分支 head 两两之间的 `diff_files`；每条父边的 `diff_rows`；每个版本的 `_ancestors` 与 `rows_at`；各 head 与每个版本之间的 `merge_base`，包括报错。另在回滚的事务中运行 17 个文件合入情形：单边修改、双边修改不同或相同、一边删除一边修改、双边删除、仅改可执行标记、多个文件冲突、行已应用后文件冲突整体回滚、合入分支、长期分支先合入 main 再合回、已合入、criss-cross、来源不存在、来源是目标的后代。
- 执行 `install_bounded_history` 两次后：
  - 业务数据与历史表的哈希不变；被替换函数的所有者、SECURITY DEFINER、ACL 和执行权限不变，search_path 末尾加上 `pg_temp`、其余配置不变；
  - 迁移后的函数与新安装（`histnew`）逐项相同，包括函数体哈希；
  - 1,991 项读取与 30 个合入结果（17 个文件、13 个行）与旧函数相同；
  - 18 项原断言再次通过；`agentdb_client` 仍能合入。
  - 多文件冲突的报错，新旧均为 `File conflict: hist/f0.txt`。比较时仍按“冲突路径之一”规范化，因为旧顺序没有定义。
- 快照策略以 `agentdb_manager` 身份、经 `SnapshotMaintenance.run_due`（MemoryStore，仅测试用）运行，interval 为 5：两张表各物化一次；第二次执行时没有目标。之后再比较全部读取与文件合入情形，结果仍相同。

## 5. 剩余限制与后续

- 预发布虚拟机上的 S4 增长测试已用新函数重跑（只修复、修复加 interval 100 的快照策略），结果见 [S4/S5](STAGING_S4_S5.md)。
- 策略需要有执行器定期运行；当前 `scripts/maintain_snapshot.py` 只处理单个目标，`run_due` 还没有命令行入口，也没有常驻调度。云端状态仍未经真实验证。
- 祖先遍历仍与历史长度线性相关（见第 3 节）；快照没有回收机制。

---

<a id="english-version"></a>

# History Growth: Bounded History Traversal and a Snapshot Policy

> Note (2026-10-30): cloud storage (GCS, OSS, S3), the HTTP state service and the ECS deployment have been removed; the code and their own docs remain in git history (last present in commit `d342ccf`), and the state engine keeps only its in-memory backend. What follows is kept as it was, and the files it names for these no longer exist.

2026-10-18. Deals with the costs growing with history length measured in section 2 of [S4/S5](STAGING_S4_S5.md). On the staging VM, with 2,000 commits on main, one 10-row merge took 50.7 s, a commit 499 ms, and a head read 1.2 s. The change is on the branch `perf/history-bounded-costs`, and the migration is [`sql/005_bounded_history.sql`](../sql/005_bounded_history.sql). All numbers come from the existing local Ubuntu WSL PostgreSQL 16.15, with client and server on the same laptop, and are only for before/after comparison. **Not yet rerun on the staging VM; no cloud services were used.**

## 1. Causes (measured on a 1,000-commit database)

The database is `agentdb_test_growth_67afbbec40`, kept from the baseline growth test, with 1,020 changesets.

- All recursive traversals were written as `lineage l JOIN vc.parent p ON p.child=l.id`. The planner chose a hash join for the recursive step, so every step did a full-table scan of `vc.parent` (`EXPLAIN ANALYZE`: Seq Scan on parent, loops=1011). So a single traversal was proportional to the square of the history length. Walking the first-parent chain over 1,000 commits once took 106 ms; the same traversal with an index lookup per step takes only 2.4 ms. `_ancestors`, `rows_at` and `_file_state` were all written this way.
- `vc._file_state` first walked to the root and then took the most recent record by depth, without stopping early even after finding the path. `write_file` called it once per call; `vc.merge` called it three times for **every** path that had ever appeared in history.
- `vc.merge` traversed all ancestors of the target twice (once to decide whether it had already been merged, once inside `merge_base`), and all ancestors of the source once.
- `rows_at` replays all commits since the most recent snapshot, and there was no mechanism to materialize snapshots automatically.

## 2. Changes

All are `CREATE OR REPLACE`: signatures, return values, error texts and SQLSTATEs, owners, SECURITY DEFINER and grants are unchanged; search_path changes from `pg_catalog, vc` to `pg_catalog, vc, pg_temp` (as 007 sets it, see [SETUP](SETUP.md)). New installations run 005 after 001–004; on old installations the database owner runs `python db.py install-bounded-history`, which can be run repeatedly. The functions with the same names in `001_core.sql` are the old definitions, and 005 takes precedence.

1. **Parent edges walked step by step through the index** (`_ancestors`, `rows_at`, `_file_state`, `snapshot_due`): the recursive step is changed to `CROSS JOIN LATERAL (SELECT p.parent FROM vc.parent p WHERE p.child=… OFFSET 0)`. `OFFSET 0` prevents the subquery from being flattened into an ordinary join, so each step is a parameterized index probe, independent of statistics. The traversal's stop condition and output set are unchanged.
2. **`vc._file_state` stops when found**: during recursion each changeset is marked with "whether it recorded this path", and the traversal stops at the first first-parent ancestor that recorded the path, taking the state there. A path never recorded by any changeset is not traversed and NULL is returned directly. The return values are the same as before: a deletion is still NULL, and the executable flag is still taken from that record. The cost of a commit therefore depends on how long ago that path was last changed, not on the whole length of history.
3. **`vc.merge` does less repeated work** (the conflict rules and the order of decisions are unchanged: rows are handled table by table first, then files path by path; each path is still compared in order: common ancestor vs source, target vs source, target vs common ancestor):
   - The target's ancestors are traversed only once, used both for the "source already merged" decision and the common-ancestor computation. A new internal function `vc._merge_base(祖先数组, b)` (`祖先数组` = ancestor array) is added, and `vc.merge_base` delegates to it with unchanged semantics: the unique maximal common ancestor, otherwise it raises `A unique merge base is required`.
   - File states are evaluated along the first-parent chain. During a merge, the first-parent chains of the common ancestor and the source are walked down in step, until one of them reaches a changeset the other has already passed. From that point down the two chains are identical; for paths recorded by neither chain above that point, the states on both sides are necessarily the same, and the first comparison of the original loop would skip them too. So only paths recorded above that point need to be checked. This relies only on the definition of the first-parent chain, not on how the history was produced. The cost depends on the length of the divergence, not on the length of history.
   - Paths are checked in path-name order. The order of the original loop came from an unordered `SELECT DISTINCT` (hash aggregate) and was undefined. Only when multiple file conflicts exist at the same time does it affect which path the error names: the error code and rollback are the same, and the path reported is still one of the conflicting paths.
4. **Snapshot policy** `vc.snapshot_due(branch DEFAULT 'main', interval DEFAULT 100)`: read-only, looking back at most interval commits. If reading a registered table at the branch head requires replaying at least interval commits, it returns (head, table_id). Granted only to `agentdb_manager`; `agentdb_client` and PUBLIC cannot execute it. The executor `SnapshotMaintenance.run_due(actor, branch, interval, policy_ref)` calls the existing recoverable `run()` for each target. The task ID is `snapshot-<revision>-<table_id>`, and repeated execution only verifies, without materializing again. Snapshots are still derived data:
   - Input version: the branch head at the time the policy runs;
   - Completion condition: `vc.snapshot_status(...).snapshot_exists`, i.e. the row exists in `snapshot_manifest`;
   - Rebuild method: regenerated from history via `vc.materialize` (i.e. `rows_at` replay);
   - Read results do not depend on whether the snapshot exists (verified in section 4).

   Computing with one run every 10 commits, a head read replays at most about interval + 10 commits. The cost is materializing 4,000 rows each time (about 50–65 ms locally, done outside the commit), and snapshot rows add storage: at 1,000 commits with interval 100, the database grows from 21.7 MB to 33.4 MB. Snapshot tables, like history tables, cannot be modified, and there is no reclamation mechanism yet.
5. File-list snapshots (saving the complete file tree at each snapshot point) are not implemented. With stop-when-found, a single `_file_state` takes about 2 µs per step along the first-parent chain: for example, for `src/app.py`, written only in the seed and unchanged for 1,000 commits, one read takes 1.9 ms. If many long-unchanged files and very long histories appear in the future, this approach can be considered then.

## 3. Before/after comparison

### Growth test (`scripts/staging_drills.py growth --config connection.local.json --levels 0 100 1000`)

Each growth commit changes 10 product rows and writes one of 50 files; merges are the median of 3, commits the median of the last 10 commits, and reads the median of 5 `EXPLAIN ANALYZE` runs. The raw results are in [results/history-growth/](../results/history-growth/). When the test was copied locally, staging's `growth.json` was not overwritten. The note about "TLS connection to the VM" in the result files is fixed text from the script; this time the actual connection was to the local machine.

| Commits | Merge before → after (no policy) → after (policy 100) | Commit before → after → after (policy) | head full-table read before → after → after (policy) |
|---|---|---|---|
| 0 | 36.7 → 45.2 → 46.5 ms | — | 7.0 → 6.4 → 6.2 ms |
| 100 | 137.7 → 62.8 → 33.5 ms | 4.8 → 5.1 → 5.1 ms | 11.9 → 11.9 → 5.1 ms |
| 1,000 | **6,521 → 160 → 42.6 ms** | **58.4 → 5.5 → 6.3 ms** | **113.6 → 44.4 → 6.3 ms** |

- Commits no longer grow with history (changes 1 and 2).
- Without running the policy, merges and head reads still grow with history, but now linearly and cheaply: `rows_at` replays all commits since the seed snapshot, and a merge calls `rows_at` 3 times. With the policy running, both stay flat between 0 and 1,000 commits (the differences among merges of 33–47 ms are single-machine timing fluctuations). Between 100 and 1,000 commits the policy ran 9 times, each time materializing two tables (products and orders), median 65 ms.
- What is still proportional to the length of history: the traversal of all ancestors of the target and the source during a merge. It now takes about 1–2 µs per commit, and at 1,000 commits the whole `merge_base` takes about 5 ms (104 ms before). Eliminating it completely would need a derived index such as generation numbers; there is currently no load-based justification, so it is not implemented for now. `vc.history` still uses the old join form, but it traverses at most limit steps and was not changed this time.

### Function level (1,000-commit baseline database, replaced inside a transaction and then rolled back)

| Measurement | Old | New |
|---|---|---|
| `_file_state` for 25 random versions × 52 paths (1,300 calls) | 27,379 ms | 90 ms |
| `_ancestors` for 25 versions | 643 ms | 24 ms |
| `rows_at` for 25 versions × 2 tables | 1,365 ms | 657 ms |
| `merge_base(head, head 的父)` | 104 ms | 5.1 ms |
| `vc.snapshot_due('main',100)` | — | 7.5 ms |

(`head 的父` = the parent of head.)

The results of the three groups of reads are identical item by item (1,300 file states, 25 ancestor sets, 50 table state hashes).

### Existing suites (run on newly created local test databases both before and after the migration)

| Suite | Before | After |
|---|---|---|
| `python run_tests.py`: the 18 original assertions + concurrency, sessions, merge semantics | All passed (`agentdb_test_06f5945e3a67`) | All passed (`agentdb_test_ff98e378fcd4`) |
| `review_local.py`: restricted role, two interruption windows, all ten branches merged | Passed; merge phase 575 ms, 45 retries | Passed; merge phase 531 ms, 45 retries |
| `scripts/check_merge_migration.py` (004's migration verification) | Passed, 13 cases identical | Passed, 13 cases identical |
| `unittest discover -s tests` | — | 75 items passed (including the new snapshot policy tests) |

## 4. Migration verification: `scripts/check_history_migration.py`

[`scripts/check_history_migration.py`](../scripts/check_history_migration.py); results in [`results/history-migration-results.json`](../results/history-migration-results.json) (databases `agentdb_test_histmig_c82e056feb`, `agentdb_test_histreg_c82e056feb`, `agentdb_test_histnew_c82e056feb`; rerun on 2026-10-21 after 005's definitions gained `pg_temp`, with conclusions identical to the first run (database suffix `2b0e3f493e`) except for search_path; the first run's result file was overwritten).

- Two old-version databases are built with 001–004 from commit 0bf7309, installed the same way as `Connection.install` did at the time.
  - The `histreg` database runs the 18 original assertions and the 13 row-merge cases. These assertions assume the database has only the seed (for example only 1 blob), so it is built separately.
  - The `histmig` database commits a realistically shaped history: 24 commits on main, changing, deleting and restoring files and toggling the executable flag, while also changing rows; a table snapshot is built midway; branches have files and rows; main merges a branch and then it is merged back into main; there are branches of branches; and there is also a criss-cross (two merge bases).
- Before the migration, 1,991 read results are recorded: `_file_state` and `read_file` for every version × every path; `diff_files` for every parent edge and between each pair of branch heads; `diff_rows` for every parent edge; `_ancestors` and `rows_at` for every version; `merge_base` between each head and every version, including errors. In addition, 17 file merge cases are run in rolled-back transactions: one-sided change, two-sided changes that differ or are the same, delete on one side and change on the other, deletes on both sides, executable flag change only, multiple file conflicts, file conflict after rows were applied rolling back as a whole, merging a branch, a long-lived branch merged into main first and then back, already merged, criss-cross, nonexistent source, source being a descendant of the target.
- After running `install_bounded_history` twice:
  - The hashes of business data and history tables are unchanged; the owner, SECURITY DEFINER, ACL and execute permissions of the replaced functions are unchanged, and their search_path gains a trailing `pg_temp` with no other configuration change;
  - The migrated functions are identical item by item to a new installation (`histnew`), including function body hashes;
  - The 1,991 reads and the 30 merge results (17 file, 13 row) are the same as with the old functions;
  - The 18 original assertions pass again; `agentdb_client` can still merge.
  - The error for multiple file conflicts is `File conflict: hist/f0.txt` both before and after. The comparison still normalizes it as "one of the conflicting paths", because the old order was undefined.
- The snapshot policy is run as `agentdb_manager` through `SnapshotMaintenance.run_due` (MemoryStore, for testing only) with interval 5: each of the two tables is materialized once; on the second run there are no targets. Afterwards all reads and file merge cases are compared again, and the results are still the same.

## 5. Remaining limitations and next steps

- The S4 growth test on the staging VM has been rerun with the new functions (the fix alone, and the fix with the interval-100 snapshot policy); results are in [S4/S5](STAGING_S4_S5.md).
- The policy needs an executor to run it periodically; currently `scripts/maintain_snapshot.py` handles only a single target, `run_due` has no command-line entry point yet, and there is no resident scheduler. Cloud state is still not verified against real cloud services.
- Ancestor traversal is still linear in the length of history (see section 3); snapshots have no reclamation mechanism.
