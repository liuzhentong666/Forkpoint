[English](AGENT_LAB_GATEWAY.en.md) | 中文

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

- **黄金样本**：改动前用脚本化的 worker 运行原 `agent_lab.py` 的 20 个场景（各动作成功与失败、参数错误、陈旧 head、断言失败），记录 stdout、退出码、stderr 以及发出的每条 SQL，存为 [tests/agent_lab_golden.json](../tests/agent_lab_golden.json)。无网关路径与网关路径都必须与之一致（traceback 只比较最后一行）。
- **单元测试**：[tests/test_agent_lab_gateway.py](../tests/test_agent_lab_gateway.py) 共 11 项，覆盖：未运行、变量已设置但文件不存在、陈旧文件这三种情况均回到原路径；网关二次检查参数、令牌、`init` 和 fingerprint 不一致；两个并发调用使用两个 Session，之后复用；繁忙时拒绝；请求发出后断开时只报告结果未知且只发送一次；空闲后关闭 Session、退出并删除文件；不会接管已有的约会文件；以及各项上限。另做了三次变异检查（改输出缩进、去掉网关侧检查、在出错后补发请求），测试均能发现。
- **全套**：`.\.venv-cloud\Scripts\python.exe -m unittest discover -s tests` 共 64 项通过；`experiments/gpt_repeat` 的计分测试通过。
- **Windows RST**：起初 401 回复在读取请求体之前发出，连续 15 轮测试中有 3 轮客户端收到 `ConnectionAbortedError`，原因是在 Windows 上关闭仍有未读数据的套接字会发送 RST，回复因此丢失。改为先读请求体后，连续 25 轮 0 失败。`agent_state/server.py` 同样在读取请求体之前就回复 401，本次没有修改。

## 本机真实 PostgreSQL 对比

`python compare_agent_lab_gateway.py` 用同一串 20 次调用（init、fork、propose、diff、read-code、陈旧 expected 的 propose、merge 成功/40001/整行冲突、verify 失败与成功、非法分支、参数错误）分别跑两个新建测试库：一个逐次起进程，一个经网关。网关以 `--idle-timeout 10` 启动，最后自行退出。[结果](../agent-lab-gateway-results.json) 记录了源文件哈希，以及运行期间源文件是否被改动过。

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
