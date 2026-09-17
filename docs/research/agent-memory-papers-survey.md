# Agent 记忆系统：论文与架构侧调研

研究日期：2026-09-12。只读一手材料：arXiv 摘要页、论文全文 HTML、作者公开的项目页；**未运行任何系统、未复现任何 benchmark、未调用付费模型**。本文只覆盖论文与架构；开源实现见 `docs/research/agent-memory-oss-survey.md`，商业产品另文。

证据分级写在每节里：**全文**＝读过该论文的 HTML 全文；**摘要**＝只读了 arXiv 摘要页。所有性能数字都是各论文自报，各自的 harness、模型与检索预算不同，**跨论文不可横向比较**。

## 结论

**一、这个领域有清晰的三代。** 2023 年那批（Generative Agents、MemoryBank、Reflexion、Voyager）把记忆定义成"记下经历并偶尔反思"；2023 年底的 MemGPT 把它重构成"上下文分页"，记忆是模型用函数调用主动管理的虚拟内存；2024 年以后，记忆被当成基础设施来做——抽取式事实（Mem0、A-MEM）、时序知识图谱（Zep/Graphiti）、图检索（HippoRAG）、记忆操作系统（MemOS、MemoryOS）。三代不是替代关系，今天的新系统仍在混用这三套词汇。

**二、"提炼对话以贴合用户习性"是 2026 年才独立出来的题目。** 在此之前，主流基准测的是**事实召回**：能不能从很久以前的对话里把那个点找回来（LoCoMo）或跨会话推理（LongMemEval）。用户**习性**（habits）、**偏好演化**、**隐式线索**成为基准对象，是从 DynamicMem、IBA-Bench、Setoka 开始的；把画像信号与支撑它的事件绑在一起，是 PGMem 与 HERO 的贡献。

**三、四个设计点各有明确分歧，没有共识答案。**

| 设计点 | 分歧 |
| --- | --- |
| 记什么 | 原始经历（Generative Agents）／抽取后的事实（Mem0、A-MEM）／画像与事件分层（MemoryBank、MemoryOS）／技能代码（Voyager） |
| 谁决定写 | 模型自己写（MemGPT 的函数调用）／抽取管道自动写（Mem0）／任务反馈后写（Reflexion）／版本化快照（ChronoMem） |
| 怎么取 | 打分 top-k（Generative Agents）／图上游走（HippoRAG）／混合检索 + rerank（Zep）／模型自己翻页（MemGPT） |
| 新旧冲突 | 只增不改 + 链接（Mem0 的 2026 版）／改写旧记忆（A-MEM 的 memory evolution）／边失效（Zep）／版本回滚（ChronoMem） |

**四、时序性是分水岭。** "用户改主意了"这件事在 2023 年的架构里基本没有表达方式（只能追加或覆盖）。Zep/Graphiti 用双时间轴加边失效把它变成一等公民，ChronoMem 干脆做整库版本控制与语义回滚。对个人 Agent 来说，这是"贴合习性"能不能成立的关键：偏好本身是会漂移的。

**五、收益有证据，但证据强度被高估了。** 最扎实的是 LongMemEval 的自证：商业助手与长上下文模型在持续交互中**准确率下降 30%**，这是个负向结论，恰恰说明问题真实存在而非论文自夸。正向数字（MemGPT 的 DMR 92.5%、Zep 的 94.8%、Mem0 的 26% 相对提升）都是各系统在自选基准上的自报，不能当成"用上记忆就能提升这么多"。

**六、"写错"比"忘掉"更贵，而且已经是新的攻击面。** AgentPoison（2024）证明了投毒记忆即可操纵 agent 行为；2026 年这条线长出至少四篇（InjecMEM、FragFuse、When Agents Remember Too Much、MemGauge）。任何长期记忆一旦写入就会持续影响后续每一次对话，这让"可追溯、可版本化、可回滚"从加分项变成防御前提（ChronoMem、MOSS、PGMem 都把这当卖点）。

## 机制对比表

