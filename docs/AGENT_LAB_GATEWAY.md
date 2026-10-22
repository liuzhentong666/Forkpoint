中文 | [English](#english-version)

# agent_lab 常驻网关：设计与本机结果

2026-10-18。代码为 [agent_lab_gateway.py](../agent_lab_gateway.py)，默认关闭。只连接现有 WSL PostgreSQL 中的 `agentdb_test_gpt_*` 专用库，不启停数据库，也不访问任何云服务。目前没有任何真实 GPT 实验通过网关运行；[十轮实验](GPT_REPEAT_EXPERIMENT.md) 使用的仍是逐次起进程的路径。

## 问题

真实 GPT 实验的每次工具调用都由 `run_gpt_repeats.py` 启动一个新的 `agent_lab.py` 进程，每条 SQL 都要再起一个 `wsl.exe` + psql。[db.Session](../README.md) 复用一个 psql 进程，每次调用约 1 ms，但它只能存活在一个进程之内，工具调用一结束就没了。要让 Agent 受益，需要有一个常驻进程替它持有 Session。

## 方案选择

**不放进 `agent_state/server.py`。** 原因有三：

- 它启动时必须连上一个已存在的 bucket（`config.py`：“no disk/memory persistence fallback”）。云端工作目前暂缓，挂在它上面就等于本机实验也要依赖云配置。
- 它属于 ECS 部署包（`deploy/build_ecs_bundle.py` 的显式清单），而清单有意不含 `db.py` 和本机连接配置。加入 PG 执行会把本机测试库的超级用户连接带进部署物。
- 它允许在有令牌时监听非回环地址；本网关无论如何只能监听 `127.0.0.1`。管理状态发布与 PG 执行本来就不共享事务，合并成一个服务没有正确性上的收益。

**也不新建一套并行服务。** 网关没有自己的 API：一个请求就是 `agent_lab.py` 的命令行参数本身，由 `agent_lab` 自己的解析器、检查和动作代码执行，只是把每次新建的 `Connection` 换成复用的 `Session`。HTTP 约定沿用 `server.py` 和 `client.py` 的做法：只接受 Content-Length 请求体并限制大小，`Cache-Control: no-store`，一次请求一个连接，不记录请求体，不隐式重试，以及“结果未知”的报告方式。

**Session 按连接设置和数据库组成池，不按 Agent 身份分配。** 除 propose/merge 之外，工具调用并不携带 actor；而 Session 每次调用后都会回滚事务、执行 `DISCARD ALL` 并重置 psql 变量，下一次调用拿到哪个空闲 Session 都不影响结果。每个 Session 同一时刻只服务一个请求，两个 Agent 同时调用时各用一个 Session，并发方式与原来各起一个进程相同。全部 Session 都以 `agentdb_client` 身份 SET ROLE，与原路径一致。

## 设计

| 方面 | 做法 |
|---|---|
| 启用 | `python agent_lab_gateway.py --rendezvous FILE` 监听 `127.0.0.1` 的临时端口，并以 `O_EXCL` 写出约会文件（端口、随机令牌、pid）。只有当 `agent_lab.py` 所在环境设置了 `AGENTDB_LAB_GATEWAY=FILE` 时才会使用网关 |
| 未运行时 | 变量未设置、文件不存在，或连接被拒绝（请求尚未发出），`agent_lab.py` 都按原路径执行。网关模块只在设置了变量时才导入 |
| 请求 | `{argv, cwd, fingerprint}`。客户端先在本进程内完成原有的全部参数检查，网关再用同一套代码检查一次，所以绕过客户端直接发请求也逃不过检查。`init` 和 `--password` 一律不经网关 |
| 连接一致 | fingerprint 包括解析后的连接设置、角色、psql 命令前缀以及 PG* 环境变量（口令只记“已设置”）。与网关自己算出的不一致时返回 409 `gateway_mismatch`，不执行任何操作。相对路径的 `--config` 按客户端的工作目录解析 |
| 输出 | 网关返回 stdout、stderr 和退出码（0/1/2），客户端原样写出并以同一退出码结束。未捕获的异常照旧以 traceback 加退出码 1 报告 |
| 上限 | 所有数据库合计最多 `--max-sessions` 个 Session（1–32，默认 8）。都在使用中时，先关闭最久空闲的一个；没有空闲的就直接返回 503 `gateway_busy`，不排队，也不执行。请求体上限 64 KiB，响应上限 4 MiB |
| 空闲 | Session 空闲满 `--idle-timeout`（1–3600 s，默认 600 s）即关闭；超过同样时长没有新请求时，网关自行退出，先删除自己的约会文件，再关闭全部 Session。之后的调用自动回到原路径 |
| 重试 | 从不重试。请求发出后连接中断、超时或收到不完整的回复，报告 `gateway_outcome_unknown`（退出码 1），提示先读取当前状态再决定是否重做。只有在请求未发出或被明确拒绝（400/401/409/503）时，才说明“nothing was executed” |
| 认证 | 每次启动生成随机 Bearer 令牌，用 `hmac.compare_digest` 比较。先读完有界的请求体再回复（见下文 Windows RST），然后检查令牌 |

客户端每次调用都要启动一个新进程，因此尽量少导入模块。`agent_lab.py` 只在 `init` 时才导入 `review_local`，工作动作直接用 `db.Connection`（`connect` 的三行逻辑与 `review_local.connect` 相同）。网关客户端只用 `socket`，HTTP 服务端模块在服务进程里才导入。在这台机器上，原导入链（含 `review_local`、状态模块和 HTTP 模块）约需 1.0 s，现在约 0.45 s，其中空解释器启动约 0.27 s。

## 行为保持的核验

- **黄金样本**：改动前用脚本化的 worker 运行原 `agent_lab.py` 的 20 个场景（各动作成功与失败、参数错误、陈旧 head、断言失败），记录 stdout、退出码、stderr 以及发出的每条 SQL，存为 [tests/agent_lab_golden.json](../tests/agent_lab_golden.json)。之后补充的场景使总数达到 23 个；2026-10-21 起非法分支、缺少的版本参数、无法解析的代码和非数字取值在发出 SQL 前以退出码 2 的参数错误拒绝，对应样本已重新生成。2026-10-22 加入 `merge --reads` 的 6 个场景，共 29 个；原有样本的 stdout、退出码和 SQL 不变，只有参数错误时 argparse 打印的用法行多了 `--reads`。该选项的检查、发出的 SQL 与 RS001 报告另由 [tests/test_agent_lab_reads.py](../tests/test_agent_lab_reads.py) 的 5 项测试说明，7 次变异检查均被发现。无网关路径与网关路径都必须与之一致（traceback 只比较最后一行）。
- **单元测试**：[tests/test_agent_lab_gateway.py](../tests/test_agent_lab_gateway.py) 共 11 项，覆盖：未运行、变量已设置但文件不存在、陈旧文件这三种情况均回到原路径；网关二次检查参数、令牌、`init` 和 fingerprint 不一致；两个并发调用使用两个 Session，之后复用；繁忙时拒绝；请求发出后断开时只报告结果未知且只发送一次；空闲后关闭 Session、退出并删除文件；不会接管已有的约会文件；以及各项上限。另做了三次变异检查（改输出缩进、去掉网关侧检查、在出错后补发请求），测试均能发现。
- **全套**：`.\.venv-cloud\Scripts\python.exe -m unittest discover -s tests` 共 64 项通过；`experiments/gpt_repeat` 的计分测试通过。
- **Windows RST**：起初 401 回复在读取请求体之前发出，连续 15 轮测试中有 3 轮客户端收到 `ConnectionAbortedError`，原因是在 Windows 上关闭仍有未读数据的套接字会发送 RST，回复因此丢失。改为先读请求体后，连续 25 轮 0 失败。`agent_state/server.py` 同样在读取请求体之前就回复 401，本次没有修改。

## 本机真实 PostgreSQL 对比

`python scripts/compare_agent_lab_gateway.py` 用同一串 31 次调用（init、fork、propose、diff、read-code、陈旧 expected 的 propose、merge 成功/40001/整行冲突、verify 失败与成功、非法分支、参数错误；2026-10-22 起另有 11 次 `merge --reads` 相关调用：从落后于 main 的基线再次合入已发布的库存扣减、按旧的 101 行定价，两者都报 `read_conflict`，从当前 main 合入成功并通过 verify，以及 `invalid_read_set`，脚本还核对这几次的结果）分别跑两个新建测试库：一个逐次起进程，一个经网关。网关以 `--idle-timeout 10` 启动，最后自行退出。[结果](../results/agent-lab-gateway-results.json) 记录了源文件哈希，以及运行期间源文件是否被改动过。下表为 2026-10-18 的运行；结果文件现为 2026-10-22 加入 `merge --reads` 后的重跑（31 次输出全部相同，读集调用的结果都符合预期；30 次工作调用中位 722 ms / 509 ms，合计 24.43 s / 15.58 s），库名见该文件。

| 本机，2026-10-18 | 逐次起进程 | 经网关 |
|---|---:|---:|
| 20 次输出（库名、UUID 归一化后） | — | 全部相同 |
| 19 次工作调用的中位耗时（含客户端进程启动） | 671 ms | 449 ms |
| 19 次工作调用合计 | 14.84 s | 8.80 s |
| 网关内部每个动作耗时 | — | 首次 238 ms（含建立 Session），其余 4–55 ms，中位 20 ms |
| 网关空闲退出 | — | 最后一次调用后 11.6 s 退出，返回码 0，约会文件已删除；WSL 内没有残留 psql 进程或会话临时文件 |

保留的库为 `agentdb_test_gpt_495bd3c5af`（逐次起进程）和 `agentdb_test_gpt_43af910cdc`（网关）。同一天另外三次未保存的运行中，逐次起进程的中位耗时为 771–1,163 ms，网关为 411–536 ms，19 次合计分别为 16.6–23.1 s 和 7.9–10.4 s。`wsl.exe` 的启动耗时在不同时段波动很大，因此结论看比例。在只去掉 psql 启动、客户端仍导入原依赖的那一版中，中位耗时与逐次起进程几乎相同（1,227 ms 对 1,274 ms），这说明客户端的导入开销必须一并削减。

## 限制与后续

- 每次调用仍要启动约 0.4–0.5 s 的 Python 客户端，这部分已成为主要开销。要进一步降低，需要实验运行器直接请求网关、不再启动 `agent_lab.py`。但这会改变已登记实验的工具接入方式，必须作为新协议重新登记，本次没有做。
- 用网关跑登记实验之前，`run_gpt_repeats.py` 的 `sources_sha256` 需要加入 `agent_lab_gateway.py`，并在 protocol.json 中记录所用传输方式和网关参数；目前两项都没有记录。陈旧文件回退时 stdout 不变，只能对照网关日志中的请求数来确认实际用了哪条路径。
- 陈旧约会文件（网关被强制终止）不会阻止调用：连接被拒绝后改走原路径，但 Windows 上每次拒绝要等约 2 s，直到有人删除该文件；新的网关也会因为文件已存在而拒绝启动。
- 任何能读取约会文件的同一用户进程都可以发起请求。可执行的范围与直接运行 `agent_lab.py` 加本机连接配置相同，不比后者更多。这不是操作系统沙箱；SET ROLE 也不是独立的登录身份。
- 单次 SQL 仍以 120 s 为超时；客户端整体等待 130 s，实验运行器本身的 120 s 超时仍然有效。客户端被终止时，网关仍会把已开始的调用执行完，这与原来终止进程时的情形相同，结果都需要事后读取确认。

---

<a id="english-version"></a>

# agent_lab Long-Running Gateway: Design and Local Results

2026-10-18. The code is [agent_lab_gateway.py](../agent_lab_gateway.py), off by default. It only connects to the dedicated `agentdb_test_gpt_*` databases in the existing WSL PostgreSQL, does not start or stop the database, and does not access any cloud service. So far no real GPT experiment has run through the gateway; the [ten-round experiment](GPT_REPEAT_EXPERIMENT.md) still used the spawn-a-process-per-call path.

## Problem

For each tool call in the real GPT experiment, `run_gpt_repeats.py` starts a new `agent_lab.py` process, and each SQL statement starts yet another `wsl.exe` + psql. [db.Session](../README.md) reuses one psql process at about 1 ms per call, but it can only live within one process and is gone as soon as the tool call ends. For Agents to benefit, a long-running process is needed to hold the Session for them.

## Choice of approach

**Not put into `agent_state/server.py`.** There are three reasons:

- At startup it must connect to an existing bucket (`config.py`: "no disk/memory persistence fallback"). Cloud work is currently on hold, and hanging off it would mean local experiments also depend on cloud configuration.
- It belongs to the ECS deployment bundle (the explicit manifest in `deploy/build_ecs_bundle.py`), and the manifest intentionally excludes `db.py` and the local connection configuration. Adding PG execution would bring the local test databases' superuser connection into the deployment artifact.
- It allows listening on non-loopback addresses when there is a token; this gateway can only ever listen on `127.0.0.1`. Management state publishing and PG execution do not share a transaction anyway, so merging them into one service brings no correctness benefit.

**Nor build a separate parallel service.** The gateway has no API of its own: a request is just `agent_lab.py`'s command-line arguments, executed by `agent_lab`'s own parser, checks and action code, only replacing the `Connection` created each time with a reused `Session`. The HTTP conventions follow `server.py` and `client.py`: only Content-Length request bodies are accepted, with a size limit, `Cache-Control: no-store`, one connection per request, request bodies are not logged, no implicit retries, and the same way of reporting "outcome unknown".

**Sessions are pooled by connection settings and database, not assigned by Agent identity.** Apart from propose/merge, tool calls do not carry an actor; and after each call a Session rolls back the transaction, runs `DISCARD ALL` and resets psql variables, so whichever idle Session the next call gets does not affect the result. Each Session serves only one request at a time; when two Agents call at the same time each uses its own Session, the same concurrency as each starting its own process before. All Sessions SET ROLE to `agentdb_client`, the same as the original path.

## Design

| Aspect | Approach |
|---|---|
| Enabling | `python agent_lab_gateway.py --rendezvous FILE` listens on an ephemeral port on `127.0.0.1` and writes the rendezvous file (port, random token, pid) with `O_EXCL`. The gateway is used only when the environment of `agent_lab.py` sets `AGENTDB_LAB_GATEWAY=FILE` |
| When not running | If the variable is not set, the file does not exist, or the connection is refused (the request has not been sent yet), `agent_lab.py` runs on the original path. The gateway module is imported only when the variable is set |
| Request | `{argv, cwd, fingerprint}`. The client first completes all the original argument checks in its own process, and the gateway checks again with the same code, so sending a request directly while bypassing the client does not escape the checks either. `init` and `--password` never go through the gateway |
| Connection consistency | The fingerprint includes the resolved connection settings, role, psql command prefix and PG* environment variables (for the password only "set" is recorded). When it differs from what the gateway computes itself, 409 `gateway_mismatch` is returned and nothing is executed. A relative `--config` path is resolved against the client's working directory |
| Output | The gateway returns stdout, stderr and the exit code (0/1/2); the client writes them out unchanged and exits with the same code. Uncaught exceptions are reported as before, with a traceback and exit code 1 |
| Limits | At most `--max-sessions` Sessions across all databases (1–32, default 8). When all are in use, the longest-idle one is closed first; if none is idle, 503 `gateway_busy` is returned directly, without queuing or executing. Request bodies are limited to 64 KiB, responses to 4 MiB |
| Idle | A Session that has been idle for `--idle-timeout` (1–3600 s, default 600 s) is closed; when there are no new requests for the same duration, the gateway exits on its own, first deleting its rendezvous file and then closing all Sessions. Later calls automatically fall back to the original path |
| Retries | Never retries. If the connection drops, times out or receives an incomplete reply after the request was sent, it reports `gateway_outcome_unknown` (exit code 1), advising to read the current state first before deciding whether to redo it. Only when the request was not sent or was explicitly rejected (400/401/409/503) does it say "nothing was executed" |
| Authentication | A random Bearer token is generated on each start and compared with `hmac.compare_digest`. The bounded request body is read in full before replying (see the Windows RST item below), then the token is checked |

The client starts a new process for every call, so it imports as few modules as possible. `agent_lab.py` imports `review_local` only for `init`; the work actions use `db.Connection` directly (the three lines of `connect` logic are the same as `review_local.connect`). The gateway client uses only `socket`, and the HTTP server modules are imported only in the service process. On this machine the original import chain (including `review_local`, the state modules and the HTTP modules) took about 1.0 s, and now about 0.45 s, of which starting an empty interpreter is about 0.27 s.

## Verification that behavior is preserved

- **Golden samples**: before the change, a scripted worker ran 20 scenarios of the original `agent_lab.py` (success and failure of each action, argument errors, stale head, assertion failures), recording stdout, exit code, stderr and every SQL statement issued, saved as [tests/agent_lab_golden.json](../tests/agent_lab_golden.json). Scenarios added later bring the total to 23; since 2026-10-21 invalid branches, missing revision options, unparsable code and non-numeric values are rejected before any SQL as argument errors with exit code 2, and the affected samples were regenerated. On 2026-10-22, 6 scenarios for `merge --reads` brought the total to 29; the existing samples keep their stdout, exit codes and SQL, and only the usage lines argparse prints on an argument error now list `--reads`. The 5 tests in [tests/test_agent_lab_reads.py](../tests/test_agent_lab_reads.py) state what that option checks, the SQL it sends and how it reports RS001; they caught all 7 mutations tried. Both the no-gateway path and the gateway path must match it (for tracebacks only the last line is compared).
- **Unit tests**: [tests/test_agent_lab_gateway.py](../tests/test_agent_lab_gateway.py) has 11 items in total, covering: not running, variable set but file missing, and stale file, all three falling back to the original path; the gateway re-checking arguments, the token, `init` and fingerprint mismatches; two concurrent calls using two Sessions, which are then reused; rejection when busy; when disconnected after the request is sent, only reporting outcome unknown and sending only once; closing Sessions, exiting and deleting the file after idling; not taking over an existing rendezvous file; and each of the limits. Three mutation checks were also done (changing output indentation, removing the gateway-side checks, re-sending the request after an error), and the tests caught all of them.
- **Full suite**: `.\.venv-cloud\Scripts\python.exe -m unittest discover -s tests` passed 64 items in total; the scoring tests of `experiments/gpt_repeat` passed.
- **Windows RST**: at first the 401 reply was sent before reading the request body, and in 3 of 15 consecutive test rounds the client received `ConnectionAbortedError`, because on Windows closing a socket that still has unread data sends an RST, so the reply was lost. After changing to read the request body first, 25 consecutive rounds had 0 failures. `agent_state/server.py` likewise replies 401 before reading the request body; it was not modified this time.

## Comparison on the real local PostgreSQL

`python scripts/compare_agent_lab_gateway.py` runs the same sequence of 31 calls (init, fork, propose, diff, read-code, propose with a stale expected, merge success/40001/whole-row conflict, verify failure and success, invalid branch, argument error; since 2026-10-22 also 11 calls around `merge --reads`: merging the already published stock decrement again and a price decided from row 101 as it was, both from a base behind main and both `read_conflict`, a merge from current main that lands and passes verify, and `invalid_read_set`, whose outcomes the script also checks) against two newly created test databases: one spawning a process per call, one through the gateway. The gateway is started with `--idle-timeout 10` and exits on its own at the end. The [results](../results/agent-lab-gateway-results.json) record the source file hashes, and whether the source files were changed during the run. The table below is the 2026-10-18 run; the results file now holds the rerun after `merge --reads` was added on 2026-10-22 (all 31 outputs identical, every read-set call with its expected outcome; over the 30 work calls, medians 722 ms / 509 ms, totals 24.43 s / 15.58 s), with its database names in the file.

| Local, 2026-10-18 | Spawn per call | Through gateway |
|---|---:|---:|
| 20 outputs (after normalizing database names and UUIDs) | — | All identical |
| Median time of the 19 work calls (including client process startup) | 671 ms | 449 ms |
| Total of the 19 work calls | 14.84 s | 8.80 s |
| Time per action inside the gateway | — | First 238 ms (including establishing the Session), the rest 4–55 ms, median 20 ms |
| Gateway idle exit | — | Exited 11.6 s after the last call, return code 0, rendezvous file deleted; no leftover psql processes or session temporary files in WSL |

The databases kept are `agentdb_test_gpt_495bd3c5af` (spawn per call) and `agentdb_test_gpt_43af910cdc` (gateway). In three other unsaved runs on the same day, the median for spawn per call was 771–1,163 ms and for the gateway 411–536 ms, with 19-call totals of 16.6–23.1 s and 7.9–10.4 s respectively. The startup time of `wsl.exe` fluctuates a lot at different times, so the conclusion is about ratios. In the version that only removed psql startup while the client still imported the original dependencies, the median was almost the same as spawn per call (1,227 ms vs 1,274 ms), which shows that the client's import overhead must be cut as well.

## Limitations and next steps

- Each call still has to start a Python client of about 0.4–0.5 s, which has become the main overhead. Reducing it further would need the experiment runner to request the gateway directly instead of starting `agent_lab.py`. But this changes how tools are connected in the registered experiment, and must be registered again as a new protocol; it was not done this time.
- Before running a registered experiment through the gateway, `agent_lab_gateway.py` needs to be added to `run_gpt_repeats.py`'s `sources_sha256`, and the transport used and gateway parameters recorded in protocol.json; neither is currently recorded. When falling back because of a stale file, stdout is unchanged, so which path was actually used can only be confirmed by comparing against the request count in the gateway log.
- A stale rendezvous file (gateway forcibly terminated) does not block calls: after the connection is refused they take the original path, but on Windows each refusal takes about 2 s, until someone deletes the file; a new gateway also refuses to start because the file already exists.
- Any process of the same user that can read the rendezvous file can send requests. What can be executed is the same as running `agent_lab.py` directly with the local connection configuration, no more. This is not an operating system sandbox; SET ROLE is not a separate login identity either.
- A single SQL statement still has a 120 s timeout; the client waits 130 s overall, and the experiment runner's own 120 s timeout still applies. When the client is terminated, the gateway still finishes executing a call that has started, the same as when a process was terminated before; in both cases the result needs to be read afterwards to confirm.
