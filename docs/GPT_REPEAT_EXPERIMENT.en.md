English | [中文](GPT_REPEAT_EXPERIMENT.md)

# Ten-Round Real GPT Experiment: Results and Raw Call Records

2026-10-17. Separate test databases on the existing WSL PostgreSQL; Claude was not run, and no real cloud storage was accessed.

This group pre-registered **10 rounds**, recorded **10 rounds**, with **8/10 (80%)** successful, and **8** rounds in which all model phases completed.
The success rate here uses all pre-registered rounds as the denominator, including model service interruptions. The corresponding 95% Wilson interval is **49.0%–94.3%**, given only as a small-sample description under a binomial model; the shared account and service failures may be correlated, so it cannot be used to estimate the model's intrinsic ability or production reliability.

The model is **`gpt-6-astra`**, as explicitly returned by the running interface, with reasoning effort **`xhigh`**; the runner is `codex-cli 0.163.0-alpha.10.2`. Each Agent's return values and model reassignment events are kept in the raw logs and results. This is the exact model ID reported by the interface; the provider does not expose the underlying weight snapshot. The original single-round experiment did not record the exact model, and is not counted in this sample.

Observed overwrites of published fields/code: **0/10 rounds**; modifications of the other Agent's branch: **0/10 rounds**. The check covers the database state after every write operation actually executed, including failed requests; even if later repaired, an earlier overwrite still counts as a failure. Rounds stopped midway only show that no overwrite happened in the part that was executed; they do not prove that no overwrite would have happened had they continued.

## Results per round

| Round | Result | Tool calls | Input / of which cached | Output / of which reasoning | Total tokens | Published overwrites |
|---|---|---:|---:|---:|---:|---:|
| [01](../experiments/gpt_repeat/results/2026-10-17/run-01/result.json) | Passed | 32 | 316,723 / 263,680 | 2,564 / 236 | 319,287 | 0 |
| [02](../experiments/gpt_repeat/results/2026-10-17/run-02/result.json) | Passed | 31 | 315,721 / 279,808 | 2,692 / 176 | 318,413 | 0 |
| [03](../experiments/gpt_repeat/results/2026-10-17/run-03/result.json) | Run error | 7 | 56,137 / 43,136 | 854 / 318 | 56,991 (partial) | 0 |
| [04](../experiments/gpt_repeat/results/2026-10-17/run-04/result.json) | Run error | 1 | 10,257 / 0 | 81 / 0 | 10,338 (partial) | 0 |
| [05](../experiments/gpt_repeat/results/2026-10-17/run-05/result.json) | Passed | 32 | 307,406 / 268,544 | 2,704 / 210 | 310,110 | 0 |
| [06](../experiments/gpt_repeat/results/2026-10-17/run-06/result.json) | Passed | 31 | 303,866 / 276,480 | 2,649 / 200 | 306,515 | 0 |
| [07](../experiments/gpt_repeat/results/2026-10-17/run-07/result.json) | Passed | 33 | 323,645 / 273,920 | 2,560 / 67 | 326,205 | 0 |
| [08](../experiments/gpt_repeat/results/2026-10-17/run-08/result.json) | Passed | 31 | 318,497 / 290,944 | 2,718 / 280 | 321,215 | 0 |
| [09](../experiments/gpt_repeat/results/2026-10-17/run-09/result.json) | Passed | 32 | 319,639 / 283,008 | 2,891 / 241 | 322,530 | 0 |
| [10](../experiments/gpt_repeat/results/2026-10-17/run-10/result.json) | Passed | 31 | 316,749 / 267,904 | 2,766 / 226 | 319,515 | 0 |

In total **261 tool calls**; rounds in which both Agents have usage records: **8/10**. Also counting the single-Agent usage already returned in the interrupted rounds, **2,611,119 tokens** were observed, of which input 2,588,640 and output 22,479; cached input 2,247,424 and reasoning output 1,954 are sub-items of these respectively and are not added again.

"Partial" means that round obtained the cumulative usage of only some threads; see the raw notifications and per-Agent records for details; usage that was not returned remains unknown. The original totals in summary.json include only rounds where both threads have data, while behavior-analysis.json separately totals all reported usage; the two use different counting bases.

Usage comes from each thread's last cumulative notification, then summed over threads, without repeatedly summing the per-event cumulative values. It includes context re-sent across multiple requests and cannot be interpreted as having read an equal amount of independent database content. Missing values are not estimated from character counts, and these counts are not treated as a billing reconciliation; usage from requests that disconnected or that the server did not return may not be obtainable on the client. Startup/reconnection errors are likewise kept in the raw streams. Timing includes local tools, side-channel verification and network reconnections, and is not used to rank model speed.

## Behavioral differences and run failures

There were 8 rounds that actually completed all three phases, and all 8 achieved the goal; the incomplete rounds stay in the denominator of the main success rate. Among the completed rounds, all 8 encountered stale_head / 40001 and then merge_conflict / P0001 in order. This order was explicitly arranged by the prompts and cannot be taken as evidence that the Agents discovered the recovery strategy on their own without prompting.

Completed rounds made 31–33 tool calls each, mean 31.6, median 31.5. Per-action and per-phase counts are in behavior-analysis.json; they can distinguish extra verification from retry costs, and model behavior should not be summarized by the single number of final success alone.