| 论文/系统 | 时间 | 记忆单元 | 写入 | 读取 | 更新/遗忘 | 与"用户习性"的距离 |
| --- | --- | --- | --- | --- | --- | --- |
| [Generative Agents](https://arxiv.org/abs/2304.03442) | 2023-04 | 自然语言经历记录（memory stream） | 每步追加 | relevance + recency + importance 打分 top-k | 只增；reflection 合成高层推断 | 建模的是 agent 自身与他人，不是用户 |
| [Reflexion](https://arxiv.org/abs/2303.11366) | 2023-03 | 语言化的反思文本 | 任务失败后写入 | 注入后续尝试 | 累积在 episodic buffer | 无用户模型 |
| [Voyager](https://arxiv.org/abs/2305.16291) | 2023-05 | 可执行代码技能 | 技能自验证通过后入库 | 检索技能 | 库只增 | 无用户模型（技能线后来的祖先） |
| [MemoryBank](https://arxiv.org/abs/2305.10250) | 2023-05 | 对话事件 + 用户画像 | 每轮 | 检索 + 画像 | Ebbinghaus 遗忘曲线：按时间与重要度衰减与强化 | 最早明确对用户画像建模 |
| [MemGPT](https://arxiv.org/abs/2310.08560) | 2023-10 | 虚拟上下文：main context + archival storage | 模型用 function call 自己写 | 模型自己检索、翻页、驱逐 | 模型自己改写 | 无用户模型，但机制能承载 |
| [HippoRAG](https://arxiv.org/abs/2405.14831) | 2024-05 | 知识图谱 | 离线索引构建 | 图上的 Personalized PageRank | 无显式遗忘 | 面向知识而非用户 |
| [Mem0](https://arxiv.org/abs/2504.19413) | 2025-04 | 抽取出的显著信息（+ 图变体） | 每对消息经抽取/评估/更新模块 | 语义与图检索 | 论文版是提取-评估-更新；开源实现 2026 已转向纯 ADD | 事实以用户为中心 |
| [A-MEM](https://arxiv.org/abs/2502.12110) | 2025-02 | Zettelkasten 式结构化笔记 | 新记忆生成笔记 | 链接网络 | link generation + memory evolution（改旧笔记） | 记忆内容自理，无用户偏好语义 |
| [Zep / Graphiti](https://arxiv.org/abs/2501.13956) | 2025-01 | 时序知识图谱（episode / entity / community 三层） | episode 摄取 | 混合检索 + reranker | 边失效（带时间区间）+ 双时间轴 | 最强：能表达"过时的偏好" |
| [MemoryOS](https://arxiv.org/abs/2506.06326) | 2025-05 | 三层存储：短期/中期/长期个人记忆（LPM） | 对话链 FIFO 逐层上推 | 按层检索（含热度） | 更新模块维护 | 把稳定 persona 当长期存储一等公民 |
| [MemOS](https://arxiv.org/abs/2507.03724) | 2025-07 | MemCube（明文/激活/参数三类记忆） | API 与调度器 | 混合检索与调度 | 生命周期建模与状态迁移 | 提供治理与生命周期词汇 |

## 逐主题展开

### 1. 三块地基（2023）

**Generative Agents 定义了"记忆流"。** 完整记录经历 → 检索时按 relevance、recency、importance 三项打分 → 定期 reflection 把碎片合成更高层推断。今天几乎所有"该记什么、怎么排序"的讨论都还是这套框架的变体。[arXiv](https://arxiv.org/abs/2304.03442)（全文）

**Reflexion 把"失败"变成可复用的语言记忆。** 它不更新权重，而是让 agent 对任务反馈做语言化反思，存进 episodic memory buffer，供下一次尝试使用。[arXiv](https://arxiv.org/abs/2303.11366)（摘要）

**Voyager 的 skill library 是"经验沉淀"这条线的祖先。** 技能是可执行代码，经自验证后入库，可跨任务检索复用。[arXiv](https://arxiv.org/abs/2305.16291)（摘要）

**MemoryBank 是第一批明说"面向个人陪伴"的工作。** 它同时维护对话记忆与用户画像，并用 Ebbinghaus 遗忘曲线按时间与重要度做衰减和强化。[arXiv](https://arxiv.org/abs/2305.10250)（全文）

### 2. 上下文即记忆：MemGPT

MemGPT 把 LLM 的上下文当作"主存"，外部存储当作"磁盘"，让模型用 function call 在两者之间分页；论文的核心主张是"用函数调用让 agent 读写外部数据、修改自己的上下文"。[arXiv](https://arxiv.org/abs/2310.08560)（全文）

它同时给了这个领域最早的量化结果之一——DMR（deep memory retrieval）任务：GPT-3.5 Turbo 38.7% → MemGPT 66.9%，GPT-4 32.1% → 92.5%，GPT-4 Turbo 35.3% → 93.4%，ROUGE-L 同步上升（论文 Table 2）。**注意这个任务本身是"就前 5 段会话里的某个话题回答一个具体问题"**，测的是找回事实，不是理解一个人。

MemGPT 的开源血脉 MemGPT→Letta 的当前形态见 `docs/research/agent-memory-oss-survey.md`（主仓已转为归档，活跃开发在 `letta-ai/letta-code`）。

### 3. 抽取与更新：Mem0、A-MEM、MemoryOS、MemOS

**Mem0 的论文版是"抽取-评估-更新"三件套**：从消息对里抽出显著信息，再决定如何并入既有记忆；图变体（Mem0^g）额外建关系结构。论文在 LoCoMo 上报告相对 OpenAI 记忆**26% 的 LLM-as-a-Judge 相对提升**，图变体比基础版高约 2%。[arXiv](https://arxiv.org/abs/2504.19413)（全文）

值得注意的是**论文与实际开源实现已经分叉**：开源仓库在 2026 年改成了纯 ADD、不 UPDATE 不 DELETE，用 `linked_memory_ids` 表达新旧关联（见 OSS 那篇的源码核对）。引用 Mem0 时必须说清是哪一版。

**A-MEM 借用 Zettelkasten 的"原子笔记 + 双向链接"。** 新记忆写入时会生成结构化笔记，触发两个操作：link generation（与既有笔记建链）和 memory evolution（**回头修改既有笔记的上下文表示**）。它在 LoCoMo 问答上按 Single Hop / Multi Hop / Temporal / Open Domain / Adversarial 五类报告 F1 与 BLEU-1；在 DialSim 上报告 F1 3.45，对比 LoCoMo 的 2.55 与 MemGPT 的 1.18。[arXiv](https://arxiv.org/abs/2502.12110)（全文）

**MemoryOS 与 MemOS 都想做"记忆的操作系统"。** MemoryOS 把存储分成三层——短期记忆、中期记忆、**长期个人记忆（LPM，即 persona）**，外加存储、更新、检索、生成四个模块；更新规则写得很具体：短期到中期按对话链 FIFO 推进，中期再往 LPM 汇。它把"跨会话的知识保留与稳定 persona"直接写成设计目标，是这一批里**最明确把用户画像当作长期存储一等公民**的论文。[arXiv](https://arxiv.org/abs/2506.06326)（全文）

MemOS 则提出 MemCube 作为记忆资源的统一封装，区分明文记忆、激活记忆与参数记忆，并强调生命周期与治理。[arXiv](https://arxiv.org/abs/2507.03724)（全文）

### 4. 时序与图：Zep/Graphiti、HippoRAG

**Zep 的价值在于把"时间"做成图的属性而非元数据。** Graphiti 建三层子图——episode（原始输入，非有损存储）、semantic entity（实体与事实）、community（社区摘要）；事实边上带时间信息，`T` 是事件发生的时间轴，`T'` 是数据被摄取的事务时间轴（双时间轴）；当新事实取代旧事实时做 **edge invalidation**，而不是删除。[arXiv](https://arxiv.org/abs/2501.13956)（全文）

它的自报数字是 DMR 94.8% 对 93.4%（对手是 MemGPT），LongMemEval 上"最高 18.5% 的准确率提升、延迟降低 90%"。这两处都写在论文里，但基准与判分由作者选。

**HippoRAG 走的是另一条生物学隐喻**：把长期记忆类比海马索引，用知识图谱 + Personalized PageRank 做多跳检索，而不是把相似度 top-k 当终点。[arXiv](https://arxiv.org/abs/2405.14831)（摘要）

### 5. 贴合用户习性：从 LaMP 到 2026 的画像-证据耦合

这条线可以看成四步：

**第一步，把个性化变成可测任务。** LaMP 给了七个个性化任务（三个分类、四个生成），并提出从用户历史里检索相关条目来增强输出的做法。[arXiv](https://arxiv.org/abs/2304.11406)（摘要）

**第二步，承认这是独立的研究方向。** 2024 年底的综述《Personalization of Large Language Models》第一次把"个性化生成"与"用 LLM 做推荐"两条线合并成一个分类框架。[arXiv](https://arxiv.org/abs/2411.00027)（摘要）

**第三步（2025–2026），把画像与支撑它的证据绑起来。**

- **PPRO**：从对话历史建 episodic 与 semantic 两个记忆库，再从中导出用户画像，把画像当作排序的显式先验，让检索"对用户而不是对查询"敏感。[arXiv](https://arxiv.org/abs/2607.00017)（摘要）
- **PGMem**：指出既有系统把画像存成与事件脱钩的扁平 profile，造成"记忆-画像有效性缺口"；它建异构图，用带类型的 provenance / evidence 边连接事件节点与画像节点，检索时按**证据有效性**排序。[arXiv](https://arxiv.org/abs/2608.01708)（摘要）
- **HERO**：批评"压缩与改写会造成信息损失与语义漂移"，转而建保留原始对话文本的可追溯异构图，再叠加人类画像增强的检索优化。[arXiv](https://arxiv.org/abs/2608.22310)（摘要）
- **MOBIMEM**：把自演化从模型权重里拆出来，用 Profile Memory 等三种记忆原语承担个性化、能力与效率的迭代。[arXiv](https://arxiv.org/abs/2512.15784)（摘要）

**第四步，开始测"习性"本身。**

- **DynamicMem**：合成 15 个月活动，画像由 attributes / habits / preferences 三类组成，三者的演化时间尺度不同；变化由季节与生活事件等外部语境驱动；证据很少被明说，散落在不同应用的小动作里等系统去推断。[arXiv](https://arxiv.org/abs/2606.22877)（摘要）
- **IBA-Bench**：把问题叫做 knowledge-to-action gap——既有基准依赖静态偏好快照或问答题，测不到"偏好是否真的影响了任务执行"；它用带噪声、隐式线索与时间不一致的长期交互史测隐式行为对齐。[arXiv](https://arxiv.org/abs/2608.02171)（摘要）
- **Setoka**：认为既有记忆基准只测"能不能把说过的事找回来"，提出四个层次的用户理解（从显式事实到抽象人格特征），并用异构数据评测。[arXiv](https://arxiv.org/abs/2607.27056)（摘要）

这一节的共同指向很清楚：**"记住事实"和"贴合习性"是两个不同的问题**，而后者要求画像信号永远挂在支撑它的事件上，并且承认它会过期。

### 6. 基准演化

| 基准 | 时间 | 测什么 | 构造方式 | 关键结论 |
| --- | --- | --- | --- | --- |
| [LoCoMo](https://arxiv.org/abs/2402.17753) | 2024-02 | 超长跨会话记忆：问答、事件摘要、多模态对话生成 | 人机协作生成，基于 persona 与时间事件图，最长 35 段会话、平均 9K token | 长上下文与 RAG 有改善，但仍显著落后人类 |
| [LongMemEval](https://arxiv.org/abs/2410.10813) | 2024-10 | 五项能力：信息抽取、跨会话推理、时间推理、**知识更新**、拒答 | 500 道题嵌入可自由扩展的对话历史 | 商业助手与长上下文模型在持续交互中**准确率下降 30%**；把记忆设计拆成索引、检索、阅读三段 |
| [DynamicMem](https://arxiv.org/abs/2606.22877) | 2026-06 | 跨月画像：属性、**习性**、偏好的演化 | 合成 15 个月活动，含外部语境驱动与隐式证据 | 既有基准的短交互测不到真实画像的三条性质 |
| [IBA-Bench](https://arxiv.org/abs/2608.02171) | 2026-08 | 隐式行为对齐（偏好是否影响做事方式） | 长期交互史 + 噪声 + 时间不一致 | 点名"知识-行动缺口" |
| [Setoka](https://arxiv.org/abs/2607.27056) | 2026-07 | 分层用户理解（显式事实 → 抽象特征） | 异构数据，心理学理论分层 | 显式事实检索不等于理解用户 |

LongMemEval 那句"索引、检索、阅读"的三段拆法，是目前最有工程指导意义的一条结构化结论：记忆系统的失效可以定位到具体是哪一段坏了。

### 7. 风险与可审计性

**投毒这条线已经成形。** AgentPoison 是最早的 red-teaming 工作：通过对记忆或知识库投毒，让 agent 在检索到特定触发条件时执行攻击者意图的动作，且论文声称攻击成功率与良性性能损失都优于既有方法。[arXiv](https://arxiv.org/abs/2407.12784)（摘要）

2026 年的续作把攻击面铺得更开：InjecMEM 针对记忆系统的注入、FragFuse 用记忆查询分片绕过访问控制、When Agents Remember Too Much 讨论"记太多"本身的风险。而 MemGauge 试图把这件事变成可控实验——分别调节**写入准入、管理策略、检索暴露**三段，在干净与投毒两种条件下测效用-风险权衡。[arXiv](https://arxiv.org/abs/2608.23471)、[arXiv](https://arxiv.org/abs/2606.15609)、[arXiv](https://arxiv.org/abs/2607.06595)、[arXiv](https://arxiv.org/abs/2608.30177)（均为摘要）

**对应的工程答案也在 2026 出现。** ChronoMem 认为既有记忆系统都是"只进不退"的，遇到纠正、概念漂移或记忆污染就脆弱，于是给每次记忆写入做整库快照提交、维护版本历史并支持语义回滚（集成在 Google 的 ADK 里）。MOSS 则直接拒绝把向量相似度当检索终点，改为让 agent 在关系数据库上做**符号化、可复现**的检索，卖点是可审计。[arXiv](https://arxiv.org/abs/2607.27773)、[arXiv](https://arxiv.org/abs/2607.04391)（均为摘要）

这与 Jarvis 已有的两条价值观同源但目前没有交集：会话记录是事件日志、证据引用要求可回到原文，但**记忆一旦被抽取出来，就脱离了事件日志**。要引入记忆，这条追溯链必须显式设计，而不是靠约定。

## 对 Jarvis 的可迁移结论

以下是把上面的机制对照 Jarvis 现有约束后的判断，**是建议而非事实**。约束来自 `CONTEXT.md` 与 ADR 0001–0005：稳定指令与工具定义保持在消息前缀、单条预算线、证据引用必须能回原文、会话记录是事件日志、纯 Python 标准库。

1. **记忆单元优先选"挂在事件上的画像信号"，而不是自由文本画像。** PGMem 的 provenance/evidence 边与 HERO 的"保留原始对话文本"都在解决同一个问题：画像写错了没法追责。Jarvis 已有会话记录与证据引用，把每条记忆指向产生它的会话记录与消息序号，成本最低、收益最直接。
2. **写入时机不要放在每轮在线路径上。** MemGPT 让模型自己写、Mem0 每轮抽取，都要在用户等待的路径上加一次模型调用；Reflexion 反馈后写、Honcho/Acontext 的任务结束或后台写，是把成本挪出关键路径的成熟做法。Jarvis 的任务边界已经是既有概念。
3. **冲突策略要有明确答案，且默认"不改写旧记忆"。** Zep 的边失效、ChronoMem 的版本回滚、Mem0 的只增不改，三种都优于"就地覆盖"。就地覆盖会同时破坏可审阅性和可回滚性。
4. **读取走工具，不要塞进稳定前缀。** ADR 0001 的缓存与预算都建立在前缀稳定上；多数论文默认"把记忆打包进上下文"，那会直接打破这个前提。MemGPT 的"模型自己翻页"与 Acontext/TencentDB 的按需取，与 Jarvis 已有的证据索引形态同构。
5. **验收标准要自己造，不要用 LoCoMo 数字。** LongMemEval 的"知识更新"与 DynamicMem 的"习性演化"才是这个项目要的行为；建议先写几条具体场景（例如"新会话里问上次的目录"、"我改口之后旧偏好不再出现"、"它能不能说出这条记忆是从哪次对话来的"），这些是 LoCoMo F1 测不到的。
6. **风险防御从第一天就要有。** 记忆写入必须可追溯、可删除、可回滚；写入准入（什么值得记）与检索暴露（把什么放进上下文）是两个独立的闸门（MemGauge 的三段拆法）。个人助手的记忆是长期资产，一旦被投毒或写错，代价随对话轮数线性放大。

## 未验证事项

- 未运行任何系统、未复现任何 benchmark；所有性能数字均为论文自报，且各自 harness 不同，本文不做跨论文比较。
- 2026 年的论文（ChronoMem、MOSS、PGMem、HERO、PPRO、DynamicMem、IBA-Bench、Setoka、MemGauge、InjecMEM、FragFuse 等）只读了 arXiv 摘要页，**机制细节未逐条核对全文**；标注（全文）的除外。
- 部分 2025 年论文（Reflexion、Voyager、HippoRAG）只读到摘要；MemGPT、Generative Agents、MemoryBank、Mem0、A-MEM、Zep、MemoryOS、MemOS 读了全文 HTML，但 Mem0 图变体的具体实现细节未逐条核对。
- arXiv 检索用 `export.arxiv.org` 的 API 与 `arxiv.org/abs/` 页面，检索式只有两条（"user profile" + "LLM agent"、"agent memory" + "personalization"，按提交日期倒序前 15 条），**不构成对 2026 年文献的穷尽覆盖**，可能漏掉未命中这两个关键词的工作。
- 未核实论文是否已被会议接收（部分条目只有 arXiv 版本），引用时需自行确认。
