English | [中文](AGENT_LAB_GATEWAY.md)

# agent_lab Long-Running Gateway: Design and Local Results

2026-10-18. The code is [agent_lab_gateway.py](../agent_lab_gateway.py), off by default. It only connects to the dedicated `agentdb_test_gpt_*` databases in the existing WSL PostgreSQL, does not start or stop the database, and does not access any cloud service. So far no real GPT experiment has run through the gateway; the [ten-round experiment](GPT_REPEAT_EXPERIMENT.en.md) still used the spawn-a-process-per-call path.

## Problem

For each tool call in the real GPT experiment, `run_gpt_repeats.py` starts a new `agent_lab.py` process, and each SQL statement starts yet another `wsl.exe` + psql. [db.Session](../README.en.md) reuses one psql process at about 1 ms per call, but it can only live within one process and is gone as soon as the tool call ends. For Agents to benefit, a long-running process is needed to hold the Session for them.

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

- **Golden samples**: before the change, a scripted worker ran 20 scenarios of the original `agent_lab.py` (success and failure of each action, argument errors, stale head, assertion failures), recording stdout, exit code, stderr and every SQL statement issued, saved as [tests/agent_lab_golden.json](../tests/agent_lab_golden.json). Both the no-gateway path and the gateway path must match it (for tracebacks only the last line is compared).
- **Unit tests**: [tests/test_agent_lab_gateway.py](../tests/test_agent_lab_gateway.py) has 11 items in total, covering: not running, variable set but file missing, and stale file, all three falling back to the original path; the gateway re-checking arguments, the token, `init` and fingerprint mismatches; two concurrent calls using two Sessions, which are then reused; rejection when busy; when disconnected after the request is sent, only reporting outcome unknown and sending only once; closing Sessions, exiting and deleting the file after idling; not taking over an existing rendezvous file; and each of the limits. Three mutation checks were also done (changing output indentation, removing the gateway-side checks, re-sending the request after an error), and the tests caught all of them.
- **Full suite**: `.\.venv-cloud\Scripts\python.exe -m unittest discover -s tests` passed 64 items in total; the scoring tests of `experiments/gpt_repeat` passed.
- **Windows RST**: at first the 401 reply was sent before reading the request body, and in 3 of 15 consecutive test rounds the client received `ConnectionAbortedError`, because on Windows closing a socket that still has unread data sends an RST, so the reply was lost. After changing to read the request body first, 25 consecutive rounds had 0 failures. `agent_state/server.py` likewise replies 401 before reading the request body; it was not modified this time.

## Comparison on the real local PostgreSQL

`python compare_agent_lab_gateway.py` runs the same sequence of 20 calls (init, fork, propose, diff, read-code, propose with a stale expected, merge success/40001/whole-row conflict, verify failure and success, invalid branch, argument error) against two newly created test databases: one spawning a process per call, one through the gateway. The gateway is started with `--idle-timeout 10` and exits on its own at the end. The [results](../agent-lab-gateway-results.json) record the source file hashes, and whether the source files were changed during the run.

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
