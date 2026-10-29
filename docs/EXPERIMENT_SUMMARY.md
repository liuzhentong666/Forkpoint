中文 | [English](#english-version)

# 我们测试了什么，为什么没有成功

2026-10-29 写给想了解本项目做了什么、结果如何的读者，不需要数据库或 AI 背景。详细记录的链接在文末。

## 一句话总结

我们做了 Forkpoint：一个加在 PostgreSQL 数据库上的工具，目的是在多个 AI Agent 处理同一份数据时防止它们出错。两周里我们用真实的 AI Agent 做了五组测试。工具每次都按设计正常工作，但 Agent 从来没有犯它要防止的那些错误；在唯一一组与普通数据库对比的测试中，得到良好指示的 Agent 在普通数据库上做得一样好，步骤还更少。所以我们暂停了新功能。

## 下文用到的几个词

- **Agent：** 一个自己一步步完成任务的 AI 模型（这里主要是 Claude Opus 5.5），通过调用工具来读取和修改数据库等。
- **数据库：** 程序保存数据的地方，例如商店的商品和库存。PostgreSQL 是一种广泛使用的数据库。
- **分支与合入：** 借用自 Git（程序员共享代码的工具）。Agent 先在一份私有副本（分支）上修改，只有合入时，修改才进入共享数据（称为 main）。如果会与别人的修改冲突，合入可以被拒绝。
- **陈旧：** 过时。陈旧的决策是指依据的数字已经被别人改掉的决策。
- **上下文：** Agent 当前对任务记得的全部内容。它有大小上限；装满后较早的部分会被丢掉或压缩，Agent 就可能忘事。
- **轮：** 一次完整的测试，从一个全新的数据库开始，到任务完成为止。

## 我们怎样测试

每次都遵守同样的规则，让结果无法被我们的期望左右：

1. 运行之前先写下问题、预期，以及由什么规则判定答案。这些内容先提交到仓库，之后不再修改。
2. 使用真实的 AI Agent，而不是假装成 Agent 的脚本（只有一组检查机制本身的测试例外）。
3. 事先固定轮数，保留每一轮，包括失败的轮次。
4. 每次运行后直接检查数据库，而不是相信 Agent 自己说做了什么。
5. 一切都在一台电脑上的一次性测试库中运行。

## 五组测试

### 1. 两个 Agent 修改同一件商品（10 月 17 日与 20 日）

**担心的问题：** 两个 Agent 同时修改同一件商品，其中一个悄悄覆盖了另一个的工作。

**做法：** 一个 Agent 提高某件商品的价格，另一个减少它的库存，各自在自己的分支上进行。用 GPT 模型跑了 10 轮，用 Claude 跑了 10 轮。

**结果：** 没有任何一轮出现覆盖。Claude 完成了全部 10 轮；GPT 完成了 8 轮，另外两轮被服务返回的用量上限错误中断。

**说明了什么：** Forkpoint 正常工作。但没有不用 Forkpoint 的对照组，而且指示里告诉了 Agent 合入被拒后该怎么做。所以这组测试说明工具能用，没有说明 Agent 需要它。

### 2. 用脚本检查 Agent 读过的数据（10 月 21 日）

**担心的问题：** Agent 根据读到的某个数字做决定，别人随后改了这个数字，而这个决定仍然被合入。

**做的功能：** 一种合入检查。Agent 列出决策所依据的行；只要其中任何一行在此期间变了，合入就被拒绝。

**做法：** 用简单的脚本而不是 AI 扮演 Agent，这样能在我们选定的情形下准确看到检查的效果：75 次运行，再完整重复一次。

**结果：** 脚本列出读过的全部行时，检查拦下了每一次错误的合入。只列出一半时，大约一半错误的合入漏了过去（70 次中 35 次）。

**说明了什么：** 检查有效，但效果完全取决于给它的清单是否完整。真实的 Agent 是否需要它，留给第 5 组测试回答。

### 3. 完整的记忆存档（10 月 23 日）

**担心的问题：** Agent 的上下文装满并被压缩后，它会忘记需要的细节，于是重复工作或出错。

**做的功能：** 一个存档，逐字保存 Agent 看到过的一切，记忆被清空后可以准确地查回任何内容。

**做法：** 一个需要用到前面 14 个数字的任务。做到一半时，清空 Agent 的记忆。一组（10 轮）有存档可查；另一组（10 轮）只是在清空前被要求给自己写一份笔记。

**结果：** 两组都没有重复工作，也没有失败的步骤。Agent 自己写的笔记（约 2–3 KB）保留了全部 14 个数字。有存档的一组之后读取的内容多了约一倍，成功率还略低（10 轮中 8 轮，对比 9 轮）：一个有存档的 Agent 从未查阅存档，结果中漏掉了那 14 个数字；另有两轮（每组一轮）因我们的用量上限中断。我们还检查了自己工作中真实发生的三次记忆压缩：都没有造成信息丢失。

**说明了什么：** Agent 自己的笔记已经够用。存档增加了成本，没有带来好处。

### 4. 提醒 Agent 它的笔记已经过时（10 月 24 日）

**担心的问题：** 记忆被清空后，Agent 仍然相信旧笔记，即使笔记依据的数据已经变了。

**做的功能：** 新鲜度标记。Agent 重新拿起笔记时，每条事实都被标为新鲜、过期或未知，取决于它依据的数据是否变了。

**做法：** 先做一轮有标记、一轮没有标记的试运行。任务进行到一半时，我们改掉了 Agent 决策所依赖的一个数字。

**结果：** 两个 Agent 都得到了正确答案。即使没有标记，Agent 也早已计划在行动前重新读取这个数字。标记没有可纠正的内容，反而让每次交接的内容变大。

**说明了什么：** 试运行之后就停止了，没有采用这些标记。

### 5. Forkpoint 与普通数据库的对比（10 月 28 日与 29 日）

这是决定性的一组测试，因为它比较了用与不用 Forkpoint。

**任务：** 商店把商品 101 和 202 作为套装出售，两者库存合计必须始终不低于 180。两者开始时都是 100，合计 200。两个 Agent 同时工作：一个出售 15 件商品 101，另一个出售 15 件商品 202。任一笔单独出售都没问题（200 → 185），两笔都出售就违反规则（170）。所以后出手的 Agent 必须注意到前一笔销售，并拒绝出售。

**两组：**
- 普通数据库：Agent 直接读取和修改数据，指示中要求它锁住所依据的行，并在修改的同一步里再次检查规则。
- Forkpoint：Agent 修改分支再合入，并列出所依据的行，让合入检查能拒绝陈旧的销售。

**让冲突每次都发生：** 测试程序先扣住每个 Agent 的第一次修改，直到另一个 Agent 也准备写入，再放行，使第二个 Agent 在第一个的修改还在保存时运行。这对第二个 Agent 是最难的时刻。

**结果（每组 10 轮）：**

| | 普通数据库 | Forkpoint |
|---|---|---|
| 两笔都卖出的轮次（错误结果） | 10 轮中 0 轮 | 10 轮中 0 轮 |
| 每轮步骤（中位数） | 4 | 12 |
| 每轮耗时（中位数） | 28 秒 | 43 秒 |
| 总费用 | 0.84 美元 | 0.87 美元 |

在普通数据库上，每个后出手的 Agent 都等前一笔销售完成，看到它，然后没有出售。在 Forkpoint 上，每个后出手的 Agent 的合入都被拒绝，于是在新分支上重来，看到前一笔销售，然后没有出售。两组都次次正确，但 Forkpoint 的步骤是普通数据库的三倍。

**说明了什么：** 按事先写下的规则，Forkpoint 没有显示出安全上的优势。在普通数据库的指示里加一句话，就做到了同样的事。

## 为什么没有成功

每个功能都是为了拦住我们预计 Agent 会犯的某个错误：互相覆盖、记忆清空后忘事、相信过时的笔记、依据已经变了的数字行事。在我们的测试里，Agent 没有犯这些错误。它写出了好笔记，使用数字前会再核对一遍，也照着关于加锁的指示去做。安全网只有在有人掉下来时才值得它的成本，而这里没有人掉下来。

引擎本身每次都正确工作。失败的是“Agent 需要它”这个想法：我们找不到它们需要它的情形。

## 我们没有测试的情形

这些结果来自小任务，并且主要来自一个能力很强的模型（Claude Opus 5.5，高推理强度设置），因此不涵盖：

- 没有被告知如何保护自己修改的 Agent；
- 更弱或更便宜的模型；
- 同时运行很多 Agent，或者在通过搜索（而不是按准确的商品编号）找到的数据上发生冲突；
- 需要很多步、无法一次完成修改的长任务。这是分支能做到而普通数据库做不到的一件事。

## 接下来

新功能已经暂停。只有在一项事先写下的测试先显示 Agent 在普通数据库上出错之后，才会添加新功能。没有删除任何东西：引擎、它的测试和每一份实验记录都留在仓库中。

## 详细记录

- [两个 Agent 修改同一件商品](GPT_REPEAT_EXPERIMENT.md)
- [用脚本检查 Agent 读过的数据](READSET_EXPERIMENT.md)
- [完整的记忆存档](CONTEXT_ARCHIVE_EXPERIMENT.md)
- [新鲜度标记](TASK_STATE_EXPERIMENT.md)
- [Forkpoint 与普通数据库的对比](READSET_AGENTS_EXPERIMENT.md)

---

<a id="english-version"></a>

# What We Tested, and Why It Didn't Work Out

Written on 2026-10-29 for anyone who wants to know what this project tried and what came of it. No database or AI background is needed. Links to the detailed records are at the end.

## The short version

We built Forkpoint, an add-on to the PostgreSQL database meant to stop AI agents from making mistakes when several of them work on the same data. Over two weeks we ran five sets of tests with real AI agents. The add-on always worked as designed, but the agents never made the mistakes it was built to prevent; in the one test that compared it with an ordinary database, agents with good instructions did just as well there, in fewer steps. So we have paused new features.

## A few words used below

- **Agent:** an AI model (here mostly Claude Opus 5.5) that does a task by itself, step by step, by calling tools, for example to read and change a database.
- **Database:** where a program keeps its data, such as a shop's products and stock. PostgreSQL is a widely used one.
- **Branch and merge:** borrowed from Git, the tool programmers use to share code. An agent makes its changes on a private copy (a branch), and they reach the shared data (called main) only when it merges them back. A merge can be refused if it would clash with someone else's change.
- **Stale:** out of date. A stale decision is one based on numbers that someone else changed in the meantime.
- **Context:** everything an agent currently remembers about its task. It has a size limit; when it fills up, older parts are dropped or condensed, and the agent can forget things.
- **Round:** one complete run of a test, from a fresh database to a finished task.

## How we tested

We followed the same rules every time, so that the results could not be bent towards what we hoped:

1. Before running anything, we wrote down the question, what we expected, and the rule that would decide the answer. This was committed to the repository first and not changed afterwards.
2. We used real AI agents, not scripts pretending to be agents (except in one test that checked the mechanism itself).
3. We fixed the number of rounds in advance and kept every round, failed ones included.
4. After each run we checked the database directly, instead of trusting what the agents said they had done.
5. Everything ran on throwaway test databases on one computer.

## The five tests

### 1. Two agents editing the same product (17 and 20 October)

**The worry:** two agents change the same product at the same time, and one silently overwrites the other's work.

**What we did:** one agent raised a product's price while another cut its stock, each on its own branch. We ran 10 rounds with a GPT model and 10 with Claude.

**What happened:** no agent overwrote the other's work in any round. Claude finished all 10 rounds; GPT finished 8, and two were cut short by usage-limit errors from the service.

**What it means:** Forkpoint worked. But no group worked without Forkpoint, and the instructions told the agents what to do when a merge was refused. So this showed that the tool works, not that agents need it.

### 2. Checking what an agent read, with scripts (21 October)

**The worry:** an agent decides something from a number it read, someone else then changes that number, and the decision goes through anyway.

**What we built:** a merge check. The agent lists the rows its decision was based on, and the merge is refused if any of them changed in the meantime.

**What we did:** simple scripts, not AI, played the agents, so we could see the check work in exactly the situations we chose: 75 runs, repeated once in full.

**What happened:** when the scripts listed everything they had read, the check stopped every bad merge. When they left out half, about half of the bad merges got through (35 of 70).

**What it means:** the check works, and it is only as good as the list it is given. Whether real agents need it was left for test 5.

### 3. A complete memory archive (23 October)

**The worry:** when an agent's context fills up and is cut down, it forgets details it needs, so it repeats work or makes mistakes.

**What we built:** an archive that keeps every word the agent saw, so that after a memory wipe it can look anything up exactly.

**What we did:** a task that needed 14 numbers from earlier in the work. Partway through, the agent's memory was wiped. One group (10 rounds) had the archive; the other (10 rounds) was simply asked to write itself a note before the wipe.

**What happened:** neither group repeated work or had a failed step. The agents' own notes, about 2–3 KB, kept all 14 numbers. The archive group read about twice as much afterwards and succeeded slightly less often (8 of 10 rounds against 9): one archive agent never looked anything up and left the 14 numbers out of its result, and two rounds, one in each group, were stopped by our usage limit. We also checked three real memory cuts from our own working sessions: none caused lost information.

**What it means:** the agent's own note was enough. The archive added cost and no benefit.

### 4. Warning an agent that its notes are out of date (24 October)

**The worry:** after a memory wipe, an agent trusts its old notes even though the data behind them has changed.

**What we built:** freshness marks. When the agent picks its notes up again, each fact is labelled fresh, stale or unknown, depending on whether the data it came from has changed.

**What we did:** a trial round with the marks and one without. Partway through the task, we changed a number the agent's decision depended on.

**What happened:** both agents got the right answer. Even without the marks, the agent had already planned to read the number again before acting. The marks had nothing to correct, and they made every handover larger.

**What it means:** we stopped after the trial and did not adopt the marks.

### 5. Forkpoint against an ordinary database (28 and 29 October)

This was the deciding test, because it compared Forkpoint with going without it.

**The task:** a shop sells products 101 and 202 as a bundle, and together they must always have at least 180 in stock. Both start at 100, so 200 in total. Two agents work at the same time: one sells 15 of product 101, the other 15 of product 202. Either sale alone is fine (200 → 185); both together break the rule (170). So whichever agent goes second must notice the first sale and refuse to sell.

**The two groups:**
- Ordinary database: agents read and changed the data directly, and their instructions told them to lock the rows they relied on and check the rule again in the same step as the change.
- Forkpoint: agents changed a branch and merged it, listing the rows they relied on so that the merge check could refuse a stale sale.

**Making the clash happen every time:** the test program held back each agent's first change until the other agent was also ready to write, then let them go so that the second ran while the first was still being saved. That is the hardest moment for the second agent.

**What happened (10 rounds per group):**

| | Ordinary database | Forkpoint |
|---|---|---|
| Rounds where both sales went through (the bad outcome) | 0 of 10 | 0 of 10 |
| Steps per round (median) | 4 | 12 |
| Time per round (median) | 28 s | 43 s |
| Total cost | $0.84 | $0.87 |

On the ordinary database, every second agent waited for the first sale to finish, saw it, and didn't sell. On Forkpoint, every second agent's merge was refused, so it started again on a fresh branch, saw the first sale, and didn't sell. Both groups were right every time, but Forkpoint took three times as many steps.

**What it means:** by the rule we wrote down beforehand, Forkpoint showed no safety advantage. One sentence in the instructions for an ordinary database did the same job.

## Why it didn't work out

Each feature was built to catch a mistake we expected agents to make: overwriting each other, forgetting after a memory wipe, trusting stale notes, acting on numbers that had changed. In our tests the agents didn't make those mistakes. They wrote good notes, checked numbers again before using them, and followed instructions about locking. A safety net is only worth its cost if someone falls, and nobody fell.

The engine itself worked correctly every time. What failed was the idea that agents need it: we could not find a situation where they did.

## What we did not test

These results come from small tasks and mostly one strong model (Claude Opus 5.5 at its high-effort setting), so they do not cover:

- agents that are not told how to protect their changes;
- weaker or cheaper models;
- many agents at once, or clashes over data found by searching rather than by exact product number;
- long jobs where an agent's change takes many steps and cannot be made in one go. This is the one thing a branch offers that an ordinary database does not.

## What happens now

New features are paused. One will be added only after a test, written down in advance, first shows agents failing on an ordinary database. Nothing has been deleted: the engine, its tests and every experiment record stay in the repository.

## The detailed records

- [Two agents editing the same product](GPT_REPEAT_EXPERIMENT.md)
- [Checking what an agent read, with scripts](READSET_EXPERIMENT.md)
- [A complete memory archive](CONTEXT_ARCHIVE_EXPERIMENT.md)
- [Freshness marks](TASK_STATE_EXPERIMENT.md)
- [Forkpoint against an ordinary database](READSET_AGENTS_EXPERIMENT.md)
