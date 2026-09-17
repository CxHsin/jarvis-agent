# Agent 记忆系统：开源项目横向调研（OSS 部分）

研究日期：2026-09-12。只读各项目的 README、官方文档与源码，未运行这些项目、未调用其云服务、未在其 issue 区提问。以下是列出的固定版本的行为，不保证其他版本相同。本文件只覆盖开源项目；论文与商业产品由同批调研的另外两支分别覆盖，因此这里不重复它们的结论。

固定版本：[mem0ai/mem0@c7ee362](https://github.com/mem0ai/mem0/tree/c7ee362aff94a369af70f13f2b4f853f6793ff4c)、[topoteretes/cognee@c0d18c8](https://github.com/topoteretes/cognee/tree/c0d18c80e24b7b78918e7642c03f6f128fdd2aee)、[supermemoryai/supermemory@2415a5c](https://github.com/supermemoryai/supermemory/tree/2415a5c796d62c7ea9d709bc9337a6e1b6f6d837)、[MemTensor/MemOS@de80694](https://github.com/MemTensor/MemOS/tree/de8069428a9247bfa7a3d35f59a9b39fa8f231d2)、[NevaMind-AI/memU@08e1ed4](https://github.com/NevaMind-AI/memU/tree/08e1ed4cdf4c0cb1fe5387e4a532ea588a8cbe46)、[plastic-labs/honcho@8e38618](https://github.com/plastic-labs/honcho/tree/8e386180bd87b852e7934cfef53cba3d6bee1bb4)、[EverMind-AI/EverOS@5076683](https://github.com/EverMind-AI/EverOS/tree/5076683ab88d714390573d8f88ff3c470e51129a)、[MemoriLabs/Memori@10d6501](https://github.com/MemoriLabs/Memori/tree/10d65015007131a69b2597fa5130da58da24a0c2)、[memodb-io/Acontext@259d73b](https://github.com/memodb-io/Acontext/tree/259d73bfdebeed35ec2d4211ddc060a2d4126bc6)、[aiming-lab/SimpleMem@db80b6a](https://github.com/aiming-lab/SimpleMem/tree/db80b6a7c591e0ea730a058e9f5fc4eb06572299)、[memvid/memvid@e6bd9f7](https://github.com/memvid/memvid/tree/e6bd9f7b9c38cd8d5370fa0fc936ac1dcd751813)、[zilliztech/memsearch@f863056](https://github.com/zilliztech/memsearch/tree/f863056e0b113d44e860dd6abf5bb892781e29ca)、[kayba-ai/agentic-context-engine@31f4e11](https://github.com/kayba-ai/agentic-context-engine/tree/31f4e11897a93dd9ad90a3d0e2051de66b2255e0)、[agentscope-ai/ReMe@9ad3daf](https://github.com/agentscope-ai/ReMe/tree/9ad3dafce5666c55e8cd5b16cc5ffba42da6cce1)、[letta-ai/letta-code@9a40d27](https://github.com/letta-ai/letta-code/tree/9a40d271f8ff77a3f84e4544645c6226faab0dab)、[volcengine/OpenViking@84bbf79](https://github.com/volcengine/OpenViking/tree/84bbf79711f3d18000beb1d16708e69627743b0e)、[BAI-LAB/MemoryOS@587ed77](https://github.com/BAI-LAB/MemoryOS/tree/587ed7755c7aed179965792830ff1b5ad9a6fa92)、[memodb-io/memobase@358c16b](https://github.com/memodb-io/memobase/tree/358c16bbc6d687937d79bc2f984a11c3be8da901)、[TencentCloud/TencentDB-Agent-Memory@0468a2a](https://github.com/TencentCloud/TencentDB-Agent-Memory/tree/0468a2a5b50eaafc54758ed1e2e6609472e5b6ce)、[MemMachine/MemMachine@da7de4c](https://github.com/MemMachine/MemMachine/tree/da7de4cbb0e1f35eba5f9f9b16d97cf561175f5c)。

## 结论

**一、"记忆"在这个生态里至少是三个不同的目标，混着谈会直接走偏。**

| 目标 | 记忆的主体 | 代表项目 |
| --- | --- | --- |
| A 用户建模：对话里提炼出关于这个人的稳定事实与偏好 | 人 | Honcho、Supermemory、memobase、mem0、TencentDB 的 Chat Memory |
| B 经验沉淀：任务轨迹里提炼出下次可复用的做法 | agent | ACE、Acontext、memU、ReMe、MemOS、Letta Code |
| C 语料组织：文档与代码变成可检索的图或文件系统 | 知识源 | cognee、OpenViking、memsearch、EverOS |

你说的"通过与用户交流的内容进行提炼，使 Agent 更贴合用户习性"精确落在 **A**。但 2025 到 2026 年开源侧最密集的创新其实在 **B**——大量项目把"记忆"定义成技能或策略，而不是用户画像。选型时这一点必须先说清，否则会拿 B 类项目去解 A 类问题。

**二、蒸馏时机正在从"每轮在线"移向"任务结束或后台"。** Honcho 把推理放进后台队列，Acontext 在任务完成/失败时触发蒸馏，memsearch 在每轮 Stop hook 用小模型总结，EverOS 离线做 reflection 合并，Letta Code 用 `/sleeptime` 周期性 dreaming。留在每轮在线路径上的只有 mem0（单次 ADD 提取）和 memsearch 的总结调用，两者都把调用次数压到一次。

**三、检索侧已经收敛：混合检索是标配。** BM25（或 FTS5）+ 稠密向量 + RRF/加权融合 + 可选 rerank，出现在 mem0、memsearch、TencentDB、ReMe、SimpleMem 等大多数项目里。纯向量 top-k 的叙事在开源侧基本退场。

**四、更新策略没有共识，而且是当前最大的分歧点。** mem0 在 2026 年 4 月把算法改成 **纯 ADD、不 UPDATE 不 DELETE**，靠实体链接和时间消歧；Supermemory 走"提取事实 + 解决矛盾 + 自动遗忘过期信息"；OpenViking 让候选新记忆与既有记忆比较后 create/merge/skip；MemOS 和 cognee 提供显式 edit/forget API。同一个问题有四种互不兼容的答案。

**五、"人类可读、可 diff、可手改"成为一股明确潮流。** ReMe、memsearch、EverOS、Acontext、memU、OpenViking 都把 Markdown 或文件当 source of truth，把向量库当可重建的影子索引。这与 Jarvis 已有的"状态目录放在工作区之外、会话记录是事件日志"的取向相邻，但**出发点是可审计与可迁移，不是省依赖**。

**六、所有 LoCoMo / LongMemEval 分数都是自报，跨项目不可比。** 不同 harness、不同模型、不同检索预算下的数字放在一张表里没有意义。mem0 甚至在自己的 README 里注明：92.5 是托管平台含闭源优化的结果，开源 SDK 只能"方向性接近"。本文件因此只在个别处引用自报数字，且都标注了来源。

## 对比表

| 项目 | 定位 | 记忆单元 | 蒸馏时机 | 检索 | 关键依赖 | 许可 | ★ |
| --- | --- | --- | --- | --- | --- | --- | --- |
| [mem0](https://github.com/mem0ai/mem0/tree/c7ee362aff94a369af70f13f2b4f853f6793ff4c) | 通用记忆层 | 原子事实 + 实体链接 | 每轮，单次 ADD | 语义 + BM25 + 实体 | 向量库 + LLM | Apache-2.0 | 65.2k |
| [Supermemory](https://github.com/supermemoryai/supermemory/tree/2415a5c796d62c7ea9d709bc9337a6e1b6f6d837) | 通用记忆层 + 用户画像 | 事实 + 画像 | 在线提取 | 混合（RAG + Memory） | 服务/本地部署 | MIT | 29.6k |
| [Honcho](https://github.com/plastic-labs/honcho/tree/8e386180bd87b852e7934cfef53cba3d6bee1bb4) | 用户建模 | 每个 peer 的表征 | 后台异步 | 表征查询 + 语义 | Postgres | AGPL-3.0 | 7.1k |
| [MemOS](https://github.com/MemTensor/MemOS/tree/de8069428a9247bfa7a3d35f59a9b39fa8f231d2) | 记忆 OS | 图 + MemCube | 异步调度 | 混合 | Neo4j + Qdrant | Apache-2.0 | 11.3k |
| [memU](https://github.com/NevaMind-AI/memU/tree/08e1ed4cdf4c0cb1fe5387e4a532ea588a8cbe46) | 个人 Wiki + 技能 | Markdown 技能 | 用户/agent 显式提交 | 向量（名/描述） | SQLite 或 pgvector | Apache-2.0 | 14.4k |
| [EverOS](https://github.com/EverMind-AI/EverOS/tree/5076683ab88d714390573d8f88ff3c470e51129a) | local-first 运行时 | Markdown 页（用户轨 + agent 轨） | 离线 reflection | SQLite + LanceDB | 无外部服务 | Apache-2.0 | 12.9k |
| [memsearch](https://github.com/zilliztech/memsearch/tree/f863056e0b113d44e860dd6abf5bb892781e29ca) | 跨 agent 记忆 | 按天 Markdown + 分块 | 每轮 Stop hook | BM25 + 稠密 + RRF | Milvus | MIT | 2.6k |
| [ReMe](https://github.com/agentscope-ai/ReMe/tree/9ad3dafce5666c55e8cd5b16cc5ffba42da6cce1) | 本地个人知识库 | Markdown + wikilink | 演化式精炼 | BM25 + 可选向量 + wikilink | 无（可选向量） | Apache-2.0 | 3.4k |
| [Acontext](https://github.com/memodb-io/Acontext/tree/259d73bfdebeed35ec2d4211ddc060a2d4126bc6) | 技能记忆层 | Markdown 技能文件 | 任务结束/失败 | 工具渐进式披露（无检索） | 无向量库 | Apache-2.0 | 3.7k |
| [OpenViking](https://github.com/volcengine/OpenViking/tree/84bbf79711f3d18000beb1d16708e69627743b0e) | context 数据库 | 虚拟文件系统 + L0/L1/L2 | 会话提交后后台 | 目录向量 + 结构探索 | 服务部署 | AGPL-3.0 | 36.8k |
| [cognee](https://github.com/topoteretes/cognee/tree/c0d18c80e24b7b78918e7642c03f6f128fdd2aee) | 知识图谱记忆 | 实体关系 + chunk | 摄取/improve | 图 + 向量 + 代码 | 图库 + 向量库 | Apache-2.0 | 30.7k |
| [SimpleMem](https://github.com/aiming-lab/SimpleMem/tree/db80b6a7c591e0ea730a058e9f5fc4eb06572299) | 压缩优先的高效记忆 | 结构化压缩单元 | 写入时压缩合成 | 意图感知检索规划 | FAISS + BM25 | MIT | 3.8k |
| [TencentDB Agent Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory/tree/0468a2a5b50eaafc54758ed1e2e6609472e5b6ce) | 团队记忆中枢 | L0–L3 分层资产 | 异步管道 | BM25 + 向量 + RRF | 多服务部署 | README 标 MIT（GitHub 未识别） | 26.4k |
| [ACE](https://github.com/kayba-ai/agentic-context-engine/tree/31f4e11897a93dd9ad90a3d0e2051de66b2255e0) | 经验学习 | Skillbook 策略 | 每任务后 | 策略注入 | LLM 多角色 | Apache-2.0 | 2.6k |
| [Memori](https://github.com/MemoriLabs/Memori/tree/10d65015007131a69b2597fa5130da58da24a0c2) | 企业记忆基础设施 | 结构化持久状态 | 后台自动 | 混合 | SDK 注册进 LLM client | GitHub 未识别 | 16.7k |
| [memobase](https://github.com/memodb-io/memobase/tree/358c16bbc6d687937d79bc2f984a11c3be8da901) | 用户画像记忆 | profile + 事件时间线 | 按用户缓冲批处理 | SQL + 语义 | Postgres | Apache-2.0 | 2.9k |
| [memvid](https://github.com/memvid/memvid/tree/e6bd9f7b9c38cd8d5370fa0fc936ac1dcd751813) | 单文件记忆 | 不可变 Smart Frame | 追加写入 | 文件内检索 | Rust 库 | Apache-2.0 | 16.5k |
| [MemoryOS](https://github.com/BAI-LAB/MemoryOS/tree/587ed7755c7aed179965792830ff1b5ad9a6fa92) | 记忆 OS（论文实现） | 分层存储 | 在线更新 | 分层检索 | Python | Apache-2.0 | 1.6k |

## 逐项目证据

### 用户建模派

**Honcho 是本次调研中"用户建模"最纯粹的一个。** 它把人和 AI agent 都当一等实体（peer），消息挂在 session 上，然后**在后台**推理出每个 peer 的表征。查询面是四条：Conclusions（对某 peer 的演绎与归纳结论）、Representations（低延迟快照）、Peer Cards（身份摘要）、Session context（可直接进 prompt 的打包）。内部实现是"按 `(observer, observed)` 配对的向量文档集合"——自我表征是 `observer == observed` 的特例，跨 peer 建模共用同一套机制。[README · Why Honcho](https://github.com/plastic-labs/honcho/blob/8e386180bd87b852e7934cfef53cba3d6bee1bb4/README.md#why-honcho)、[README · Core Concepts](https://github.com/plastic-labs/honcho/blob/8e386180bd87b852e7934cfef53cba3d6bee1bb4/README.md#core-concepts)

**mem0 在 2026 年 4 月把算法改成了纯 ADD。** 这是本次调研里最反直觉的一处改动。README 的迁移说明写着"Single-pass ADD-only extraction -- one LLM call, no UPDATE/DELETE. Memories accumulate; nothing is overwritten."，同时补上实体链接、多信号检索和时间推理。[README · New Memory Algorithm](https://github.com/mem0ai/mem0/blob/c7ee362aff94a369af70f13f2b4f853f6793ff4c/README.md#new-memory-algorithm-april-2026)

这一点在源码里可以直接核对。提取提示词的第一句就是操作约束："Your sole operation is ADD: identify every piece of memorable information and produce self-contained, contextually rich factual statements."；它同时提取 user 与 assistant 两方的信息，但要求把 assistant 内容转写成"User was recommended X"这种以用户为主语的表述；去重靠两份参照——本次会话最近提取的 20 条与既有记忆；遇到相关的旧记忆时**不改写它，而是把旧记忆的 UUID 写进新记忆的 `linked_memory_ids`**。[mem0/configs/prompts.py:468](https://github.com/mem0ai/mem0/blob/c7ee362aff94a369af70f13f2b4f853f6793ff4c/mem0/configs/prompts.py#L468)、[mem0/memory/main.py:942](https://github.com/mem0ai/mem0/blob/c7ee362aff94a369af70f13f2b4f853f6793ff4c/mem0/memory/main.py#L942)

检索侧在同一份源码里也能看到融合形态：`search()` 先算 `bm25_scores`，再算 `entity_boosts`，两者与语义分数一起交给排序层。[mem0/memory/main.py:1379](https://github.com/mem0ai/mem0/blob/c7ee362aff94a369af70f13f2b4f853f6793ff4c/mem0/memory/main.py#L1379)、[mem0/memory/main.py:1652](https://github.com/mem0ai/mem0/blob/c7ee362aff94a369af70f13f2b4f853f6793ff4c/mem0/memory/main.py#L1652)

**Supermemory 把"解决矛盾"和"自动遗忘"当作核心卖点。** README 用 `"I just moved to SF"` 覆盖 `"I live in NYC"` 举例说明矛盾消解，用 `"I have an exam tomorrow"` 举例说明临时事实过期后自动失效；同时明确写了一节 "Memory is not RAG"，把"检索文档块"和"跟踪关于用户的事实"区分开。[README · How memory works under the hood](https://github.com/supermemoryai/supermemory/blob/2415a5c796d62c7ea9d709bc9337a6e1b6f6d837/README.md#how-memory-works-under-the-hood)

**memobase 是"用户画像"这条路上最直白的实现。** 每个用户恒定有一个 user profile 加一条 event timeline，读取只需要几次 SQL，在线延迟声称低于 100ms；对话先进入 per-user buffer 批量处理，借此摊平 LLM 成本（0.0.40 版把单次运行的模型调用固定到 3 次）。README 直接给出 900 轮真实对话的画像产出样例，字段包括 basic_info、demographics、education、interest、psychological、work。[README](https://github.com/memodb-io/memobase/blob/358c16bbc6d687937d79bc2f984a11c3be8da901/README.md)

需要提醒的是，memobase 最近一次推送是 2026-01-11，活跃度明显低于同批项目，且同一团队的新项目已经转向 Acontext。

### 经验与技能派

**Acontext 的立场最锋利：它拒绝向量检索。** 记忆单元就是 Markdown 技能文件，蒸馏在任务完成或失败时触发（LLM 归纳"什么做成了、什么没做成、用户偏好是什么"），然后由 Skill Agent 决定写进已有技能还是新建，写入结构由你自己的 `SKILL.md` schema 定义。召回不走 top-k，而是给 agent `get_skill` / `get_skill_file` 两个工具，让它自己按需取——作者称之为 "progressive disclosure, agent in the loop"。[README · What is Acontext](https://github.com/memodb-io/Acontext/blob/259d73bfdebeed35ec2d4211ddc060a2d4126bc6/README.md#what-is-acontext)、[README · How It Works](https://github.com/memodb-io/Acontext/blob/259d73bfdebeed35ec2d4211ddc060a2d4126bc6/README.md#how-it-works)

**ACE（Agentic Context Engine）走的是策略库路线。** 它维护一个 Skillbook，三个角色分工：Agent 执行、Reflector 分析轨迹、SkillManager 增删改策略。它的创新点是 Recursive Reflector——不是单次摘要轨迹，而是在沙箱里**写并执行 Python** 来搜索模式、定位错误、迭代到找到可执行结论。基于 ACE 论文（arXiv:2510.04618）与 Dynamic Cheatsheet（arXiv:2504.07952）。[README · How It Works](https://github.com/kayba-ai/agentic-context-engine/blob/31f4e11897a93dd9ad90a3d0e2051de66b2255e0/README.md#how-it-works)

**memU 的关键取舍是"记忆服务自己不调用模型"。** 它把会话历史切成自包含的 job，交给 agent 判断"不做事 / 补丁已有技能 / 新建技能"，然后由 `commit` 把结果写回 Markdown 技能并索引名字与描述。README 原文："The judgment and synthesis stay inside the agent. `MemoryService` makes no LLM or chat calls; it stores, embeds, and retrieves the skill Markdown the agent prepared."。核心记忆逻辑自称约 500 行。存储三档：内存、SQLite（暴力余弦）、Postgres（pgvector）。[README · Automatic skill extraction](https://github.com/NevaMind-AI/memU/blob/08e1ed4cdf4c0cb1fe5387e4a532ea588a8cbe46/README.md#automatic-skill-extraction)、[README · Storage backends](https://github.com/NevaMind-AI/memU/blob/08e1ed4cdf4c0cb1fe5387e4a532ea588a8cbe46/README.md#storage-backends)

**Letta 的形态变了，这件事本身值得记一笔。** `letta-ai/letta` 主仓现在只剩历史源码与迁移指引，活跃开发搬到了 `letta-ai/letta-code`（TypeScript harness）。也就是说 MemGPT 血统的那个开源服务端已经退役，当前形态是 CLI/桌面/云。它的记忆机制是另一条路线：memory blocks（agent 改写自己的上下文，包括系统提示学习）、skill learning、`/sleeptime` 周期性 dreaming、`/search` 跨消息检索。最有意思的是 MemFS——**所有上下文包括 memory blocks 都用 git 跟踪**，可以同步到自己的 GitHub 仓库。[letta README](https://github.com/letta-ai/letta/blob/main/README.md)、[letta-code README · Feature Overview](https://github.com/letta-ai/letta-code/blob/9a40d271f8ff77a3f84e4544645c6226faab0dab/README.md#feature-overview)

**OpenViking 把记忆、资源、技能统一成一个虚拟文件系统** `viking://`，agent 用 `ls`/`tree`/`read`/`write`/`search` 操作它。它的分层是 L0 abstract（一句话摘要）、L1 overview（结构与要点）、L2 details（原文按需读），目录里直接落 `.abstract.md` 和 `.overview.md` 文件。会话提交后触发后台抽取，候选记忆与既有记忆比较后做 create/merge/skip，策略可配。[README · Why OpenViking](https://github.com/volcengine/OpenViking/blob/84bbf79711f3d18000beb1d16708e69627743b0e/README.md#why-openviking)

### Markdown 原生派

**ReMe（原 MemoryScope）是本地优先这条线上最完整的。** 对话与资源逐步变成 daily notes 和长期知识，全部是可读可编辑的 Markdown（frontmatter + wikilink），索引可重建；检索用 BM25 + 可选 embedding + wikilink 扩展，返回行级 passage。项目宣称"保留来源"的同时精炼 facts/preferences/procedures/relationships。它的前身 MemoryScope 以 `memoryscope_branch` 分支保留在仓库内，ACL 2026 Findings 论文是《Remember Me, Refine Me》。[README](https://github.com/agentscope-ai/ReMe/blob/9ad3dafce5666c55e8cd5b16cc5ffba42da6cce1/README.md)

**memsearch 把"索引只是影子"这件事做得最干净。** 记忆是 `memory/2026-03-27.md` 这样的按天 Markdown，Milvus 只是可重建的影子索引；文件监听 + SHA-256 分块哈希，内容没变就跳过 embedding 调用。捕获路径是每轮 Stop hook 触发小模型总结后追加，召回是三层渐进：`search` 拿到排名 chunk → `expand` 回到完整 md 段落 → `parse-transcript` 回到原始 `session.jsonl`。同一份记忆服务 Claude Code、Codex、DSH、OpenClaw、OpenCode。[README · Why memsearch](https://github.com/zilliztech/memsearch/blob/f863056e0b113d44e860dd6abf5bb892781e29ca/README.md#why-memsearch)、[README · Markdown as Source of Truth](https://github.com/zilliztech/memsearch/blob/f863056e0b113d44e860dd6abf5bb892781e29ca/README.md#-markdown-as-source-of-truth)

**EverOS 的差异点是双轨与正交切分。** 用户侧是 episodes/profile，agent 侧是 cases/skills，两套一等公民分开；检索可以按 `user_id`、`agent_id`、`app_id`、`project_id`、`session_id` 分别限定。落盘是 Markdown + SQLite + LanceDB，明确宣称不需要 Mongo/Elasticsearch/Redis。reflection 在会话之间离线跑，合并 episode 簇并精炼画像与技能。[README · Why EverOS](https://github.com/EverMind-AI/EverOS/blob/5076683ab88d714390573d8f88ff3c470e51129a/README.md#why-everos)

**SimpleMem 把"少花 token"本身当作记忆系统的第一性问题。** 它的三段管线是：语义结构化压缩（把非结构化交互蒸馏成自包含事实，消解指代、时间戳取绝对值）、在线语义合成（会话内就把相关上下文合并成统一表示，而不是等到查询时才去重）、意图感知检索规划（先推断查询意图，再决定取什么、拼多长的上下文）。自报在 LoCoMo 上 F1 平均提升 26.4%，同时推理 token 降到约三十分之一。它还有个少见的副项目 EvolveMem：让检索配置本身进入 Evaluate → Diagnose → Propose → Guard 的闭环自演化，并在回退时自动回滚。[README · Overview](https://github.com/aiming-lab/SimpleMem/blob/db80b6a7c591e0ea730a058e9f5fc4eb06572299/README.md#-overview)

### 图谱与平台派

**cognee 的操作面只有四个动词**：`remember`（存，可带 session 落成会话记忆）、`recall`（取，自动路由或指定策略）、`improve`（把会话里被接受的结论桥接进永久图）、`forget`（删除）。文本变成实体关系与可检索 chunk，代码变成符号依赖图，查询时按需选择图、向量或代码上下文。[README · How Cognee works](https://github.com/topoteretes/cognee/blob/c0d18c80e24b7b78918e7642c03f6f128fdd2aee/README.md#how-cognee-works)

**TencentDB Agent Memory 是唯一以团队为主要场景的。** 它把记忆拆成四类资产（Chat Memory、Skill、LLM-Wiki、CodeGraph），配 ACL（private/team/restricted）、版本、Owner，以及 "Agent loadout"——按 agent 装配它该用哪些资产。对话分层是 L0 原文 → L1 Atom（事实/偏好/约束/事件）→ L2 Scenario（项目或场景的知识块）→ L3 Core/Persona（长期画像）；平时用 L2/L3 做上下文引导，需要细节时用 BM25 + 向量 + RRF 回落到 L1/L0，并对条目数、字符预算、超时设上限。接入方式是改 base URL 指向它的 proxy，不需要插件或 MCP。[README · Technical Implementation](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/0468a2a5b50eaafc54758ed1e2e6609472e5b6ce/README.md#technical-implementation)

两点提示：它当前的默认分支是 `feat/server_team`，说明项目仍在快速变动；README 徽章写 MIT，但 GitHub 的许可识别是 NOASSERTION，采用前需自行确认。

**MemMachine、Memori、memvid 各自代表一种工程取向。** MemMachine 主打"通用 memory layer、5 行接入、Docker 部署"，但 README 基本停在安装层，没有暴露内部设计，本次未核实到它的记忆机制。Memori 主打企业部署（云、单租户、VPC、on-prem），接法是把 memory 注册进 LLM client，之后对话自动持久化与召回；自报 LoCoMo 87%、平均 721 tokens/query。memvid 是把数据、嵌入、索引、元数据打包进**单个文件**的 Rust 实现，写入是 append-only 的不可变 Smart Frame，因此可以回放、分支、时间旅行调试——代价是自有文件格式。[Memori README](https://github.com/MemoriLabs/Memori/blob/10d65015007131a69b2597fa5130da58da24a0c2/README.md)、[memvid README · Core Concepts](https://github.com/memvid/memvid/blob/e6bd9f7b9c38cd8d5370fa0fc936ac1dcd751813/README.md#core-concepts)

### 更早的两块地基

虽然不是本轮活跃项目，但这两条血脉值得知道。**Generative Agents**（★22.1k，2023）确立了 memory stream + 三维打分检索（recency / importance / relevance）+ reflection 的范式，今天几乎所有"该记什么"的打分都源自它。**HippoRAG**（★4.0k，NeurIPS'24）把长时记忆类比成知识图谱上的 Personalized PageRank，是"图 + 随机游走"这条线的代表。两者的代码都在，可作为算法参照而非工程选型。

## 设计轴：真正分歧的地方

把上面这些项目按"它们在哪一条轴上做了不同选择"重排，比按项目名罗列更有用。以下七条轴是任何记忆系统都绕不开的。

**1. 记忆的主体是谁。** 用户（Honcho、memobase、Supermemory）/ agent 经验（ACE、Acontext、memU、Letta）/ 语料（cognee、OpenViking、memsearch）/ 团队（TencentDB）。这决定了作用域 key 的形态：EverOS 用 user/agent/app/project/session 五维正交切分，TencentDB 用"资产 + loadout"。

**2. 记忆单元多粗。** 从细到粗：原子事实（mem0 与 Supermemory 提取的 facts）→ 分层块（TencentDB L0–L3、OpenViking L0–L2）→ Markdown 页（ReMe、EverOS、memsearch）→ 技能文件（Acontext、memU）→ 不可变帧（memvid）。单元越细，更新越精确、越容易做冲突消解；单元越粗，越容易被人读懂和手改。

**3. 蒸馏在什么时候发生。** 每轮在线（memsearch、mem0）/ 任务结束（Acontext）/ 会话提交或后台队列（Honcho、OpenViking、MemOS）/ 周期性离线（EverOS、Letta `/sleeptime`）/ 只在用户或 agent 显式要求时（memU）。在线延迟与成本、后台复杂度、记忆新鲜度三者互相牵制。

**4. 谁做"什么值得记"的判断。** 模型自由写（Supermemory）/ 模型按你给的 schema 写（Acontext 的 `SKILL.md`）/ 记忆服务完全不调用模型、判断留在 agent 里（memU）/ 人审核后才共享（TencentDB）/ 人直接编辑文件（ReMe、EverOS、memsearch）。这条轴的第 3 和第 5 个选项是可以叠加的。

**5. 新旧记忆冲突怎么处理。** 四种互不兼容的答案：只增不改、用链接和时间消歧（mem0）；提取即消解矛盾、过期自动遗忘（Supermemory）；候选与既有记忆比较后 create/merge/skip（OpenViking）；显式 edit/forget API（MemOS、cognee）。memobase 走的是画像整块重写，是第五种。

**6. 检索怎么做。** 混合检索已是共识（BM25 + 稠密 + RRF + 可选 rerank）；在此之上分三支：图遍历（cognee、MemoryOS、Graphiti）、工具渐进式披露（Acontext、OpenViking、TencentDB 的 `/tools/list` + `/tools/call`）、时序检索（mem0 的 temporal、Graphiti 的时序边）。

**7. 落盘形态。** 服务 + 数据库（Honcho、MemOS、TencentDB）/ 向量库（memsearch 的 Milvus）/ 图库（cognee）/ 嵌入式零服务（EverOS 的 Markdown + SQLite + LanceDB，memU 的 SQLite）/ 单文件（memvid）。这一条直接决定了"用户能不能打开文件自己改"。

## 与 Jarvis 的接口点

Jarvis 现状（见 CONTEXT.md 与 ADR 0001–0005）：只有单会话的轨迹持久化与压缩 checkpoint，跨会话记忆被明确推迟；状态目录在工作区之外；预算线已经收敛成单条；实现是纯 Python + 标准库。

以下只列"如果要做跨会话记忆，绕不开的决策，以及开源侧各自给出的选项"，不构成推荐。

| 决策 | 开源侧可选的做法 |
| --- | --- |
| 记忆归属 | 用户级全局（Honcho 的 peer）／工作区级（TencentDB 的 team）／多维正交（EverOS 的 user+agent+app+project+session） |
| 写入时机 | 每轮单次调用（mem0、memsearch）／任务结束触发（Acontext）／后台队列（Honcho）／离线 reflection（EverOS） |
| 记忆单元 | 原子事实（mem0）／Markdown 页（ReMe、EverOS）／技能文件（Acontext、memU） |
| 冲突策略 | ADD-only + 链接（mem0）／冲突消解 + 过期遗忘（Supermemory）／create-merge-skip（OpenViking）／显式 edit-forget（MemOS） |
| 检索 | 纯关键词 BM25（ReMe 的零依赖形态）／BM25 + 向量 + RRF（多数项目）／工具渐进式披露（Acontext、OpenViking） |
| 注入方式 | 固定前缀（多数项目的 context 打包）／检索工具按需取（Acontext、TencentDB、OpenViking） |
| 可核对性 | 保留原文分层（TencentDB 的 L0、memsearch 的三层回溯到 `session.jsonl`）／不可变帧（memvid）／git 跟踪上下文（Letta MemFS） |

有两处与 Jarvis 现有决策直接相关，值得在设计时优先想清楚：

**一是注入方式与 ADR 0001 的稳定前缀约束。** 现存 ADR 建立在"稳定指令与工具定义保持在消息前缀"这一前提上。多数开源项目把记忆打包成一段可注入的 context（Honcho 的 `Session context`、EverOS 的 profile），那本质上是往前缀里塞每轮变动的内容；而 Acontext、OpenViking、TencentDB 选择把记忆做成工具让 agent 按需取，形态与 Jarvis 已经实现的证据索引更接近。

**二是"本地文件 + 索引可重建"与 Jarvis 的既有价值观的契合度。** 本项目的证据引用、会话归档、原文可核对这几条，跟 ReMe / EverOS / memsearch 的取向是同源的：Markdown 或事件日志是事实，索引只是影子。这条线的好处是零新依赖和可 diff 可手改，代价是自己承担分块、哈希、重建这些机械工作（memsearch 的做法可以直接参照）。

## 学习路径建议

如果只是想先把这个领域看懂，而不是选型，建议按这个顺序读四份材料：

1. **两个原点**：Generative Agents 的 memory stream + 打分检索 + reflection（2023）定义了"记忆流"的基本形态；Letta 的 memory blocks（agent 改写自己的上下文）定义了"记忆即上下文编辑"这条路线。两者构成了后面所有项目的光谱两端。
2. **两个极端**：Honcho 代表"记忆是服务端的用户表征"，Acontext 代表"记忆是文件，召回是工具调用"。把这两个立场对照着看，能最快看清"记忆"这个词在不同项目里的真实所指。
3. **一份最可抄的提示词**：mem0 的 `ADDITIVE_EXTRACTION_PROMPT` 值得逐字读。它把"什么值得记、怎么去重、怎么和旧记忆关联"全部写进了提示词，是目前公开材料里最完整的一份记忆提取规格。[mem0/configs/prompts.py:468](https://github.com/mem0ai/mem0/blob/c7ee362aff94a369af70f13f2b4f853f6793ff4c/mem0/configs/prompts.py#L468)
4. **一个能立刻对照 Jarvis 的形态**：ReMe 与 memsearch 展示了"Markdown 是事实、索引可重建、检索用 BM25 起步"的最小可行形态——它与 Jarvis 现有的会话归档、证据引用最接近，迁移成本也最容易估。

## 未核实与局限

- 所有 benchmark 数字均为项目自报，未复现。跨项目的 LoCoMo 分数不可横向比较。
- 本次未运行任何项目、未读它们的完整源码，只对 mem0 的 2026 算法做了源码级核对（提取提示词与检索融合）；其余项目的机制描述来自各家的 README 与官方文档。
- MemMachine 的 README 未暴露内部设计，它的记忆机制本次未核实。
- Memori、TencentDB Agent Memory 在 GitHub 上的许可字段未识别（NOASSERTION），采用前需确认。
- Letta 的 memory blocks / MemFS 细节本次只读到 README 层，未深入其文档站与代码。
- 本次没有覆盖 Zep/Graphiti（开源但主线是商业产品）、Mem0 的托管平台能力、以及各项目的企业版差异，这些属于商业产品那一路的分工。
