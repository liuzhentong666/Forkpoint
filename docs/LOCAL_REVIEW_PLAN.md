中文 | [English](#english-version)

# 本机维护与 Agent 实验计划

2026-10-17。用户要求五项本机检查，第五项明确改用 GPT Agent。云存储仍未经真实验证，不采购或连接云存储，不运行 Claude。

1. 用事件屏障重现 A 停顿、B 完成、A 醒来发布的顺序；旧执行者须收到 Conflict，不能把别人的完成结果当成自己的成功发布。保留相同命令的网络重试语义；不声称提供跨系统 fencing。
2. 增加意图已发布、数据库动作尚未发生的中断点；新执行者核验数据库后执行一次，再与已有提交后中断场景对照。
3. 经 JSON 配置、存储工厂、维护 CLI 与服务启动装配使用非默认 prefix；检查它们读写同一命名空间，默认前缀中不存在该任务。用 SDK/内存替身，不访问真实桶。
4. 添加只允许检查和物化快照的 `agentdb_manager` 角色及固定 search_path 的状态函数。在现有 WSL PostgreSQL 的专用测试库验证允许/拒绝操作；同一服务内新建测试数据库不等于临时 PostgreSQL 实例。新增十个分支全部合入的端到端实验，计时覆盖 COMMIT，记录陈旧 head 重试和最终数据/代码完整性。
5. 使用两个真实 GPT/Codex 子 Agent 操作另一个专用测试库：从相同基线修改同一产品的不同字段和对应代码，观察整行冲突、重读及重新提案。子 Agent 不获得部署或业务库写入任务。通过数据库结果与真实工具记录区分模型行为和脚本模拟。

受限角色用于常规维护，不替代能修改系统规则的管理员。`SET ROLE` 是本机测试的数据库权限检查方式，不是对持有原管理员连接的执行环境建立安全隔离；真正部署时应由受限登录身份承接该角色。

本次修复和新功能不调整持久对象格式。旧安装需先应用新的 SQL 角色/函数迁移，再使用新的维护执行器；回退执行器不需要回写任务历史。保留已有测试的行为断言。最终结果另写本机记录，不修改为云端成功结论。

---

<a id="english-version"></a>

# Plan for Local Maintenance and Agent Experiments

2026-10-17. The user asked for five local checks, and explicitly switched the fifth to GPT Agents. Cloud storage is still not verified against real cloud services; no cloud storage is purchased or connected, and Claude is not run.

1. Use event barriers to reproduce the sequence in which A stalls, B completes, and A wakes up and publishes; the stale executor must receive a Conflict and must not treat someone else's completion result as its own successful publish. Keep the network retry semantics of identical commands; do not claim to provide cross-system fencing.
2. Add an interruption point after the intent has been published but before the database action has happened; the new executor verifies the database and then executes once, and this is compared with the existing interrupted-after-commit scenario.
3. Use a non-default prefix through the JSON configuration, the storage factory, the maintenance CLI and the service startup wiring; check that they read and write the same namespace, and that the task does not exist under the default prefix. Use SDK/in-memory doubles, without accessing a real bucket.
4. Add an `agentdb_manager` role that is only allowed to check and materialize snapshots, and a status function with a fixed search_path. Verify the allowed/denied operations in a dedicated test database on the existing WSL PostgreSQL; creating a new test database in the same service is not the same as a temporary PostgreSQL instance. Add an end-to-end experiment in which ten branches are all merged, with timing that covers COMMIT, recording stale-head retries and the final data/code integrity.
5. Use two real GPT/Codex sub-Agents operating on another dedicated test database: from the same baseline, modify different fields of the same product and the corresponding code, and observe the whole-row conflict, re-reading and re-proposing. The sub-Agents are not given deployment or business-database write tasks. Distinguish model behavior from scripted simulation using the database results and the real tool records.

The restricted role is for routine maintenance and does not replace the administrator who can change system rules. `SET ROLE` is the way database permissions are checked in local testing; it does not establish security isolation for an execution environment that holds the original administrator connection; in a real deployment, a restricted login identity should take on this role.

The fixes and new features in this round do not change the persistent object format. Old installations must first apply the new SQL role/function migration before using the new maintenance executor; rolling back the executor does not require rewriting task history. The behavior assertions of the existing tests are kept. The final results are written up as a separate local record and are not changed into a cloud success conclusion.