In this group the differences appeared mainly in reading and verification: info 5–7 times per round, read-code 9–10 times, diff 5–6 times. The write steps of the eight completed rounds were the same: 3 branch creations, 3 joint proposals, 4 merge requests, of which 2 merges were rejected by the expected conflict checks. Consistent results do not mean the call paths or resource consumption were the same.

Rounds that received usageLimitExceeded: 3, 4; rounds with other database tool errors: none. Quota errors are returned by the running service and cannot be directly attributed to the database or to model reasoning.

After rounds 3 and 4 were interrupted, a separate read-only quota query returned ordinaryUsageAllowed=true and showed that the general Codex window still had remaining quota; later rounds continued to get real tool calls. This is inconsistent with the quota errors of the failed turns; the specific cause has not been confirmed, and it cannot be stated that the whole account's quota was definitely exhausted. The raw messages of the query are saved separately in diagnostics/quota-status; login, model and account settings were not changed.

## Experimental conditions and criteria

Each round has two brand-new GPT contexts and a separate 4,000-row test database. The stock Agent first prepares a proposal for stock 100→95 from the common baseline; the price Agent then prepares and merges a proposal for price 10→11 from the same baseline; finally the stock Agent completes its merge based on the error and the current state. Each task also modifies the corresponding Python policy file. At most two rounds run in parallel, and operations within one database proceed in phases.

The three-part prompts, tool definitions, model and effort were kept the same across the ten rounds, all written in advance into protocol.json; there was no manual rescue based on any round's results and no replacement of failed samples. The prompts explicitly arrange to first check the conflict with the old expected, and hint to keep other work and re-propose when necessary, so this is still a controlled experiment with recovery hints. Compared with the original single round, this time the same agent_lab.py capabilities are wrapped as structured tools, read-code is available from the start, and no quota interruption was deliberately injected.

The two Agents have the same database capabilities: both can request to modify price or stock; the recorder does not force corrections of wrong fields. Success requires both Agents to complete their own merges, a final price of 11 and stock of 95, both code constants correct, the other 3,999 rows and the baseline unchanged, no unfinished transactions, and that throughout the process neither overwrote the other's published content or modified the other's branch. Code is only checked for syntax and constants, not executed.

The overwrite detector first passed a counter-example test of "overwrite and then restore". The independent SQL verification after each write request is saved in database_audit.jsonl; an offline audit recomputed the overwrite events, checked the snapshots of all 82 actual write requests, and confirmed that the tool texts and counts are consistent. This observation scope covers commits made through the experiment tools, and does not claim to verify arbitrary SQL, DDL, external programs or cloud storage.

## Raw evidence and re-analysis

- [Pre-registered protocol, prompts, source file hashes](../experiments/gpt_repeat/results/2026-10-17/protocol.json)
- [Machine-readable summary](../experiments/gpt_repeat/results/2026-10-17/summary.json)
- [Per-action differences and partial usage analysis](../experiments/gpt_repeat/results/2026-10-17/behavior-analysis.json)
- [Raw record consistency check](../experiments/gpt_repeat/results/2026-10-17/log-audit.json)
- [File SHA-256 manifest](../experiments/gpt_repeat/results/2026-10-17/SHA256SUMS.json)

Each round's directory contains the following files:

| File | Content |
|---|---|
| raw/server.jsonl | Raw JSONL bytes of the app-server's standard output, saved first and parsed afterwards |
| raw/client.jsonl | Bytes of the requests and tool responses actually sent to the app-server |
| raw/tool_calls.jsonl | Tool requests and responses extracted verbatim, linked by callId / RPC id |
| raw/transport_timing.jsonl | Receive times recorded separately, without rewriting the raw messages |
| raw/server.stderr.log | Runner errors and reconnection records |
| database_audit.jsonl | Independent database state after each write operation |
| result.json | Recomputable summary, model identifier, usage and results |

Call arguments and returned content in the JSONL have not been replaced by summaries or truncated; an offline check confirmed that the tool texts come line by line from the server/client raw streams. The preceding interface probes and one echo pre-check are saved separately in diagnostics/preflight and diagnostics/instrumentation-pilot, and are not counted in these ten rounds' call counts and usage. The locally generated protocol schema is saved separately as protocol-schema.zip. The raw files are kept as local experimental material, and .gitignore prevents accidentally committing account run metadata; publishing the source code does not mean publishing these logs.

To re-check the existing records from the project directory, without calling models or writing to the database:

```console
python experiments/gpt_repeat/audit_repeat_logs.py experiments/gpt_repeat/results/2026-10-17
```

Running a new group of experiments needs a new output directory, uses the model quota of the existing Codex login, and creates new dedicated test databases:

```console
.\.venv-cloud\Scripts\python.exe experiments/gpt_repeat/run_gpt_repeats.py --project . --output experiments/gpt_repeat/results/NEW_RUN --runs 10 --workers 2 --model gpt-6-astra --effort xhigh
```

The recording entry point is based on the [OpenAI App Server documentation](https://learn.chatgpt.com/docs/app-server) and the schema generated from the local version. **Cloud storage is still not verified against real cloud services, and cloud deployment is on hold.**

## Run errors in this group

- Round 3: `RuntimeError`; for the full error text see [result.json](../experiments/gpt_repeat/results/2026-10-17/run-03/result.json) and that round's raw logs.
- Round 4: `RuntimeError`; for the full error text see [result.json](../experiments/gpt_repeat/results/2026-10-17/run-04/result.json) and that round's raw logs.
