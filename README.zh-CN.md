# Forkpoint

**在 PostgreSQL 里给 AI Agent 用的 Git 式分支。** 每个 Agent 在自己的分支上工作，把代码文件和表中的行一起提交；只有在不覆盖别人的工作、并且真实约束仍然成立时，才能合入 `main`。

[English](README.md) · [技术博客](docs/TECHNICAL_BLOG.md) · [设计说明](docs/DESIGN_REVIEW.md)

![License: MIT](https://img.shields.io/badge/license-MIT-blue) ![PostgreSQL 16](https://img.shields.io/badge/PostgreSQL-16-336791) ![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB)

原名 AgentDB，2026-10-20 更名。`agentdb_demo`、`agentdb_manager` 等数据库名和角色名仍沿用旧前缀。

## 为什么需要它

多个 Agent 同时修改同一个数据库时，会遇到 Git 曾经替开发者解决过的问题：一个 Agent 覆盖了另一个的修改；另一个 Agent 做到一半，读到的数据已经变了；还有一个改了代码里的定价规则，却没有改依赖它的价格。

Forkpoint 把 Git 的解法加到普通的 PostgreSQL 数据库上：

- **分支。** Agent 从一个固定版本建分支，`main` 之后的变化不会渗进来。
- **代码和数据在同一个提交里。** 改动的文件和改动的行在一个事务中发布，任何一步失败，两者都不会生效。
- **合入不靠猜。** 基于旧 `main` 的合入会被拒绝，与别人的修改冲突的合入也会被拒绝。两种失败各有错误码，Agent 能判断该重试还是该重做。

核心是纯 SQL 和 PL/pgSQL，不需要扩展，也不需要额外的服务进程；Python 文件只是一层很薄的命令行和测试工具。

## 看它怎么工作

两个 Agent 从商品 101 的同一个版本开始（价格 10，库存 100）。一个提价，一个减库存。`docker compose up` 会运行 [`examples/two_agents.sql`](examples/two_agents.sql)，输出如下（示例的说明文字为英文）：

```text
1. main: product 101 costs 10 and has 100 in stock
2. price-agent and stock-agent each fork a branch from that version
3. price-agent: price 11 and src/pricing.py, in one commit
4. stock-agent: stock 95, on a branch that never sees the price change
5. price-agent merges into main
6. stock-agent merges, still expecting main at the old version (should fail: stale head)
psql:examples/two_agents.sql:44: ERROR:  Stale head: expected 2265b903-…, actual cd557e23-…
   SQLSTATE 40001 -> re-read main and retry
7. stock-agent retries against the latest main (should fail: its row 101 still has price 10)
psql:examples/two_agents.sql:49: ERROR:  Row conflict: table 3c320fad-…, key {"id": 101}
   SQLSTATE P0001 -> re-propose from the latest main
8. stock-agent re-proposes from the latest main and merges
9. main now has both changes, and the price change brought its code
 id  | price | stock
-----+-------+-------
 101 | 11.00 |    95

 pricing_code
--------------
 PRICE = 11

OK: both conflicts were caught, and no agent overwrote the other.
```

第 3 步是一个普通事务，行的修改和代码文件进入同一个提交：

```sql
BEGIN;
SELECT vc.begin_change('price-agent', :'base', 'price-agent', 'raise price to 11');
UPDATE agent.products SET price = 11 WHERE id = 101;
SELECT vc.write_file('src/pricing.py', convert_to('PRICE = 11', 'UTF8'));
SELECT vc.finish_change() AS price_rev \gset
COMMIT;
```

每次合入都只是一次调用 `SELECT vc.merge('main', :'stock_rev', :'base', 'stock-agent');`，要么完整生效，要么以上面的某个错误码失败。

以上是 PostgreSQL 16.15 上一次真实运行的输出，UUID 做了缩写。同样的场景也用两个真实 GPT Agent 做过[预登记的十轮实验](docs/GPT_REPEAT_EXPERIMENT.md)：完成的 8 轮全部达成目标，没有一轮覆盖对方的工作；另外 2 轮因模型服务额度用尽而中断，计为失败。

## 快速开始

最快的试用方式是 Docker。一条命令即可启动 PostgreSQL 16、安装 Forkpoint 并运行两个 Agent 的示例：

```bash
git clone https://github.com/liuzhentong666/Forkpoint.git
cd Forkpoint
docker compose up
```

运行最后会输出 `OK: both conflicts were caught, and no agent overwrote the other.`。PostgreSQL 会继续运行，可以用 `docker compose exec db psql -U postgres -d agentdb_demo` 查看数据。按 Ctrl+C 停止，用 `docker compose down` 删除容器。

如果要使用自己的 PostgreSQL，需要一个 PostgreSQL 服务器（在 16 上测试）、`psql` 客户端和 Python 3.10+，不需要安装任何 pip 包。

```bash
python db.py check                              # 找到 psql、连接并输出服务器版本
python db.py install --database agentdb_demo    # 创建数据库并安装 schema
psql -h 127.0.0.1 -U postgres -d agentdb_demo -f examples/two_agents.sql
python run_tests.py                             # 在新建的 agentdb_test_* 库中运行集成测试（该库保留以便检查）
```

`db.py` 默认以 `postgres` 用户连接 `127.0.0.1:5432`，并在 `PATH` 中查找 `psql`。需要修改时，把 `connection.example.json` 复制为 `connection.local.json`。密码、远程服务器（只接受 TLS `verify-full`）、WSL 中的 PostgreSQL 以及升级已有安装，见 [docs/SETUP.md](docs/SETUP.md)。

要对自己的表做版本管理，先建一张带主键的空表，再注册它；它的分支视图会出现在 `agent` schema 中：

```sql
SELECT vc.register_table('app.products');   -- Agent 通过 agent.products 写入
```

## 亮点

| | 做法 | 依据 |
|---|---|---|
| **代码与数据一起提交** | 文件、行变更和 changeset 在一个 PostgreSQL 事务中发布，任何一步失败都整体回滚 | [run_tests.py](run_tests.py) 中的 `code_and_native_data_publish_atomically` |
| **分支几乎零成本** | 建分支不复制行，也不复制文件（实测新增行历史 0 条） | [验证记录](docs/VALIDATION.md) |
| **Agent 能据此行动的错误** | `40001` 旧 head → 重读后重试 · `P0001` 合入冲突 → 重新提案 · `55P03` 等锁超时 → 稍后重试 · `RS001` 读冲突 → 重读这些行后重新判断 · `P0002` 分支不存在（在 `merge_validated` 中也指版本不存在）→ 核对名称或 ID | 10 个连接竞争同一个旧 head：1 个成功，9 个被拒绝；写偏斜与不存在的分支被拒见 [run_tests.py](run_tests.py) |
| **由真实约束把关 `main`** | 合入以普通 SQL 作用在真实表上，UNIQUE、外键和 CHECK 约束照常生效 | [run_tests.py](run_tests.py) 中的三项约束违规用例 |
| **历史变长后依然快** | `main` 上有 2,000 个提交时：一次 10 行合入 181 ms，一次提交约 20 ms（修复前为 50.7 s 和 499 ms） | [S4/S5 报告第 7 节](docs/STAGING_S4_S5.md) |
| **经过故障演练** | 断电后 201 个已确认的提交全部保留，没有半个提交；备份恢复 13 项指纹一致；失联客户端持有的锁约 60 s 内释放 | [S4/S5 报告](docs/STAGING_S4_S5.md) |
| **证据可以重跑** | 每个数字都链接到 [results/](results/) 中的原始结果文件和产生它的脚本 | [scripts/](scripts/) |

这些数字来自一台笔记本和本机虚拟机上的合成负载，说明设计在第一阶段目标（单表约 4,000 行、10 个并发 Agent）下可行，不是生产容量承诺。

## 工作原理

```text
选择一个固定版本
  → 建分支                                 （新增一个指针，不复制表）
  → BEGIN; begin_change(branch, expected_head)
  → 通过 agent.* 视图改行，用 write_file() 改文件
  → finish_change(); COMMIT
  → diff_rows() / diff_files()
  → merge(main, source, expected_head)     （冲突检查 + 原生约束）
  → 在一个事务中发布代码、历史和 main 的业务表
```

| Schema | 内容 |
|---|---|
| `app.*` | 真实的业务表，它们就是 `main`。 |
| `agent.*` | 每张已注册表对应一个视图，显示所选分支或版本；写入由 `INSTEAD OF` 触发器捕获。 |
| `vc.*` | changeset、分支头、文件 blob、行历史和可重建的快照。 |

由数据库强制保证：

- 在版本化修改之外写 `app.*` 会被拒绝。
- 已发布的 changeset 不能被修改、删除或追加。
- 忘记调用 `finish_change()` 时，`COMMIT` 失败，表的修改一并回滚。
- 同一次修改中对一行的多次更新只保留最初的 before 和最终的 after；插入后再删除会相互抵消。

主要 SQL 函数：`vc.head`、`vc.fork`、`vc.pin`、`vc.checkout`、`vc.begin_change`、`vc.finish_change`、`vc.write_file`、`vc.read_file`、`vc.diff_rows`、`vc.diff_files`、`vc.history`、`vc.merge`、`vc.merge_validated`、`vc.register_table`。完整设计与取舍见 [docs/DESIGN_REVIEW.md](docs/DESIGN_REVIEW.md)。

## 与其他方案的比较

| | Forkpoint | [Dolt](https://github.com/dolthub/dolt) | [Neon](https://neon.tech) 分支 |
|---|---|---|---|
| 运行在 | 你现有的 PostgreSQL | 自己的数据库引擎（兼容 MySQL；Doltgres 面向 PostgreSQL） | Neon 托管的 PostgreSQL |
| 分支是什么 | 一个指针，加上在分支上改过的行 | 版本化表的一个分支 | 整个数据库的写时复制副本 |
| 合回 `main` | 行级三方合并，由你的约束检查 | 单元格级三方合并 | 不按行合并 |
| 同时管理代码文件 | 是，与行在同一个提交里 | 否 | 否 |
| 成熟度 | 早期：单节点，第一阶段规模 | 成熟，已用于生产 | 托管服务 |

如果需要成熟、通用的版本化数据库，请用 Dolt。Forkpoint 适合两种情况：想继续使用自己的 PostgreSQL；希望 Agent 的代码修改和数据修改成为一个可审查、可合入的整体。我们没有与这两者做过性能对比。

## 当前限制

- **固定 schema。** 还没有 DDL 分支或迁移合并。
- **整行冲突。** 两个 Agent 修改同一行的不同列仍会冲突，尚不支持逐列合并。
- **不推断跨行规则。** 原生约束会被检查；像“订单量不能超过库存”这样的规则，可用 `vc.merge_validated` 合入并给出决定读过的行（它写过的行也会校验），这些行在建分支后被改过时合入会被拒绝（写偏斜）。
- **分支可以暂时无效。** UNIQUE 和外键只在合入 `main` 时检查。
- **读取需要回放历史。** 分支视图由历史加快照解析，用不到表自身的索引，因此需要维护任务按 `vc.snapshot_due` 生成快照。快照与历史目前都不回收。
- **单节点、单租户。** 没有复制、配额或租户隔离；同一分支的写入串行执行，所有合入 `main` 的操作都要取得 `main` 的锁。
- **表类型有限。** 表必须有主键，并且要在空表时注册；不支持 generated 和 identity 列。
- **云端状态服务未经验证。** 管理 Agent 的状态服务有 GCS、阿里云 OSS 和 S3 适配器，但都没有在真实云上运行过，部署也已暂缓。见 [docs/STORAGE_BACKENDS.md](docs/STORAGE_BACKENDS.md)。

## 目录结构

```text
db.py                 命令行：检查连接、安装、升级已有安装
run_tests.py          集成测试（自行创建 agentdb_test_* 库）
review_local.py       本机审查：维护恢复、受限角色、十个分支全部合入
agent_lab.py          实验中 GPT 与 Claude Agent 使用的工具命令行
agent_lab_gateway.py  可选的回环网关，为 agent_lab.py 复用 psql 会话
smoke_cloud.py        云端状态服务冒烟测试（尚未运行）
compose.yaml          一条命令的 Docker 演示
sql/                  版本引擎，按 001–010 顺序安装
examples/             可运行的 SQL 示例
scripts/              基准测试、迁移检查、预发布演练
results/              文档中各项数字对应的原始结果
agent_state/          管理 Agent 状态服务与存储适配器
experiments/          预登记的实验：GPT 与 Claude 重复实验、读集校验、上下文存档、可核查的任务状态
deploy/               预发布虚拟机配置和暂缓的 ECS 部署包
docs/                 安装、设计、验证记录、计划、技术博客
tests/                单元测试
```

## 文档

- [安装与升级](docs/SETUP.md)：连接选项、密码、TLS、WSL、迁移、测试
- [技术博客](docs/TECHNICAL_BLOG.md)：架构，以及两个真实 GPT Agent 带来的发现
- [设计说明](docs/DESIGN_REVIEW.md)：版本模型以及对原计划的修订
- [GPT 与 Claude 重复实验](docs/GPT_REPEAT_EXPERIMENT.md)：两种模型各预登记十轮，保留失败样本
- [读集校验实验](docs/READSET_EXPERIMENT.md)：在 75 次预登记的合成运行中比较读集校验与陈旧 head 检查，并完整重复一次
- [上下文存档实验](docs/CONTEXT_ARCHIVE_EXPERIMENT.md)：重置后逐字存档的上下文与模型自写的交接说明对比；按采纳标准没有证据支持存档
- [可核查的任务状态](docs/TASK_STATE_EXPERIMENT.md)：恢复时把检查点中的事实标为新鲜或过期；只做了试运行，未采纳
- [真实 Agent 的读集校验](docs/READSET_AGENTS_EXPERIMENT.md)：Forkpoint 的读集合入与普通 PostgreSQL 上被要求加锁并复查的 Agent 对比，每轮两个真实 Agent；已登记，只做了试运行（两轮都正确），样本尚未运行
- [agent_lab 网关](docs/AGENT_LAB_GATEWAY.md)：可选的回环常驻进程，为 Agent 的工具复用 psql 会话
- [历史增长](docs/HISTORY_GROWTH.md) 与 [合入耗时分解](docs/MERGE_BENCHMARK.md)
- [预发布 S1](docs/STAGING_S1_RESULTS.md) 与 [S4/S5 演练](docs/STAGING_S4_S5.md)：TLS 接入、增长、备份、断电、断网
- [参考数据计划](docs/REFERENCE_DATA_PLAN.md)：在现有 PostgreSQL 中，对应用读取而不写入的小表做受审变更；尚未实现
- [管理 Agent 协议](docs/MANAGER_AGENT_PROTOCOL.md) 与 [AGENTS.md](AGENTS.md)：在本仓库中工作的 Agent 须遵守的约定

`docs/` 中的大部分文档先写中文，英文版在同一文件的后半部分。

## 开发方式

本项目在 AI 编程助手的协助下开发，其中包括 Anthropic 的 Claude Code。由 Claude 参与的提交在提交信息中带有 `Co-Authored-By: Claude` 行，因此 Claude 会出现在贡献者列表中；项目方向、审查与取舍由作者决定。

## 许可证

[MIT](LICENSE)
