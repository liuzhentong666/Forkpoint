中文 | [English](#english-version)

# 形式化模型：Agent 怎样合入 main

2026-11-06。Agent 把分支合入 main 时要经过的规则，写成了一个 TLA+ 模型 [formal/AgentMerge.tla](../formal/AgentMerge.tla)，用 TLC 检查：TLC 会尝试两个 Agent 各个步骤的所有先后顺序。它检查的是模型，不是 SQL：模型是照下面这些函数手写的，两者不一致时它发现不了。唯一的发现已在 PostgreSQL 上重放，确认属实。

## 模型包括什么

- **规则**，取自 SQL。`vc.agent_merge`（[sql/011_agent_boundary.sql](../sql/011_agent_boundary.sql)）在取得 main 的锁之前，拒绝没有剩余行数的登录（AB002）；然后锁住 main 那一行，执行 `vc.merge_validated`，再按合入提交记录的行数扣预算，剩余不够就拒绝（同样是 AB002）。`vc.merge_validated`（[sql/008_read_set_merge.sql](../sql/008_read_set_merge.sql)）从合并基点开始比较：声明读过的行在 main 上变了，或分支写过的行在 main 上已经是分支自己的值，就报 RS001；main 自基点以来没动过，或分支已经合入过，就跳过这两项检查。`vc.merge`（[sql/001_core.sql](../sql/001_core.sql)）对已经合入的分支直接返回 head；对分支改过的行，main 上已是同样的值就跳过，main 改成了别的值就拒绝（P0001），其余照常合入。整个过程是一个事务，任何拒绝都会全部回滚并释放锁。
- **Agent**。一家店有两个仓库，各有两件货。两个 Agent 各自建分支、读取、从自己选的仓库卖出一件或两件、记一行订单，然后合入。卖出按两条规则之一：*take*（这个仓库有货），或 *keep*（卖出后两个仓库合计至少还剩一件；这是跨行的规则，写偏斜就出在这里）。它声明的读集可以是任意一组仓库，只要加上它写的那个仓库，能覆盖规则读过的行，这正是 `vc.merge_validated` 的要求。两个 Agent 是同一个登录的两个会话，共用一份预算；TLC 分别尝试 3 行和 4 行的预算（一次卖出记 2 行）。第一次调用可能还在运行时，Agent 可以再调用一次 `vc.agent_merge`，就像重试那样。
- **没有包括**：闸门与冻结（AB000、AB006），字节预算与 `max_table_share`（AB003、AB007，它们拒绝和回滚的方式与 AB002 相同），文件，快照，以及历史的回放（模型里每个版本直接存它的行）。规模只有两个 Agent、四行：TLC 检查了它们的所有步骤顺序，但不超出这个规模。

## 必须成立的性质

- `MainIsSerial`：main 上的内容，等于已合入的卖出按合入顺序一个接一个执行的结果。也就是说，没有卖出丢失，也没有卖出被算两次。
- `WithinBudget`：没有登录合入的行数超过发给它的预算，而且剩余行数恰好反映它合入了多少。
- `HeadStaysUnderLock`：合入持有 main 的锁期间，main 一直停在它检查时读到的 head 上，所以检查看到的就是它要合入的那个 main。
- `TypeOK`：每个变量都在取值范围内。

## 结果

TLC 2.19（TLA+ 工具 v1.7.4），单线程，Java 17，开发用的笔记本；原始输出见 [results/formal/](../results/formal/)。

| 配置 | 结果 |
|---|---|
| [AgentMerge.cfg](../formal/AgentMerge.cfg)：现行规则，Agent 按文档在自己的分支上读取 | 所有性质成立：777,896 个不同状态，最深 19 步，用时 2 分 17 秒 |
| [AgentMerge_no_written_check.cfg](../formal/AgentMerge_no_written_check.cfg)：对照，去掉 008 对分支写过的行的检查 | `MainIsSerial` 在第 13 步被违反：两个 Agent 都从 w1 取一件，都不声明读集；A 合入后，B 合入时发现 main 上的 w1 已经是 B 的值，于是跳过这一行，只合入了 B 的订单。卖出两件，库存只减了一件：这正是 008 要防的情形（“否则最后一件货的两次售出都会合入”） |
| [AgentMerge_read_main_first.cfg](../formal/AgentMerge_read_main_first.cfg)：Agent 可以先读 main、之后再建分支，它的登录允许这样做 | `MainIsSerial` 在第 13 步被违反，见下文 |

为了确认通过不是空洞的，我用临时的、必须被违反的不变式（没有纳入版本库），逐一核对主配置能走到每条路径：两次卖出都合入，锁之前和锁之下的 AB002，RS001（其中包括被拒绝的写偏斜），P0001，重试时发现分支已经合入，同一分支的两次调用同时在运行。我还把模型改坏了三处：去掉锁之下的预算检查，登录的剩余行数会降到零以下（`TypeOK` 在第 13 步被违反）；去掉锁，`HeadStaysUnderLock` 在第 9 步被违反；去掉读集检查，写偏斜得以合入，`MainIsSerial` 在第 13 步被违反。

## 发现：在建分支之前读取

读集是从分支开始的地方比较的，而不是从 Agent 读取的地方。Agent 先读 main、后建分支，那么这期间合入的改动不受保护。TLC 给出的步骤：

1. w1 有 2 件。B 读 main，读到 2。
2. A 建分支，卖出一件（w1 = 1，订单 A），合入。
3. B 这时才建分支，分支来自 w1 = 1 的 main。B 按它读到的值卖出：写 w1 = 2 − 1 = 1 和订单 B。分支上的 w1 本来就是 1，所以分支只记下了订单。
4. B 合入时没有要检查的：自 B 的分支开始以来，main 没有动过。于是合入成功。

最后 main 上有两张各一件的订单，w1 却还是 1，可 2 件货卖出了两件。

在 PostgreSQL 16.15 上新建的 `agentdb_test_*` 库中重放了一次，以属主身份调用 `vc.merge_validated`（`vc.agent_merge` 执行的就是它），B 的读集写明了这个商品。B 先建分支、在分支上读取时，B 的合入被拒绝，报 RS001（库存 1，一张订单）。B 先读 main 时，B 的合入成功，只记下 `app.orders I`，结果库存 1，却有两张各一件的订单。重放脚本没有纳入版本库。

怎样避免：在 `vc.agent_fork` 之后，在分支上读取。[Agent 边界](AGENT_BOUNDARY.md)中给 Agent 的用法示例就是这个顺序，但没有说明在此之前的读取不会被检查。本文没有修改那份说明。

## 运行

需要 Java 11 或更高版本，以及 TLA+ 工具 v1.7.4 的 `tla2tools.jar`（[下载](https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar)，2,274,532 字节，SHA-256 `936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`）。在 `formal/` 中运行：

```console
java -XX:+UseParallelGC -cp <路径>/tla2tools.jar tlc2.TLC -workers 1 -difftrace -config AgentMerge.cfg AgentMerge.tla
```

换成另外两个配置文件即可运行它们。所有性质成立时 TLC 以 0 退出；有性质被违反时以 12 退出，并打印违反它的最短步骤。TLC 的工作文件写在 `formal/states/`，git 会忽略它。`results/formal/` 中的文件就是这些输出，只去掉了被解析文件的路径。

模型是手写的：改动 `vc.agent_merge`、`vc.merge_validated` 或 `vc.merge` 时，要同步修改模型并重新运行。它不连接任何数据库。

## 为什么这不违反功能暂停

它没有给引擎增加或修改任何函数：它是一项检查，和测试一样。

---

<a id="english-version"></a>

# Formal Model: How Agents Merge into main

2026-11-06. The rules an agent's merge into main goes through are written as a TLA+ model, [formal/AgentMerge.tla](../formal/AgentMerge.tla), checked with TLC, which tries every order of two agents' steps. It checks the model, not the SQL: the model is written by hand from the functions below, so a difference between the two goes unnoticed. The one finding was replayed on PostgreSQL to make sure it's real.

## What it models

- **The rules**, from the SQL. `vc.agent_merge` ([sql/011_agent_boundary.sql](../sql/011_agent_boundary.sql)) refuses a login with no rows left before it takes main's lock (AB002). It then locks main's row, runs `vc.merge_validated`, and charges the rows the merge commit records, refusing if fewer are left (AB002 again). `vc.merge_validated` ([sql/008_read_set_merge.sql](../sql/008_read_set_merge.sql)) compares from the merge base: RS001 if a declared read changed on main, or if a row the branch wrote now holds the branch's own value there. It skips both checks when main hasn't moved since the base or the branch has already landed. `vc.merge` ([sql/001_core.sql](../sql/001_core.sql)) returns the head for a branch that has already landed. Of the rows the branch changed, it skips those main already holds, refuses one main changed to another value (P0001), and applies the rest. It's all one transaction, so any refusal rolls everything back and releases the lock.
- **The agents.** A shop has two warehouses with two units each. Each of two agents forks a branch, reads, sells one or two units from a warehouse it picks, records an order row, and merges. It sells by one of two rules: *take* (the warehouse has the units) or *keep* (at least one unit stays in stock across both warehouses; a rule across rows, which is where write skew comes from). Its read set is any set of warehouses that, with the warehouse it writes, covers the rows its rule reads, as `vc.merge_validated` asks. Both agents are sessions of one login, so they share a budget; TLC tries a budget of 3 rows and of 4 (a sale records 2). An agent may call `vc.agent_merge` a second time while the first call may still be running, as a retry would.
- **Left out**: the gate and freezes (AB000, AB006), the bytes budget and `max_table_share` (AB003, AB007, which refuse and roll back the way AB002 does), files, snapshots, and replaying history (each revision in the model stores its rows). The size is two agents and four rows: TLC checks every order of their steps, but nothing beyond that size.

## What must hold

- `MainIsSerial`: main holds what the sales that landed would give if they had run one at a time, in the order they landed. So no sale is lost, and none counts twice.
- `WithinBudget`: no login lands more rows than it was issued, and the rows it has left say exactly what it landed.
- `HeadStaysUnderLock`: while a merge holds main's lock, main stays at the head its checks read, so the checks look at the main it lands on.
- `TypeOK`: every variable stays in its range.

## Results

TLC 2.19 (TLA+ tools v1.7.4), one worker, Java 17, on the development laptop; raw output in [results/formal/](../results/formal/).

| Config | Result |
|---|---|
| [AgentMerge.cfg](../formal/AgentMerge.cfg): the rules as they are, with agents reading on their branch as documented | Every invariant holds: 777,896 distinct states, 19 steps deep, 2 min 17 s |
| [AgentMerge_no_written_check.cfg](../formal/AgentMerge_no_written_check.cfg): control, without 008's check of the rows a branch wrote | `MainIsSerial` fails at step 13. Both agents take a unit from w1 and declare no reads. After A lands, B's merge finds main already holding B's value for w1, skips that row, and lands only B's order. Two units sold, stock down by one: the case 008 was written against ("else two sales of the last unit both land") |
| [AgentMerge_read_main_first.cfg](../formal/AgentMerge_read_main_first.cfg): an agent may read main first and fork afterwards, which its login allows | `MainIsSerial` fails at step 13; see below |

To make sure the pass means something, I checked by hand that the main config reaches every path, with temporary invariants that TLC had to break (not committed): two sales landing, AB002 before and under the lock, RS001 (a refused write skew among them), P0001, a retry finding its branch already landed, and two calls on one branch running at once. I also broke the model three ways. Without the budget check under the lock, a login's rows left go below zero (`TypeOK` fails at step 13). Without the lock, `HeadStaysUnderLock` fails at step 9. Without the read check, write skew lands and `MainIsSerial` fails at step 13.

## The finding: reads made before the branch

The read set is compared from where the branch started, not from where the agent read. An agent that reads main and forks later isn't protected against what landed in between. TLC's trace:

1. w1 has 2 units. B reads main: 2.
2. A forks, sells one (w1 = 1, order A), and lands.
3. Only now does B fork, from main with w1 = 1. B sells by what it read: it writes w1 = 2 − 1 = 1 and order B. w1 is already 1 on the branch, so the branch records only the order.
4. B's merge has nothing to check, because main hasn't moved since B's branch started. It lands.

Main ends with two orders of one unit each and w1 still at 1, though two units were sold out of 2.

It was replayed once on PostgreSQL 16.15, in a new `agentdb_test_*` database, as the owner, calling `vc.merge_validated` (which is what `vc.agent_merge` runs) with B's read set naming the product. With B forking first and reading on its branch, B's merge was refused with RS001 (stock 1, one order). With B reading main first, B's merge landed recording only `app.orders I`, leaving stock 1 with two orders of one unit. The replay script isn't committed.

What avoids it: read on the branch, after `vc.agent_fork`. The instructions for agents in [The agent boundary](AGENT_BOUNDARY.md) show that order but don't say that reads made before it go unchecked. This doc doesn't change them.

## Running it

It needs Java 11 or later and `tla2tools.jar` from the TLA+ tools v1.7.4 ([download](https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar), 2,274,532 bytes, SHA-256 `936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`). From `formal/`:

```console
java -XX:+UseParallelGC -cp <path>/tla2tools.jar tlc2.TLC -workers 1 -difftrace -config AgentMerge.cfg AgentMerge.tla
```

Swap in the other two config files to run them. TLC exits 0 when every invariant holds and 12 when one is violated, printing the shortest trace that breaks it. Its working files go to `formal/states/`, which git ignores. The files in `results/formal/` are this output with the paths of the parsed files removed.

The model is written by hand: a change to `vc.agent_merge`, `vc.merge_validated` or `vc.merge` needs the model changed to match and rerun. It touches no database.

## Why this doesn't break the feature pause

It adds no function to the engine and changes none: it's a check, like the tests.
