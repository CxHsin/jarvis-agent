# Agent 记忆系统调研：总览

研究日期：2026-09-12。这份文档是入口，负责把下面三份材料收口到 Jarvis 的设计问题上；**机制细节与逐条来源都在各自文件里，本文件不重复证据**。

| 文件 | 覆盖范围 | 方法 |
| --- | --- | --- |
| [agent-memory-papers-survey.md](./agent-memory-papers-survey.md) | 论文与架构，重点在"贴合用户习性"这条线的 2026 年进展 | 七篇读全文 HTML，其余摘要级 |
| [agent-memory-papers-survey-arxiv-verified.md](./agent-memory-papers-survey-arxiv-verified.md) | 43 篇的横向清单，条目更宽 | 全部条目经 arXiv API 与 OpenAlex 核对 |
| [agent-memory-oss-survey.md](./agent-memory-oss-survey.md) | 18 个开源项目的实现形态 | 固定 commit 的 README 与源码；mem0 做过源码级核对 |
| [agent-memory-products.md](./agent-memory-products.md) | 7 个商业产品的记忆设计 | 官方帮助中心与官方文档 |

两份论文文档是互补的：前者读透少数几篇、覆盖 2026 年新工作；后者条目更宽但只到摘要级。**两边的 arXiv 编号都已被逐条核验存在且标题相符**（分别 29 条与 17 条抽查，全部命中）。

## 先说一个分类问题

调研里反复出现的一个坑：**"记忆"这个词在这三份材料里指的是三件不同的事**，混着谈会直接选错方案。

| 类型 | 记忆的主体 | 想解决的问题 | 代表 |
| --- | --- | --- | --- |
| A 用户建模 | 人 | 从交流里提炼出关于这个人的稳定事实与偏好 | Honcho、Supermemory、MemoryBank、PersonaMem-v2、ChatGPT/Claude/Gemini 的记忆 |
| B 经验沉淀 | agent | 从任务轨迹里提炼下次可复用的做法 | ACE、Acontext、memU、Voyager、ExpeL、Memp |
| C 语料组织 | 知识源 | 文档与代码变成可检索的图或文件系统 | cognee、OpenViking、memsearch、RAG 类 |

Jarvis 这次的目标——"通过与用户交流的内容进行提炼，使 Agent 更贴合用户习性"——精确落在 **A**。但 2025–2026 年开源与论文两侧最密集的创新其实在 **B**。选型时如果拿 B 类项目去解 A 类问题，会得到一堆技能库，而不是一个懂你的助手。

需要留意的是，**A 类本身也在 2026 年被重新定义**。此前主流基准（LoCoMo、LongMemEval）测的是"能不能把很久以前说过的事实找回来"，而 DynamicMem、IBA-Bench、Setoka、PersonaMem-v2 这一批开始测**习性、偏好演化与隐式线索**——偏好会漂移、证据往往没被明说、且"知道"不等于"照着做"。这比事实召回难得多，也是"贴合习性"真正要解决的问题。

## 三边的交叉结论

把三份材料放在一起，有五条结论被反复独立地验证：

1. **没有共识，而且分歧集中在同一处。** "记什么、谁决定写、怎么取、新旧冲突怎么办"这四个问题，论文、开源、产品三边给出的答案互不相同。最典型的是冲突处理：mem0 在 2026 年 4 月改成纯 ADD、不改不删；Supermemory 做冲突消解加过期遗忘；Zep 用双时间轴加边失效；ChronoMem 做整库版本回滚。
2. **基准分数不可跨项目比较。** 所有 LoCoMo / LongMemEval 数字都是自报，harness、裁判模型、检索预算各不相同；mem0 自己在 README 里注明托管分数含闭源优化、开源 SDK 只"方向性接近"。三份材料里最扎实的反而是 LongMemEval 的**负向结论**：商业助手与长上下文模型在持续交互中准确率下降约 30%——问题真实存在，但"用上记忆能提升多少"没有可信的公共答案。
3. **蒸馏时机正在从每轮在线移向任务结束或后台。** 留在每轮路径上的只剩 mem0 与 memsearch，且都压到单次调用；Honcho 用后台队列、Acontext 在任务结束触发、EverOS 离线 reflection、Letta 用 `/sleeptime`。
4. **"文件是事实、索引是可重建的影子"成为明确潮流。** ReMe、memsearch、EverOS、Acontext、memU 都把 Markdown 当 source of truth。产品侧则以另一种方式表达了同一件事：Cursor 干脆不自动记忆，让用户把持久上下文写成 Rules / AGENTS.md；Windsurf 官方也建议"一次性事实用 Memories，持久知识用 Rules"。
5. **"写错"比"忘掉"更贵，而且已经是攻击面。** AgentPoison 之后，2026 年长出至少四篇记忆投毒工作；对应的工程答案（可追溯、可版本化、可回滚、写入前校验）在 ChronoMem、MOSS、PGMem 与 GitHub Copilot 的"引用校验 + 28 天淘汰"里同时出现。

