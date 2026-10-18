English | [中文](LOCAL_REVIEW_PLAN.md)

# Plan for Local Maintenance and Agent Experiments

2026-10-17. The user asked for five local checks, and explicitly switched the fifth to GPT Agents. Cloud storage is still not verified against real cloud services; no cloud storage is purchased or connected, and Claude is not run.

1. Use event barriers to reproduce the sequence in which A stalls, B completes, and A wakes up and publishes; the stale executor must receive a Conflict and must not treat someone else's completion result as its own successful publish. Keep the network retry semantics of identical commands; do not claim to provide cross-system fencing.
2. Add an interruption point after the intent has been published but before the database action has happened; the new executor verifies the database and then executes once, and this is compared with the existing interrupted-after-commit scenario.
3. Use a non-default prefix through the JSON configuration, the storage factory, the maintenance CLI and the service startup wiring; check that they read and write the same namespace, and that the task does not exist under the default prefix. Use SDK/in-memory doubles, without accessing a real bucket.
4. Add an `agentdb_manager` role that is only allowed to check and materialize snapshots, and a status function with a fixed search_path. Verify the allowed/denied operations in a dedicated test database on the existing WSL PostgreSQL; creating a new test database in the same service is not the same as a temporary PostgreSQL instance. Add an end-to-end experiment in which ten branches are all merged, with timing that covers COMMIT, recording stale-head retries and the final data/code integrity.
5. Use two real GPT/Codex sub-Agents operating on another dedicated test database: from the same baseline, modify different fields of the same product and the corresponding code, and observe the whole-row conflict, re-reading and re-proposing. The sub-Agents are not given deployment or business-database write tasks. Distinguish model behavior from scripted simulation using the database results and the real tool records.

The restricted role is for routine maintenance and does not replace the administrator who can change system rules. `SET ROLE` is the way database permissions are checked in local testing; it does not establish security isolation for an execution environment that holds the original administrator connection; in a real deployment, a restricted login identity should take on this role.

The fixes and new features in this round do not change the persistent object format. Old installations must first apply the new SQL role/function migration before using the new maintenance executor; rolling back the executor does not require rewriting task history. The behavior assertions of the existing tests are kept. The final results are written up as a separate local record and are not changed into a cloud success conclusion.
