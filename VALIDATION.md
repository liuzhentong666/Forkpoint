中文 | [English](#english-version)

# 本机验证记录

验证日期：2026-10-15（Asia/Shanghai）。运行时：**Ubuntu WSL 中已有的 PostgreSQL 16.15**，集群 `16/main`，端口 `5432`。通过 `/usr/bin/psql` 与 Unix socket `/var/run/postgresql` 连接。

**18 项集成测试通过，另外完成两组 10 连接并发验证。** 本次输出记录在 `test-results-local.json`。旧 `test-results.json` 是先前便携实例的历史记录，不代表本次运行。

## 本地实例接入

- 持久项目库：`agentdb`，已安装版本管理对象和演示业务表。
- 集成测试库：`agentdb_test_b44811c63482`，测试数据单独存放，保留供检查。
- 已确认项目库商品和订单均为 0 行，未封口事务为 0；测试数据没有写入项目库。
- 对已安装项目库再次执行安装，返回拒绝覆盖错误；原对象保留。
- 数据目录为既有的 `/var/lib/postgresql/16/main`，验证结束后集群状态为 `online`。
- 脚本不再调用 `initdb`、`pg_ctl` 或项目里的 PostgreSQL 运行程序，也不更改现有服务认证配置。

## 行为验证

- 代码和普通业务表的修改在同一提交下可见；旧版本仍可读。
- main 继续更新后，旧分支仍读取冻结的基线。
- 分支创建不复制 row_change 或 blob。
- 重复更新归并；插入后删除抵消；主键修改记录为删除和插入。
- 遗漏 finish_change 时，COMMIT 失败且真实业务表回滚。
- 非版本化直接写入被拒绝；普通角色不能追加已发布历史。
- 分支中的 UNIQUE、FK、CHECK 违规在合入 main 时被原生约束拒绝。
- 不相交修改可合入；行冲突与文件冲突使整个合入回滚。
- 快照物化前后的固定版本状态一致。
- 旧分支头条件不能覆盖新提交。

## 4,000 行、10 Agent 对应的并发连接

| 场景 | 结果 |
|---|---|
| 10 个连接分别写独立分支 | 10 个成功，main 未变化 |
| 创建 10 个分支 | 新增业务历史行 0；仅增加分支记录 |
| 10 个连接持有同一个旧 main head 竞争写入 | 1 个成功，9 个收到 SQLSTATE 40001 |
| 测试结束 | 测试库业务表仍为 4,000 行，没有未封口事务；原有本地服务继续运行 |

## 单次计时

独立分支这组试验总墙钟约 1,715.73 ms，包含 Windows 调用 WSL 和启动 psql 客户端的开销。SQL 内部测得中位数约 15.38 ms、最大约 31.61 ms；这个内部计时截至 finish_change 后，**不包含最终 COMMIT**，不能称作持久化提交延迟。

这是单次本机合成负载，不包含真实模型思考、跨地区网络或长期历史增长，也没有与 Spanner 作对照。因此只用于验证功能与实验方法，不作为生产容量或产品优劣结论。

---

<a id="english-version"></a>

# Local Validation Record

Validation date: 2026-10-15 (Asia/Shanghai). Runtime: **the existing PostgreSQL 16.15 in Ubuntu WSL**, cluster `16/main`, port `5432`. Connected through `/usr/bin/psql` and the Unix socket `/var/run/postgresql`.

**18 integration tests passed, and two groups of 10-connection concurrency checks were also completed.** The output of this run is recorded in `test-results-local.json`. The old `test-results.json` is a historical record from the earlier portable instance and does not represent this run.

## Connecting to the local instance

- Persistent project database: `agentdb`, with the version management objects and demo business tables installed.
- Integration test database: `agentdb_test_b44811c63482`; test data is stored separately and kept for inspection.
- Confirmed that the project database has 0 rows of products and of orders, and 0 unsealed transactions; test data was not written to the project database.
- Running the installation again on the installed project database returns a refuse-to-overwrite error; the original objects are kept.
- The data directory is the existing `/var/lib/postgresql/16/main`, and after validation the cluster state is `online`.
- The scripts no longer call `initdb`, `pg_ctl` or the PostgreSQL runtime programs in the project, and do not change the existing service's authentication configuration.

## Behavior validation

- Changes to code and to ordinary business tables become visible under the same commit; old versions remain readable.
- After main keeps updating, old branches still read the frozen baseline.
- Creating a branch does not copy row_change or blobs.
- Repeated updates are coalesced; an insert followed by a delete cancels out; a primary key change is recorded as a delete and an insert.
- When finish_change is omitted, COMMIT fails and the real business tables are rolled back.
- Unversioned direct writes are rejected; ordinary roles cannot append to published history.
- UNIQUE, FK and CHECK violations in a branch are rejected by native constraints when merging into main.
- Disjoint changes can be merged; row conflicts and file conflicts roll back the whole merge.
- Pinned-version state is identical before and after snapshot materialization.
- A stale branch-head condition cannot overwrite a newer commit.

## Concurrent connections corresponding to 4,000 rows and 10 Agents

| Scenario | Result |
|---|---|
| 10 connections each write a separate branch | 10 succeed, main unchanged |
| Create 10 branches | 0 new business history rows; only branch records added |
| 10 connections holding the same old main head compete to write | 1 succeeds, 9 receive SQLSTATE 40001 |
| End of test | The test database's business table still has 4,000 rows, no unsealed transactions; the original local service keeps running |

## Single-run timing

The total wall-clock time of the separate-branch trial was about 1,715.73 ms, including the overhead of Windows calling WSL and starting the psql client. Measured inside SQL, the median was about 15.38 ms and the maximum about 31.61 ms; this internal timing ends after finish_change and **does not include the final COMMIT**, so it cannot be called durable commit latency.

This is a single local synthetic load, not including real model thinking, cross-region networking or long-term history growth, and with no comparison against Spanner. So it is only used to validate functionality and the experimental method, not as a conclusion about production capacity or product merits.