## 产品侧最具体的三个信号

产品是唯一有真实用户检验过的一边，它们的取舍比论文更有参考价值：

- **记忆单元在往"条目"退，而不是往"摘要"走。** ChatGPT 与 Claude 都用一张综合摘要承载记忆，两家官方都承认摘要不等于全部记忆；Gemini 连列表都没有，只能靠提问确认。而 GitHub Copilot 与 Microsoft Copilot 都是可枚举、可逐条删除的列表。
- **溯源被做成了回答层的一等公民。** ChatGPT 在回答下方列出这次用到了哪些记忆与哪段过去聊天，并能解释"为什么用它"。这与 Jarvis 既有的证据引用是同一种东西。
- **删除语义是所有人都在打补丁的地方。** ChatGPT 要求删掉信息出现过的全部来源，Gemini 要求删聊天并断开应用，Microsoft Copilot 承认 30 天内重新开启设置会把删掉的记忆加回来。没有一家把"删一条记忆"做成干净利落的操作。

## 对 Jarvis 的建议（是判断，不是调研事实）

以下四条基于上面三份材料与 Jarvis 现有约束（单用户、纯 CLI、纯 Python、会话内 JSONL 事件日志 + 压缩 checkpoint、ADR 0001 的稳定前缀前提、证据引用可回原文）。**这是建议，需要人来定。**

1. **记忆单元选"挂在事件上的条目"，而不是自由文本画像或综合摘要。** 每条记忆指向产生它的会话记录与消息序号。PGMem 的 provenance/evidence 边、HERO 的"保留原始对话"、OpenAI 的"点开来源回到原文"三边指向同一件事。Jarvis 已经有一个事件日志，做这件事的成本比这些产品低。
2. **写入不要放在每轮在线路径上。** 任务边界已经存在（`record_task`），把提炼挂在那里，用户等待路径上不会多出一次模型调用。
3. **读取走工具，不要塞进稳定前缀。** 多数产品默认"记忆永远参与生成"，但 ADR 0001 的缓存与预算都建立在稳定前缀上。MemGPT 的"模型自己翻页"与 Acontext/TencentDB 的按需取，与 Jarvis 已有的证据索引形态同构。
4. **冲突默认不改写旧记忆，并且第一条要有淘汰策略。** Zep 的边失效、ChronoMem 的版本回滚、mem0 的只增不改都优于就地覆盖；GitHub Copilot 的"28 天未使用自动删除 + 用前校验引用"给出了一个可执行的默认值。

## 学习路径

如果只想把这个领域看懂，按这个顺序读四份材料最省时间：

1. **两个原点**：Generative Agents 的 memory stream + 打分检索 + reflection（2023）定义了"记忆流"；MemGPT 的 memory blocks 定义了"记忆即上下文编辑"。后面所有项目都在这两端之间。
2. **两个极端立场**：Honcho 代表"记忆是服务端的用户表征"，Acontext 代表"记忆是文件、召回是工具调用"。
3. **一份最可抄的提取规格**：mem0 的 `ADDITIVE_EXTRACTION_PROMPT`（[源码](https://github.com/mem0ai/mem0/blob/c7ee362aff94a369af70f13f2b4f853f6793ff4c/mem0/configs/prompts.py#L468)）把"什么值得记、怎么去重、怎么和旧记忆关联"全写在提示词里，是公开材料里最完整的一份。
4. **一个能立刻对照 Jarvis 的形态**：ReMe 与 memsearch 展示了"Markdown 是事实、索引可重建、BM25 起步"的最小可行形态，迁移成本最容易估。

## 待定

这四份文档都是新增的未提交文件。按项目约定，接下来的设计决策应走 Issue 与 `CONTEXT.md` 术语更新；本次调研过程中确认了 `CONTEXT.md` 里**没有"记忆"词条**，而它已有的"归档 / 累积 checkpoint / 证据索引"都不是跨会话记忆。这个术语缺口需要在设计定下来时补上。

另外三个尚未回答、需要人来定的问题：

- 记忆的判定场景是哪一条（"新会话里它多知道了什么"）？
- 作用域是用户级、工作区级，还是分层并存？
- 记忆与压缩 checkpoint 的关系：独立管道，还是共享同一次提炼？
