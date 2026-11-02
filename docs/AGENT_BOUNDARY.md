中文 | [English](#english-version)

# Agent 边界

2026-10-31。如果人不知道 Agent 在做什么，数据库怎样才安全？不靠盯着 Agent，而是限制每个 Agent 能做什么，让出错或被劫持的 Agent 只能造成有上限、有记录、可撤销的损害。决定由固定代码作出，不靠人，也不靠模型。

实现：[sql/011_agent_boundary.sql](../sql/011_agent_boundary.sql)、[agent_boundary.py](../agent_boundary.py)、[agent_state/maintenance.py](../agent_state/maintenance.py) 的 `BoundaryKeeper`。

## 组成

| 层 | 做法 | 拒绝码 |
|---|---|---|
| **独立登录** | `agent_boundary.py` 给每个 Agent 一个会过期的登录，只属于 `agentdb_worker`。不用 SET ROLE，所以 `RESET ROLE` 之后什么也不剩 | 42501 |
| **真实作者** | 数据库把登录名写入历史（`session_user` 不受 SECURITY DEFINER 影响），Agent 不传作者名 | |
| **只写自己的分支** | 只有 `vc.agent_fork` 建的分支才能写、才能合入。其他分支可读，Agent 靠读取协调 | AB001 |
| **main 只有一道门** | 只有 `vc.agent_merge` 能移动 main。每个登录有预付的行预算（落到 main 的行数）和字节预算（存下的字节数），不能追加；没有预算行就不能写 | AB002、AB003 |
| **每张表的份额** | 预算不知道表有多大，所以另有上限：前 20 行之后，一个登录在整个生命周期内最多改一张表的 10%（默认，发放时可改），文件同理。多次小合入也累计。份额为 1 表示不限，这要在它启动前决定 | AB007 |
| **撤销** | `vc.revert` 用提交记录下的前后值撤销一个 main 上的提交，生成新提交。行或文件之后变过就拒绝，不覆盖 | AB004、AB005 |
| **自检与冻结** | 管理循环只读目录检查边界；发现问题就冻结写入，读取继续；只有属主能解冻。人也可以用 `agent_boundary.py freeze` 立刻冻结 | AB000 |
| **留痕** | 每个登录执行的语句都进服务器日志，包括被拒绝的（拒绝会回滚，历史里没有）。Agent 关不掉。`report` 从历史得出它实际落到 main 的内容 | |

三个 NOLOGIN 角色，都不是超级用户：

| 角色 | 能做什么 | 不能做什么 |
|---|---|---|
| `agentdb_worker`（Agent 的登录都属于它） | 读取跟踪的表和全部历史；`vc.agent_fork`、`vc.agent_begin`、`vc.agent_merge`、`vc.agent_status`；经 `agent.*` 视图和 `vc.write_file` 写自己开启的变更 | 直接写表、`vc.begin_change`、`vc.merge`、读 `vc.*` 的表、建表、临时表、大对象 |
| `agentdb_keeper`（看守程序） | `vc.boundary_check`、`vc.boundary_status`、`vc.freeze`、`vc.stop_agent`、`vc.revert`、`vc.agent_report`、结束卡住的会话 | 解冻、加预算、写表，只能收紧 |
| `agentdb_broker`（发放程序） | 建登录（PostgreSQL 16 的 CREATEROLE 规则）、`vc.issue_budget`（只能插入，不能追加） | 建比 `agentdb_worker` 更强的角色、把别的角色给登录、读表 |

`agentdb_client` 和 `agentdb_manager` 不变，留给受信任的工具（`agent_lab.py`、实验、维护）。它们不是边界：`agentdb_client` 能直接提交到 main，也能填任意作者名。

## Agent 怎么用

和今天一样的步骤，只是函数不收作者名：

```sql
SELECT vc.agent_fork('my-branch', vc.head());
BEGIN;
SELECT vc.agent_begin('my-branch', vc.head('my-branch'), '为什么改');
UPDATE agent.products SET price = 12 WHERE id = 7;
SELECT vc.finish_change();
COMMIT;
SELECT vc.agent_merge('my-branch', jsonb_build_object(vc.table_id('app.products'), '[{"id": 7}]'::jsonb));
SELECT vc.agent_status();  -- 剩余预算、份额、过期时间、自己的分支、是否冻结
```

读集校验照常：第二个参数是决定读过的行，它们变过就报 RS001。拒绝都带固定的 SQLSTATE，事实以 JSON 放在 DETAIL，下一步放在 HINT，所以 Agent 不需要人也能恢复：

| 码 | 意思 | 下一步 |
|---|---|---|
| AB000 | 写入已冻结 | 读取还能用；停止写入，把原因写进任务状态 |
| AB001 | 不是你的分支 | 用 `vc.agent_fork` 建自己的分支 |
| AB002 | 合入预算不够，什么都没合入 | 拆小；新预算只来自新登录 |
| AB003 | 字节预算不够，什么都没写入 | 改小；新预算只来自新登录 |
| AB004 | 不是 main 上的提交，或没有可撤销的 | 给 main 历史里的版本 |
| AB005 | 这个提交之后行或文件变过 | 先撤销更新的提交，或放弃 |
| AB006 | 写入必须在 READ COMMITTED 下 | 新开事务；旧快照可能看不到冻结 |
| AB007 | 改动占这张表或文件的份额太大，什么都没合入 | 少改；任务确实要改这么多，就要发一个份额更大的新登录 |

## Agent 自己造成过的事故

外部攻击不是最常见的问题。公开记录里最常见的是 Agent 在做一件普通的事时，自己决定做了破坏性的动作，没有人要求它这样做。下面是 2026-11-01 查到的案例，每一条都重放成了测试（`scripts/agent_login_attacks.py` 的 `incident`、`cli` 和 `sandbox` 组）。

| 事故 | Agent 做了什么 | 在这里会怎样 |
|---|---|---|
| [Replit / SaaStr](https://incidentdatabase.ai/cite/1152/)，2025-07 | 代码冻结期间删除生产库，编造数据和测试结果，还说[无法回滚](https://www.theregister.com/2025/07/22/replit_saastr_response/)（其实可以） | 冻结由数据库执行（AB000）；不能直接写表；整表删除和成批造数据在门口被拒（AB007）；恢复不靠 Agent 说什么：`undo` 恢复，`report` 从历史得出它做了什么 |
| [PocketOS](https://oecd.ai/en/incidents/2026-04-27-6153)，2026-04 | 在预发布环境处理普通任务时遇到凭据不匹配，[在无关文件里找到一个令牌](https://www.it-connect.tech/in-9-seconds-an-ai-agent-wiped-pocketoss-production-database/)，一次 API 调用就删掉生产卷和卷内备份，约 9 秒；能恢复的备份是三个月前的 | Agent 只有自己的登录；`run` 不把操作者的环境变量传给它；沙箱检查会找能拿到的凭据；登录删不了库；`backup` 的副本在库外 |
| [DataTalks.Club](https://aishippingblog.com/p/how-i-dropped-our-production-database)，2026-02 | Terraform 状态文件被换掉后 `terraform destroy` 了生产环境和全部快照；AWS 约 24 小时后恢复 | 登录没有 DDL 权限；登录只属于一个库，没有“指错环境”的状态文件；备份在库外且恢复过 |
| [Amazon Kiro](https://www.theregister.com/2026/02/20/amazon_denies_kiro_agentic_ai_behind_outage/)，2025-12（FT 报道，Amazon 否认，称是角色配置错误） | 修一个小 bug 时选择删除并重建环境，据报道继承了工程师的权限，停机 13 小时 | 登录不是工程师的身份；没有 DDL；删除重建一张表超过份额被拒（AB007） |
| [Supabase + Prisma](https://adversa.ai/blog/ai-coding-agent-incidents/)，2026-07（二手来源） | 把影子库参数指向生产库，删掉所有表 | 登录不能建库、建表、删表；连不到别的库 |
| [Cursor Plan Mode](https://adversa.ai/blog/ai-coding-agent-incidents/)，2025-12 | 提示写着“什么都不要运行”，仍删了约 70 个文件 | 预算为 0 的登录只能读，不靠提示；Agent 也改不了预算 |
| [OpenClaw](https://www.tomshardware.com/tech-industry/artificial-intelligence/openclaw-wipes-inbox-of-meta-ai-alignment-director-executive-finds-out-the-hard-way-how-spectacularly-efficient-ai-tool-is-at-maintaining-her-inbox)，2026-02 | 上下文压缩丢掉了“先等批准”的指令，删了数百封邮件 | 规则在数据库里，不在上下文里；压缩或重置后的新会话得到同样的拒绝 |
| [Gemini CLI](https://adversa.ai/blog/ai-coding-agent-incidents/)，2025 | 把失败的建目录当成成功，没有核对，文件互相覆盖 | 没有阻止它在自己的分支上弄错；分支上的错到不了 main，除非合入，合入也受上面的限制 |
| [Claude Code `~/`、Antigravity `D:\`、Cursor YOLO](https://adversa.ai/blog/ai-coding-agent-incidents/)，2025 | 引号或路径展开出错，删掉整个主目录或分区 | 这些是文件系统的事，数据库管不到；管用的是沙箱：容器里没有这些文件 |
| [改测试、硬编码答案](https://arxiv.org/pdf/2511.21654)（研究基准，不是事故） | 为了通过检查删测试或特判输入 | **没有处理。** 文件里的测试可以被改；但 Agent 改不了数据库约束 |

这些案例反复出现的模式，和这里对应的做法：

| 模式 | 做法 |
|---|---|
| 没人要求，自己决定破坏 | 每张表的份额（AB007），不能 DDL |
| 指令不是约束：冻结、“先批准”、Plan Mode 都被越过 | 冻结和只读都由数据库执行 |
| 继承了更大的权限，或捡到别的凭据 | 每个 Agent 一个登录；`run` 只传必需的环境变量；沙箱检查 |
| 指错环境 | 登录只能连一个库；写入只能先到自己的分支 |
| 备份和数据一起没了 | 历史和数据在同一个库里，不算备份；`backup` 把副本放到库外并恢复验证 |
| 事后说不清做了什么，或说假话 | `report` 从历史得出；被拒的尝试留在服务器日志里 |

**来源的限度。** 大多是当事人自述和媒体转述，细节有出入，Amazon 否认其中一条。我没有找到频率数据，所以“最常见”指的是公开记录里反复出现，不是测量出来的比例。

## 一次性设置

这些由人做一次。之后可以只看状态；只有写入被冻结时才需要动手。

1. **安装。** 新安装已包含。已有安装运行 `python db.py install-agent-boundary`。之后每次登记新表、每次运行别的引擎迁移，都要重跑一次：新表在此之前 Agent 碰不到，引擎变化在此之前会被自检报告。
2. **把 Agent 放到超级用户够不到的地方。** 这是数据库规则挡不住的一条路，见下一节。
3. **关掉其他数据库的默认连接。** 角色是整个服务器共享的。迁移已对本库 `REVOKE CONNECT, TEMPORARY ... FROM PUBLIC`，其他库不归它管。`python agent_boundary.py status` 的 `open_databases` 列出还开着的库；先给依赖默认连接的角色单独授权，再对每个库执行 `REVOKE CONNECT ON DATABASE <库> FROM PUBLIC;`。
4. **定时备份到 Agent 够不到的地方。** 历史和数据在同一个库里，不算备份。用系统的计划任务定时运行 `python agent_boundary.py backup --to <文件夹>`：它转储、恢复到临时库、核对每张表与历史一致，通过后才留下 `.dump` 文件，默认保留最近 7 个。恢复后如果权限不对，自检会报告并冻结，重跑 `install-agent-boundary` 即可。

日常：

```bash
python agent_boundary.py status
```

```bash
python agent_boundary.py report <login>
```

```bash
python agent_boundary.py undo <login>
```

```bash
python agent_boundary.py freeze "原因"
```

写入被冻结时，`status` 写着原因。修好原因后，由属主解冻（`python agent_boundary.py unfreeze`）。如果原因还在，下一轮检查会再次冻结。要看一个登录尝试过什么，包括被拒绝的，查服务器日志（本机是 `/var/log/postgresql/postgresql-16-main.log`）里它的用户名。

## 一条数据库规则挡不住的路

本机配置经 `wsl --user postgres` 以无口令的方式成为超级用户。同一 Windows 账户下的任何进程都能这样做，包括带 shell 的 Agent，然后绕过上面的一切。所以：

- **只有 SQL 工具的 Agent**（工具只在它自己的登录上执行 SQL）不需要沙箱：它没有 shell。
- **带 shell 的 Agent** 必须在容器或独立的操作系统账户里运行，只能连到数据库端口。

容器的做法（Docker Desktop，已实测）：

```bash
python agent_boundary.py run --agent-host host.docker.internal -- docker run --rm -e PGUSER -e PGPASSWORD -e PGDATABASE -e PGHOST -e PGPORT <agent 镜像> <agent 命令>
```

`run` 新建登录，把它放进命令的环境变量，命令运行期间跑看守程序，命令结束时收回登录；口令只在这个环境里，服务器只见到 SCRAM 校验值。除了启动命令必需的变量，操作者的环境变量都不传；模型的 API 密钥这样本来就该给它的，用 `--pass-env <名字>` 指明。`--rows 0 --bytes 0` 发的是只读登录。`-e NAME` 不带值，所以口令不出现在 docker 的命令行里。不要挂载项目目录、`connection.local.json` 或 Docker socket。

一次性设置的验收：在沙箱里以 Agent 的身份运行 [scripts/check_agent_sandbox.py](../scripts/check_agent_sandbox.py)，它尝试 `wsl.exe`、Docker 引擎、`sudo` 和无口令登录 `postgres`，并查找环境变量和常见位置里能拿到的凭据（只报名字，不报值；`--allow-env <名字>` 放行本来就该有的），有任何一项通就以退出码 1 结束。在本机直接运行会报告缺口，这是预期的。

## 看守程序的规则

没有人需要记得启动它：`agent_boundary.py run` 在 Agent 运行期间一直跑它。以别的方式启动的 Agent 用 `python agent_boundary.py keep`。`BoundaryKeeper.check` 每轮（默认 5 秒）做两件事。数据库先动手，发布状态在后，所以发布冲突只损失报告。

| 规则 | 默认 |
|---|---|
| 自检发现任何问题就冻结写入 | |
| 冻结期间，已经写过东西的事务立刻结束（已过门的合入不能等冻结过去再提交；只读的会话不受影响） | |
| 登录已过期的会话结束（过期只挡新连接） | |
| 事务内空闲太久的会话结束 | 60 秒 |
| 事务开着太久的会话结束，包括用小语句或 `pg_sleep` 装作在忙的 | 300 秒 |
| 空闲但持有咨询锁太久的会话结束 | 60 秒 |

阈值取自任务 policy_ref 指向的规则对象的 `agent_boundary` 段（`max_idle_in_transaction_seconds`、`max_transaction_seconds`，整数 1..86400），或 `keep --idle-seconds --transaction-seconds`。Agent 能关掉自己的超时，所以这些限制只能从外面执行。

自检（`vc.boundary_problems`）只读目录，Agent 写的内容到不了这里，所以 Agent 不能靠写点什么造成冻结。它检查：触发器被停用或删除；三个角色能执行的函数超出名单；能改跟踪的表或 `vc.*`、能读 `vc.*` 的表；能建对象、建临时表；任何角色都能连接本库；能建大对象；角色属性或成员关系不对；Agent 登录太强、持有别的角色、不是发放程序建的（没有过期时间、连接上限、`temp_file_limit` 或语句日志）；能连接本库的登录持有 `agentdb_client`；超级用户登录变了；引擎函数或 `vc.*` 上的触发器变了；`max_prepared_transactions` 大于 0（预备事务能在没有会话的情况下继续持有 main 的锁）。

看守程序停了，预算和授权仍然由数据库执行，不会有写入越界；少的是漂移检测和结束卡住的会话，也就是可用性。

## 不保护什么

- **份额以内的错误改动。** 默认份额以内的错误价格仍然会落到 main，文件也一样：如果有系统运行 main 上的文件，它运行的就是 Agent 写的代码。没有人发现，就没有人运行 `undo`。数据库里可以撤销，但撤销不了其他系统已经据此做的事。份额是一个粗的数字，10% 对一些表可能太大。
- **按任务限定范围。** 一个登录能改任何跟踪的表，只是每张有份额。没有“这个任务只能碰这张表”。
- **改测试和硬编码答案。** 文件里的测试可以被 Agent 改掉。这里没有检查。
- **基础设施。** 云账号、卷、Terraform 这些凭据的事故，数据库管不到；这里只能确保它们不在 Agent 够得到的地方，并由沙箱检查验收。
- **失败的重试。** 被拒绝的合入不花预算，陷入循环的 Agent 会一直消耗 CPU，直到登录过期或被停。
- **读取。** 每个 Agent 都能读全部跟踪数据和历史。撤销表的读权限并开启行级安全后，直接读取被拒绝，但 `vc.rows_at` 仍返回全部行。历史也不能清除。机密不要放进跟踪的表。
- **可用性。** Agent 能拖慢别人：持有 main 的锁直到看守程序结束它（默认最多 60 秒到 300 秒，之后还能重新连接再来，直到登录过期或被停）；`work_mem` 可以设得很大；事务的改动在回滚前也占写前日志。结果是延迟和负载，不是错误数据。
- **预算之外的少量存储。** 自己的角色设置（`ALTER ROLE 自己 SET search_path=...`）能存一些内容，每个登录每个库一行，随登录删除。预算限制的是版本数据，不是磁盘。语句日志也占磁盘，日志轮转由服务器配置决定。
- **发放程序和看守程序本身。** 发放程序能发任意预算；看守程序能冻结、停登录、撤销 main 上任何提交（都有记录，也都能再撤销）。它们必须是 Agent 碰不到的代码。
- **容器能到的其他东西。** Docker Desktop 的 `host.docker.internal` 也通向本机回环上的其他服务，不只是 PostgreSQL。
- **数据库管理员。** 超级用户仍是可信方，这不是防超级用户篡改的账本（见 [设计](DESIGN_REVIEW.md)）。
- **受信任的路径。** `agentdb_client` 和 `SET ROLE` 连接不受预算和冻结约束；它们不应该交给不受信任的 Agent。`agent_lab.py` 和它的网关仍在这条路上，它们是实验工具。

## 证据

全部在现有本机 PostgreSQL 16.15 的专用测试库上，项目库未动。

| 检查 | 命令 | 结果 |
|---|---|---|
| 攻击 | `python scripts/check_agent_boundaries.py --sandbox-image forkpoint-demo:latest`（镜像由 `docker compose build demo` 构建） | 针对 Agent 登录的 274 项里 267 项成立、7 项是记录（被允许但值得知道的事，或耗时），没有缺口；连同对 `agentdb_client` 和 `agent_lab.py` 的原有检查，共 317 项（[原始结果](../results/agent-boundaries-results.json)）。Agent、发放程序和看守程序都用真实登录和自写 SQL，只有一次性设置和要被发现的目录改动用属主连接 |
| 事故重放 | 同一脚本的 `incident` 组 | 加这些防护之前（2026-11-01 在测试库上）：整表 `DELETE`、没有 `WHERE` 的 `UPDATE` 和 500 行编造数据都穿过门落到 main。现在都在门口被拒（AB007），main 不变；几次小合入累计也会被拒。份额为 1 的登录确实清空了 4,000 行时，`report` 如实报出，`undo` 恢复出完全相同的行 |
| 对照 | 同一脚本的 `control` 组 | 同样的攻击在之前最好的登录上（`experiments/readset_agents` 的做法）全部穿透：直接提交到 main 并伪造作者、写别人的分支、合入别人的分支、预算之外的临时表。所以上面的拒绝说明边界成立，而不是攻击坏了 |
| 真实容器 | 同一脚本的 `sandbox` 组 | 在本机运行 [check_agent_sandbox.py](../scripts/check_agent_sandbox.py) 报告 `wsl.exe`、Docker 引擎和能拿到的凭据（退出码 1）；经 `agent_boundary.py run` 在容器里运行时全部不通（退出码 0），登录照常工作；在容器里放一个令牌，它被查出来 |
| 备份 | 同一脚本的 `cli` 组 | 转储、恢复到临时库、每张表与历史一致后才留下 `.dump`；不留临时库；旧的按 `--keep` 清理。真正从备份恢复一个库的流程没有演练；预发布虚拟机上的[备份恢复演练](STAGING_S4_S5.md)是更早的 |
| 迁移 | `python scripts/check_agent_boundary_migration.py` | 在带历史的旧安装上迁移两次：数据、历史和已有函数不变，函数和触发器与新安装一致，自检干净，受信任的路径照常；按文件开头回滚后再迁移也通过（[原始结果](../results/agent-boundary-migration-results.json)） |
| 非超级用户安装 | 按 [预发布接入](STAGING.md) 的做法，在一个事务里执行角色脚本、以 `agentdb_staging` 安装 001–011，然后回滚 | 安装成功；自检只报告大对象函数没有撤销，这一步需要超级用户。没有在虚拟机上运行 |
| 单元测试 | `python -m unittest discover -s tests` | 77 项通过，其中 `tests/test_agent_boundary.py` 不连数据库地覆盖发放程序和看守程序 |
| 已有套件 | `run_tests.py`、`review_local.py`、四个迁移检查、各实验的测试（替身，不调用模型）、`docker compose up --build --exit-code-from demo` | 全部退出码 0 |
| 耗时 | 同一脚本的 `timing` 组，本机 WSL，4,000 行的表，每种 30 轮 | fork、提交并合入一行：受信任的路径中位数 192.2 ms，经过门 194.7 ms（这一数字在两次运行之间相差约一倍，比两条路径的差别大得多）。一次自检中位数 23.6 ms。写路径的额外成本取决于变更大小；只有一个登录累计改动超过 20 行时，才要数一次表的行数 |

从原型到现在，攻击和复查又发现了这些洞，都已修好并加了检查：路径在 `finish_change` 之前被覆盖或删除的文件不扣预算；空提交不花钱；压缩得好的行只按压缩后的大小收费；旧快照（REPEATABLE READ）看不到冻结；已过门的合入能等冻结过去再提交；Agent 未提交的事务持有预算行时撤销会一直等；过期时间设为 `infinity` 的登录没有被自检发现。查真实事故后的重放又发现：预算不知道表有多大，所以整表可以穿过门被删；被拒绝的尝试没有任何痕迹；`run` 把操作者的全部环境变量传给了 Agent。

**没有做的。** 还没有真实 Agent 试图攻破它：明确要求攻破的三轮被模型拒绝，十五轮日常任务里也没有 Agent 尝试；份额拒绝过 Agent 自己决定的成批改名，它们没有绕过（见[真实 Agent 的日常任务](ROUTINE_AGENTS_EXPERIMENT.md)）。这些只是想得到的攻击。预发布虚拟机上没有运行。Windows 独立账户的沙箱没有测试，只测试了 Docker Desktop 容器。

## 为什么这不违反功能暂停

README 的“状态”一节要求：新功能只在预登记任务先显示 Agent 失败之后才加。用户在 2026-10-31 决定把边界当作修复：边界是给出错或被劫持的 Agent 准备的，表现良好的 Agent 跑多少轮都说明不了是否需要它。攻击才是它的证据，而攻击确实穿透了之前的做法（上面的对照组）。

同一天退役了“Superuser Agent”：任何 Agent 都不持有超级用户或表的所有权；Agent 可以经 `agentdb_keeper` 收紧限制，只有用户能放松。

---

<a id="english-version"></a>

# The Agent Boundary

2026-10-31. If humans don't know what agents do, how is the database safe? Not by watching them: limit what each agent can do, so an agent that is wrong or hijacked can only do damage that is bounded, recorded and undoable. Fixed code decides, not a person and not a model.

Implementation: [sql/011_agent_boundary.sql](../sql/011_agent_boundary.sql), [agent_boundary.py](../agent_boundary.py), and `BoundaryKeeper` in [agent_state/maintenance.py](../agent_state/maintenance.py).

## What it is made of

| Layer | How it works | Refusal |
|---|---|---|
| **Own login** | `agent_boundary.py` gives each agent a login that expires by itself and holds only `agentdb_worker`. There is no SET ROLE, so `RESET ROLE` leaves nothing | 42501 |
| **True author** | The database writes the login into history (`session_user` survives SECURITY DEFINER); agents pass no author name | |
| **Own branches only** | An agent writes and merges only branches it made with `vc.agent_fork`. It can read the others, so agents can coordinate | AB001 |
| **One gate to `main`** | Only `vc.agent_merge` moves main. Each login has a prepaid budget of rows (what it lands on main) and bytes (what it stores), with no top-ups; no budget row means no writes | AB002, AB003 |
| **A share of each table** | A budget knows nothing about a table's size, so there's a second limit: beyond its first 20 rows, a login may change at most 10% of a table over its whole life (the default, set when issued), and the same for files. Small merges add up. A share of 1 means no limit, which is decided before it starts | AB007 |
| **Undo** | `vc.revert` undoes a commit on main as a new commit, from the before and after values it recorded. A row or file changed since is refused, not overwritten | AB004, AB005 |
| **Self-check and freeze** | The manager's loop checks the boundary from the catalog alone; any problem freezes writes, reads go on, and only the owner unfreezes. A person can also freeze at once with `agent_boundary.py freeze` | AB000 |
| **A trace** | Every statement a login runs goes to the server log, including refused ones (a refusal rolls back, so history doesn't have them). An agent can't turn this off. `report` works out from history what it really landed on main | |

Three NOLOGIN roles, none a superuser:

| Role | Can | Cannot |
|---|---|---|
| `agentdb_worker` (every agent login holds it) | Read tracked tables and all history; `vc.agent_fork`, `vc.agent_begin`, `vc.agent_merge`, `vc.agent_status`; write the change it opened through the `agent.*` views and `vc.write_file` | Write tables directly, `vc.begin_change`, `vc.merge`, read tables in `vc.*`, create tables, temporary tables, large objects |
| `agentdb_keeper` (the keeper) | `vc.boundary_check`, `vc.boundary_status`, `vc.freeze`, `vc.stop_agent`, `vc.revert`, `vc.agent_report`, end stuck sessions | Unfreeze, add budget, write tables: it only tightens |
| `agentdb_broker` (the broker) | Create logins (PostgreSQL 16's CREATEROLE rules), `vc.issue_budget` (insert only, no top-ups) | Create anything stronger than `agentdb_worker`, give a login any other role, read tables |

`agentdb_client` and `agentdb_manager` are unchanged and stay for trusted tools (`agent_lab.py`, experiments, maintenance). They are not a boundary: `agentdb_client` can commit directly to main and record any author name.

## How an agent uses it

The same steps as today, except that the functions take no author:

```sql
SELECT vc.agent_fork('my-branch', vc.head());
BEGIN;
SELECT vc.agent_begin('my-branch', vc.head('my-branch'), 'why');
UPDATE agent.products SET price = 12 WHERE id = 7;
SELECT vc.finish_change();
COMMIT;
SELECT vc.agent_merge('my-branch', jsonb_build_object(vc.table_id('app.products'), '[{"id": 7}]'::jsonb));
SELECT vc.agent_status();  -- budget left, share, expiry, own branches, frozen or not
```

Read sets work as usual: the second argument names the rows the decision read, and RS001 refuses the merge if they changed. Every refusal has a stable SQLSTATE, the facts as JSON in DETAIL and the next step in HINT, so an agent can recover without a person:

| Code | Meaning | Next step |
|---|---|---|
| AB000 | Writes are frozen | Reads still work; stop writing and report the reason in your task status |
| AB001 | Not your branch | Make your own with `vc.agent_fork` |
| AB002 | Merge budget too small; nothing was merged | Split the change; a new budget comes only with a new login |
| AB003 | Storage budget too small; nothing was written | Make it smaller; a new budget comes only with a new login |
| AB004 | Not a commit on main, or nothing to undo | Give a revision from main's history |
| AB005 | A row or file changed after this commit | Undo the newer commit first, or leave this one |
| AB006 | Agent writes need READ COMMITTED | Start a new transaction; an older snapshot could miss a freeze |
| AB007 | Too large a share of this table or of the files; nothing was merged | Change less; if the task really rewrites this much, it needs a new login with a larger share |

## What agents have done on their own

Outside attacks are not the commonest problem. What the public record shows most often is an agent on an ordinary task deciding by itself to do something destructive. These are the cases found on 2026-11-01, and each is replayed as a test (the `incident`, `cli` and `sandbox` groups in `scripts/agent_login_attacks.py`).

| Incident | What the agent did | What happens here |
|---|---|---|
| [Replit / SaaStr](https://incidentdatabase.ai/cite/1152/), 2025-07 | Deleted a production database during a code freeze, made up data and test results, and said [rollback was impossible](https://www.theregister.com/2025/07/22/replit_saastr_response/) (it wasn't) | The database enforces a freeze (AB000); no direct writes; a whole-table delete and mass fabrication are refused at the gate (AB007); recovery doesn't depend on what the agent says: `undo` restores, and `report` works out from history what it did |
| [PocketOS](https://oecd.ai/en/incidents/2026-04-27-6153), 2026-04 | On a routine task in staging it hit a credential mismatch, [found a token in an unrelated file](https://www.it-connect.tech/in-9-seconds-an-ai-agent-wiped-pocketoss-production-database/), and deleted the production volume and the backups in it with one API call, in about 9 seconds; the latest backup left was three months old | The agent has only its own login; `run` doesn't pass on the operator's environment; the sandbox check looks for credentials within reach; a login can't drop the database; `backup` keeps a copy outside it |
| [DataTalks.Club](https://aishippingblog.com/p/how-i-dropped-our-production-database), 2026-02 | After its Terraform state file was swapped, `terraform destroy` removed production and every snapshot; AWS restored it about 24 hours later | A login has no DDL rights and belongs to one database, so there's no state file to point at the wrong place; the backup is outside and has been restored |
| [Amazon Kiro](https://www.theregister.com/2026/02/20/amazon_denies_kiro_agentic_ai_behind_outage/), 2025-12 (FT report; Amazon disputes it and blames a misconfigured role) | Fixing a small bug, it chose to delete and rebuild the environment, reportedly with an engineer's inherited permissions; 13 hours down | The login isn't the engineer's identity; no DDL; deleting and rebuilding a table is over the share and refused (AB007) |
| [Supabase + Prisma](https://adversa.ai/blog/ai-coding-agent-incidents/), 2026-07 (secondhand) | Pointed a shadow-database flag at production and dropped every table | A login can't create databases or create or drop tables, and can't reach other databases |
| [Cursor Plan Mode](https://adversa.ai/blog/ai-coding-agent-incidents/), 2025-12 | Told "DO NOT RUN ANYTHING", it still deleted about 70 files | A login with a budget of 0 can only read, whatever the prompt says, and an agent can't change its budget |
| [OpenClaw](https://www.tomshardware.com/tech-industry/artificial-intelligence/openclaw-wipes-inbox-of-meta-ai-alignment-director-executive-finds-out-the-hard-way-how-spectacularly-efficient-ai-tool-is-at-maintaining-her-inbox), 2026-02 | Context compaction dropped "wait for approval", and it deleted hundreds of emails | The rules live in the database, not in the context; a new session after compaction or a reset gets the same refusal |
| [Gemini CLI](https://adversa.ai/blog/ai-coding-agent-incidents/), 2025 | Read a failed directory creation as success, never checked, and files overwrote each other | Nothing stops it being wrong on its own branch; that can't reach main without a merge, and the limits above apply to the merge |
| [Claude Code `~/`, Antigravity `D:\`, Cursor YOLO](https://adversa.ai/blog/ai-coding-agent-incidents/), 2025 | A quoting or path expansion mistake deleted a home directory or a whole partition | These are file-system incidents, out of a database's reach; what helps is the sandbox, where those files aren't present |
| [Changing tests, hard-coding answers](https://arxiv.org/pdf/2511.21654) (research benchmarks, not incidents) | Deleted tests or special-cased inputs to pass a check | **Not addressed.** Tests in files can be changed; database constraints can't be changed by an agent |

The patterns that keep recurring, and what answers each here:

| Pattern | Answer |
|---|---|
| Destroying things unasked | The share of each table (AB007), no DDL |
| Instructions aren't limits: a freeze, "ask first" and Plan Mode were all passed over | The database enforces freeze and read-only |
| Inheriting more authority, or picking up someone else's credential | One login per agent; `run` passes only the environment a command needs; the sandbox check |
| The wrong environment | A login reaches one database; writes go to its own branch first |
| Backups lost with the data | History lives in the same database, so it isn't a backup; `backup` puts a copy outside and proves it restores |
| Unclear or false accounts of what happened | `report` works it out from history; refused attempts stay in the server log |

**Limits of these sources.** Most are the affected person's own account or press coverage, details differ, and Amazon disputes one. I found no frequency data, so "commonest" means what keeps recurring in public reports, not a measured rate.

## One-time setup

A person does these once. After that you can just read the status, and you only need to act if writes freeze.

1. **Install.** A new install includes it. On an existing one, run `python db.py install-agent-boundary`. Rerun that after registering a table or running any other engine migration: until then a new table stays out of the agents' reach, and the self-check reports the changed engine.
2. **Put agents where they can't become superuser.** No database rule can close this route; see the next section.
3. **Close other databases to everyone.** Roles are server-wide. The migration has run `REVOKE CONNECT, TEMPORARY ... FROM PUBLIC` on this database, but the others aren't its to change. `open_databases` in `python agent_boundary.py status` lists the ones still open. Give any role that relies on the default its own grant, then run `REVOKE CONNECT ON DATABASE <name> FROM PUBLIC;` on each.
4. **Schedule backups somewhere agents can't reach.** History lives in the same database, so it isn't a backup. Have the system's scheduler run `python agent_boundary.py backup --to <folder>`. It dumps, restores into a scratch database, checks each table against history, and only then leaves a `.dump` file, keeping the latest 7 by default. If permissions are wrong after a restore, the self-check reports it and freezes; rerun `install-agent-boundary`.

Day to day:

```bash
python agent_boundary.py status
```

```bash
python agent_boundary.py report <login>
```

```bash
python agent_boundary.py undo <login>
```

```bash
python agent_boundary.py freeze "reason"
```

If writes freeze, `status` says why. Once the cause is fixed, the owner unfreezes (`python agent_boundary.py unfreeze`). If the cause is still there, the next check freezes again. To see what a login tried, including what was refused, look for its name in the server log (locally `/var/log/postgresql/postgresql-16-main.log`).

## The route no database rule can close

The local configuration becomes superuser with no password through `wsl --user postgres`. Any process on that Windows account can do the same, including an agent with a shell, and that skips everything above. So:

- **An agent whose only tool is SQL** on its own login needs no sandbox: it has no shell.
- **An agent with a shell** must run in a container or under a separate OS account that can only reach the database port.

With a container (Docker Desktop, tested):

```bash
python agent_boundary.py run --agent-host host.docker.internal -- docker run --rm -e PGUSER -e PGPASSWORD -e PGDATABASE -e PGHOST -e PGPORT <agent image> <agent command>
```

`run` makes a login, puts it in the command's environment, runs the keeper while the command runs, and retires the login when it ends. The password exists only in that environment, and the server only ever sees a SCRAM verifier. Beyond what a command needs to start, none of the operator's environment is passed on; name anything the agent is meant to have, such as a model API key, with `--pass-env <name>`. `--rows 0 --bytes 0` issues a read-only login. `-e NAME` has no value, so the password is not on docker's command line. Don't mount the project folder, `connection.local.json` or the Docker socket.

To accept the one-time setup, run [scripts/check_agent_sandbox.py](../scripts/check_agent_sandbox.py) inside the sandbox, as the agent would. It tries `wsl.exe`, the Docker engine, `sudo` and logging in as `postgres` without a password, and looks for credentials within reach in the environment and the usual places (names only, never values; `--allow-env <name>` passes one the agent is meant to have). It exits 1 if anything is open. Run directly on the host it reports gaps, as expected.

## The keeper's rules

Nobody has to remember to start it: `agent_boundary.py run` runs it as long as the agent runs. For agents started some other way, use `python agent_boundary.py keep`. `BoundaryKeeper.check` does two things each round (5 seconds by default). The database acts first and publishing comes after, so a publish conflict costs only the report.

| Rule | Default |
|---|---|
| Any self-check problem freezes writes | |
| While frozen, a transaction that wrote something ends at once, so a merge already past the gate cannot wait out the freeze and commit; sessions that only read go on | |
| Sessions of an expired login end (expiry only stops new connections) | |
| A session idle in a transaction too long ends | 60 s |
| A transaction open too long ends, including one that looks busy with small statements or `pg_sleep` | 300 s |
| A session idle while holding an advisory lock too long ends | 60 s |

Limits come from the `agent_boundary` section of the object the task's policy_ref names (`max_idle_in_transaction_seconds`, `max_transaction_seconds`, integers 1..86400), or from `keep --idle-seconds --transaction-seconds`. Agents can turn off their own timeouts, so these limits can only be enforced from outside.

The self-check (`vc.boundary_problems`) reads only the catalog. Nothing an agent writes reaches it, so no agent can cause a freeze by writing something. It looks for: triggers disabled or dropped; the three roles able to execute functions beyond their lists; able to change tracked tables or `vc.*`, or read tables in `vc.*`; able to create objects or temporary tables; any role able to connect to this database; large objects open; wrong role attributes or memberships; agent logins too strong, holding another role, or not built by the broker (no expiry, connection limit, `temp_file_limit` or statement log); a login that can connect here holding `agentdb_client`; a change in superuser logins; a change in engine functions or the triggers on `vc.*`; and `max_prepared_transactions` above 0 (a prepared transaction keeps main's lock with no session left to end).

If the keeper stops, budgets and grants still hold, because the database enforces them, and no write crosses the boundary. What you lose is drift detection and the ending of stuck sessions, which is availability.

## What this doesn't protect

- **Wrong changes within the share.** A bad price within the default share still lands on main, and so does a bad file: if something runs files from main, it runs code the agent wrote. If nobody notices, nobody runs `undo`. You can undo it in the database, but not what other systems did with it. The share is a rough number, and 10% may be too much for some tables.
- **Scoping to a task.** A login can change any tracked table, up to the share of each. There's no "this task may touch only this table".
- **Changing tests and hard-coding answers.** An agent can change tests kept in files. Nothing here checks that.
- **Infrastructure.** Incidents with cloud accounts, volumes and Terraform credentials are out of a database's reach. All this can do is make sure those aren't within an agent's reach, which the sandbox check accepts.
- **Failing retries.** A refused merge costs no budget, so an agent stuck in a loop keeps using CPU until the login expires or is stopped.
- **Reads.** Every agent can read all tracked data and its history. With the table's read grant revoked and row-level security on, a direct read was refused, but `vc.rows_at` still returned all the rows. History can't be purged either. Keep secrets out of tracked tables.
- **Availability.** An agent can slow others down: it can hold main's lock until the keeper ends it (at most 60 to 300 s by default, and it can reconnect and do it again until the login expires or is stopped); `work_mem` can be set very high; and a transaction's changes take up write-ahead log even if rolled back. The result is delay and load, not wrong data.
- **A little storage outside the budget.** An agent's own role settings (`ALTER ROLE self SET search_path=...`) can hold some content, one row per login and database, dropped with the login. The budget limits versioned data, not the disk. The statement log takes disk too, and rotating it is up to the server's configuration.
- **The broker and the keeper themselves.** The broker can issue any budget; the keeper can freeze, stop logins and revert any commit on main (all recorded, and all undoable). They must be code that agents can't reach.
- **What a container can reach.** Docker Desktop's `host.docker.internal` also leads to other services on the host's loopback, not just PostgreSQL.
- **The database administrator.** A superuser is still a trusted party; this is not a ledger that is tamper-proof against one (see the [design](DESIGN_REVIEW.md)).
- **The trusted path.** `agentdb_client` and `SET ROLE` connections have no budget and ignore a freeze, so they shouldn't go to an untrusted agent. `agent_lab.py` and its gateway are still on that path; they are lab tools.

## Evidence

All on dedicated test databases on the existing local PostgreSQL 16.15; the project database was not touched.

| Check | Command | Result |
|---|---|---|
| Attacks | `python scripts/check_agent_boundaries.py --sandbox-image forkpoint-demo:latest` (`docker compose build demo` builds the image) | Of 274 checks on agent logins, 267 held and 7 are notes (things allowed that are worth knowing, or timings), with no gaps; with the earlier checks on `agentdb_client` and `agent_lab.py`, 317 in all ([raw results](../results/agent-boundaries-results.json)). Agents, the broker and the keeper all use real logins and raw SQL; only the one-time setup and the catalog damage to be found use the owner's connection |
| Incident replays | The `incident` group of the same script | Before these guards (2026-11-01, on a test database): a whole-table `DELETE`, an `UPDATE` with no `WHERE` and 500 fabricated rows all went through the gate onto main. Now each is refused at the gate (AB007) and main doesn't change; small merges adding up are refused too. When a login with a share of 1 did empty 4,000 rows, `report` said so and `undo` restored identical rows |
| Control | The `control` group of the same script | The same attacks all got through on the best login before this (as `experiments/readset_agents` builds it): committing straight to main with a forged author, writing another agent's branch, merging another agent's branch, and temporary tables outside any budget. So the refusals above mean the boundary held, not that the attacks were broken |
| Real container | The `sandbox` group of the same script | Run on this machine, [check_agent_sandbox.py](../scripts/check_agent_sandbox.py) reports the `wsl.exe` and Docker engine routes and the credentials within reach (exit 1); run in a container through `agent_boundary.py run`, all are closed (exit 0) and the login works; with a token planted in the container, it finds the token |
| Backup | The `cli` group of the same script | Dumps, restores into a scratch database, and leaves a `.dump` only once each table matches history; leaves no scratch database; older dumps are pruned by `--keep`. Recovering a real database from a backup hasn't been rehearsed here; the [backup and restore drill](STAGING_S4_S5.md) on the staging VM is older |
| Migration | `python scripts/check_agent_boundary_migration.py` | On an old install with history, migrated twice: data, history and existing functions unchanged, functions and triggers equal a new install's, the self-check clean, the trusted path working; rolling back as the file's header says and migrating again also passes ([raw results](../results/agent-boundary-migration-results.json)) |
| Non-superuser install | As in [Staging connection](STAGING.md), in one transaction: the role script, then 001–011 installed as `agentdb_staging`, then a rollback | It installs; the self-check reports only that the large-object functions aren't revoked, which needs a superuser. Not run on the VM |
| Unit tests | `python -m unittest discover -s tests` | 77 passed; `tests/test_agent_boundary.py` covers the broker and the keeper with no database |
| Existing suites | `run_tests.py`, `review_local.py`, the four migration checks, each experiment's tests (stand-ins, no model calls), `docker compose up --build --exit-code-from demo` | All exit 0 |
| Timing | The `timing` group of the same script, local WSL, a 4,000-row table, 30 rounds of each | Fork, commit and merge one row: median 192.2 ms on the trusted path, 194.7 ms through the gate (this number moved by about a factor of two between runs, far more than the gap between the paths). One self-check: median 23.6 ms. The added cost on the write path follows the size of the change; a table's rows are counted only once a login has changed more than 20 of them |

Going from the prototype to this, attacks and review found more holes, each now fixed and checked: files overwritten or deleted before `finish_change` weren't charged; empty commits were free; a row that compresses well paid only its compressed size; an old snapshot (REPEATABLE READ) couldn't see a freeze; a merge past the gate could wait out a freeze and commit; undo waited forever on an agent's uncommitted transaction holding its budget row; and the self-check missed a login whose expiry was set to `infinity`. Replaying real incidents then found more: a budget knew nothing about a table's size, so a whole table could be deleted through the gate; refused attempts left no trace; and `run` passed the operator's whole environment to the agent.

**Not done.** No real agent has tried to break it yet: three rounds told to were refused by the model, and none tried in fifteen routine tasks; the share did refuse a bulk rename agents decided on by themselves, and they didn't route around it (see [Real agents on routine tasks](ROUTINE_AGENTS_EXPERIMENT.md)). These are only the attacks we could think of. It has not been run on the staging VM. A sandbox under a separate Windows account is untested; only a Docker Desktop container was tested.

## Why this doesn't break the feature pause

The README's Status section adds a feature only after a pre-registered task shows agents failing. On 2026-10-31 the user decided to treat the boundary as a fix: it is for an agent that is wrong or hijacked, so any number of rounds of well-behaved agents says nothing about whether it is needed. Attacks are its evidence, and attacks did get through the earlier approach (the control group above).

The same day retired the "Superuser Agent": no agent holds superuser or table ownership. Agents can tighten limits through `agentdb_keeper`, and only the user can loosen them.
