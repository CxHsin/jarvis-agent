# Agent 记忆系统：论文调研（论文部分）

研究日期：2026-09-12。检索方式是：以 arXiv API 取条目本身（标题、首次提交日期、作者、摘要原文），用 OpenAlex 核对 DOI、发表场所与引用量。每条描述都指向具体论文，未读全文的地方在文末「未核实与局限」里列出。本文件只覆盖**论文**；开源项目见同目录的 `agent-memory-oss-survey.md`，商业产品由同批调研的另外一支覆盖，因此这里不重复。同一批调研里另有一位 agent 产出了 `agent-memory-papers-survey.md`，那份读的是七篇论文的全文，本份是更宽但只到摘要级的版本，两者互补。

与开源侧一样，本文件描述的是一篇论文在它自己那份稿件里宣称的东西，不代表已被独立复现。引用量取自 OpenAlex 在 2026-09-12 的快照，只作热度参考，不作为质量判据。

## 结论

**一、论文里的「记忆」和开源侧一样至少是四件事，而且分类更细。** 上下文管理（MemGPT、CoALA）、会话记忆与用户画像（MemoryBank、SeCom、PersonaMem-v2）、结构化检索（HippoRAG、Zep、A-MEM）、经验与技能（Voyager、ExpeL、Memp、ACE）。你说要做的「把交流内容提炼成更贴合用户习性的东西」，在论文这边精确对应第二类里的一条支线：*隐式用户偏好（implicit preferences）*。这条线在 2025 年底之后才真正被独立评测，代表是 PersonaMem-v2。

**二、2026 年最大的转向是「记忆写入策略从提示词启发式变成可训练策略」。** 2023–2025 年的写入规则都是人写的（阈值、打分、模板），2026 年出现了一批把「什么时候写、写什么、什么时候删」当学习问题的工作：Memory-R1 用 RL 让模型学会 ADD/UPDATE/DELETE/NOOP 四种操作，只用 152 条训练问答；ACE 用增量 delta 演化上下文而不是整段重写；PersonaMem-v2 用强化微调把小模型（Qwen3-4B）推到超过前沿模型。这条线对 Jarvis 的意义是：写入规则可以先用启发式起步，但要把「可被替换成学习策略」当成接口约束。

**三、记忆单元的粒度是这三年里被反复验证、结论最一致的一个变量。** SeCom 把这一点做成了论文的主要发现：turn 级、session 级、摘要级各有缺陷，**segment 级**（用分段模型切出话题连贯的段）更好，而且把 LLMLingua-2 这类提示压缩当去噪器用还能再提升检索准确率。SimpleMem 把同一件事推到底，整篇论文的命题就是「语义无损压缩」——把记忆的信息密度本身当作第一性问题。工程含义：不要只在「整段原文」和「一句摘要」两个极端之间选。

**四、结构增强有个明确的教训：图不是免费的。** HippoRAG 用知识图谱 + Personalized PageRank 模拟海马索引，在多跳问答上比当时最好方法高约 20%，且单步检索比 IRCoT 便宜 10–30 倍；但 HippoRAG 2 那篇的出发点恰恰是「加了结构的 RAG 在**基础事实记忆**上反而退步」——作者原话是 unintended deterioration。HippoRAG 2 的目标是同时在事实记忆、意义建构、联想记忆三类任务上都不输标准 RAG。2026 年的 GAM、MAGMA 继续走分层图，但都要么显式处理瞬态噪声，要么把图拆成多张。

**五、遗忘与驱逐从实现细节升成了一等公民。** MemoryBank 用艾宾浩斯遗忘曲线决定「忘记与强化」；MemoryOS 的短→中→长期迁移分别是对话链 FIFO 和分段页组织；2026 年 9 月已经有人专门审计驱逐造成的损失（What Eviction Destroys，用 restore counterfactual 区分「驱逐造成的不可逆损失」和「可恢复的检索失败」）。对 Jarvis：记忆系统的失败模式不只是「记错了」，还有「删早了」。

**六、记忆安全成为一个独立子领域，并且有理论化的批判。** 攻击面（memory poisoning，通过纯查询交互注入）、生命周期治理（长时记忆安全综述把它归纳为 persistence、statefulness、propagation 三性）、以及一篇立场鲜明的批判论文（Contextual Agentic Memory is a Memo, Not True Memory）——后者援引神经科学的互补学习系统理论，论证「检索 ≠ 记忆」，并称把两者混同会让 agent 永远积累笔记却长不出专长，且结构性地易受持久记忆投毒。这篇是 2026 年最值得先读的反方观点。

**七、评测线清晰可见：LoCoMo → LongMemEval → LongMemEval-V2。** 2024 年 2 月的 LoCoMo 做「超长期闲聊记忆」，2024 年 10 月的 LongMemEval 把它变成可诊断的基准（ICLR 2025），2026 年 5 月的 LongMemEval-V2 明确转向「专业 web 环境里的工作经验」——界面可供性、状态动态、工作流、反复出现的失败模式。同时 PersonaMem-v2 给出了隐式偏好的基准。**但这些分数仍然不可跨论文比较**，各自 harness 不同。

**八、有一条论文结论对 Jarvis 特别值得注意：单一人类可读记忆可以很省。** PersonaMem-v2 的 agentic memory 框架维持「一条随用户增长的、人类可读的记忆」，用 2k token 的记忆替代 32k 的完整对话，报告 16 倍更少的输入 token 且准确率最高（55%）。这和 ReMe / memsearch 那派「Markdown 是事实源」在结论上撞上了。

## 对比表

标注「场所」的只在能从 arXiv comment 或 OpenAlex 记录核对到时才写。

| 论文 | 类别 | 提交 | 记忆单元 | 写入时机 | 读取 | 关键机制 |
| --- | --- | --- | --- | --- | --- | --- |
| [Generative Agents](https://arxiv.org/abs/2304.03442) | 原点 | 2023-04 | 自然语言观察流 | 每步追加 | 按 recency / importance / relevance 打分召回 | reflection 生成更高阶洞察并参与规划 |
| [MemGPT](https://arxiv.org/abs/2310.08560) | 上下文管理 | 2023-10 | 分层上下文（主上下文 + 外部存储） | 模型自己调函数读写 | 分页式换入换出 | 用 OS 的虚拟内存类比做 virtual context management，interrupts 控制与用户交接 |
| [CoALA](https://arxiv.org/abs/2309.02427)（TMLR） | 框架 | 2023-09 | 分类框架：working / episodic / semantic / procedural | — | — | 给整个领域提供词汇表，多数后续论文的「记忆类型」都从这里借 |
| [Reflexion](https://arxiv.org/abs/2303.11366) | 经验 | 2023-03 | 语言化的自我反思 | 失败后 | 下一轮 trial 读回 | 口头强化学习，不改权重 |
| [Voyager](https://arxiv.org/abs/2305.16291) | 经验 | 2023-05 | 可执行代码技能库 | 技能验证通过后 | 按描述检索复用 | 自动课程 + 自我验证，技能是可运行的代码 |
| [ExpeL](https://arxiv.org/abs/2308.10144)（AAAI-24） | 经验 | 2023-08 | 从轨迹抽取的 insight 池 | 任务结束后 | 检索 insight 注入提示 | 免微调的经验学习 |
| [MemoryBank](https://arxiv.org/abs/2305.10250)（AAAI 2024） | 用户画像 | 2023-05 | 记忆条目 + 用户画像 | 对话后总结更新 | 召回相关记忆 | 艾宾浩斯遗忘曲线驱动的遗忘与强化；明确面向陪伴与心理咨询场景 |
| [Think-in-Memory](https://arxiv.org/abs/2311.08719) | 会话记忆 | 2023-11 | 历史 thoughts | 每次回复后 | 生成前 recall | 针对「重复推理同一段历史得到不一致结论」这个具体缺陷 |
| [SeCom](https://arxiv.org/abs/2502.05589) | 会话记忆 | 2025-02 | segment 级记忆 | 分段模型切话题段 | 检索 + 提示压缩去噪 | 两发现：粒度决定成败；LLMLingua-2 当去噪器提升检索准确率 |
| [MIRIX](https://arxiv.org/abs/2507.07957) | 多类型 | 2025-07 | 六类：Core / Episodic / Semantic / Procedural / Resource / Knowledge Vault | 多 agent 协调更新 | 多 agent 协调检索 | 明确处理视觉与多模态记忆，ScreenshotVQA 上约两万张截图/序列 |
| [MemInsight](https://arxiv.org/abs/2503.21760)（EMNLP 2025） | 语义结构 | 2025-03 | 属性增强后的记忆条目 | 自主增广 | 语义检索 | 自动为历史交互挖属性做结构化；LLM-REDIAL 上推荐说服力最高 +14% |
| [HippoRAG](https://arxiv.org/abs/2405.14831)（NeurIPS 2024） | 检索结构 | 2024-05 | 开放知识图谱的节点与边 | 建索引时 | Personalized PageRank 单步检索 | 海马索引理论；多跳 QA 最高 +20%，比 IRCoT 便宜 10–30 倍、快 6–13 倍 |
| [HippoRAG 2](https://arxiv.org/abs/2502.14802)（ICML 2025） | 检索结构 | 2025-02 | 图 + 段落节点 | 同上 | 更深的段落编码与识别记忆 | 修掉「图结构导致基础事实检索退步」这个 unintended deterioration |
| [Zep / Graphiti](https://arxiv.org/abs/2501.13956) | 检索结构 | 2025-01 | 时序知识图 | 会话与业务数据持续流入 | 图检索 | 时间感知的图引擎，保留历史关系；在 MemGPT 团队自己定的 DMR 上超过 MemGPT |
| [Mem0](https://arxiv.org/abs/2504.19413) | 通用记忆层 | 2025-04 | 提取出的显著事实（另有图变体） | 对话过程中动态提取与合并 | 混合检索 | 在 LOCOMO 上与六类基线对比，含全上下文与不同 chunk 的 RAG |
| [A-MEM](https://arxiv.org/abs/2502.12110)（NeurIPS 2025） | 通用记忆层 | 2025-02 | Zettelkasten 式笔记（描述、关键词、标签） | 新记忆入库时建链 | 链接扩展 | memory evolution：新记忆会触发对旧记忆属性与上下文的更新 |
| [MemoryOS](https://arxiv.org/abs/2506.06326) | 分层存储 | 2025-05 | 短期 / 中期 / 长期个人记忆 | 短→中按对话链 FIFO；中→长按分段页组织 | 分层检索 | OS 类比；LoCoMo 上 GPT-4o-mini 平均 F1 +49.11%、BLEU-1 +46.18% |
| [SimpleMem](https://arxiv.org/abs/2601.02553) | 压缩优先 | 2026-01 | 多视图索引的压缩记忆单元 | 写入即语义结构化压缩；会话内即时合成 | 意图感知的检索规划 | 命题是信息密度；LoCoMo 平均 F1 +26.4% 并大幅降低推理 token |
| [Memory-R1](https://arxiv.org/abs/2508.19828)（ACL 2026 长文） | 学习式管理 | 2025-08 | 记忆条目 + 四种操作 | RL 学会 ADD / UPDATE / DELETE / NOOP | Answer Agent 先筛选再推理 | PPO / GRPO 训练，仅 152 条训练问答 |
| [Memp](https://arxiv.org/abs/2508.06433)（ACL 2026 Findings） | 程序性记忆 | 2025-08 | 步骤级指令 + 脚本级抽象 | Build / Update / Deprecate 全流程 | Retrieval 策略对比实验 | 把轨迹蒸成可复用流程，并用动态机制持续纠正与废弃；TravelPlanner、ALFWorld |
| [ACE](https://arxiv.org/abs/2510.04618)（ICLR 2026） | 经验 | 2025-10 | 上下文条目（playbook） | 增量 delta 更新 | 注入 | 针对「简洁偏置」导致细节丢失，改成增量演化而非整段重写 |
| [Dynamic Cheatsheet](https://arxiv.org/abs/2504.07952) | 经验 | 2025-04 | 一份持久 cheatsheet | 测试时累积 | 读取 | 免训练；黑盒 LM 也能跨样本积累策略 |
| [EM-LLM](https://arxiv.org/abs/2407.09450) | 事件记忆 | 2024-07 | 事件级片段 | Bayesian surprise + 图论边界细化，在线切分 | 相似度 + 时间连续的两阶段检索 | 不微调处理「近似无限」上下文 |
| [Larimar](https://arxiv.org/abs/2403.11901)（ICML 2024） | 参数化记忆 | 2024-03 | 分布式事件记忆 | 写入即更新 | 按需读取 | 把知识编辑做成内存更新，而不是重新训练 |
| [MemoRAG](https://arxiv.org/abs/2409.05591) | 长上下文 | 2024-09 | 全局记忆（gist） | 预处理建全局记忆 | clue-guided 检索 | 从全局记忆生成线索再去精确定位 |
| [Second Me](https://arxiv.org/abs/2503.08102) | 用户画像 | 2025-03 | 个人记忆 | — | — | AI-native memory 2.0：用户不必向每个服务重复交代同一批个人信息 |
| [PersonaMem-v2](https://arxiv.org/abs/2512.06688) | 用户画像 | 2025-12 | 一条人类可读、随用户增长的记忆 | 训练出的 agentic memory | 读回 | 隐式偏好基准：1000 段交互、300+ 场景、20000+ 偏好、128k 上下文；前沿模型仅 37–48% |

## 逐篇证据

### 原点与框架：四个不同的起点

**Generative Agents 定了「记忆流」的原型。** 记忆是自然语言的观察记录，每步追加；召回按新近度、重要度、相关性三个维度打分；反思（reflection）把零散观察合成更高阶的判断，再参与规划。今天几乎所有「该记什么」的打分都能追溯到这篇。[arXiv:2304.03442](https://arxiv.org/abs/2304.03442)

**MemGPT 把问题定义成「虚拟上下文管理」。** 它的类比对象是操作系统的分层内存：主上下文像内存，外部存储像磁盘，模型通过函数调用在两处搬运数据，用 interrupts 在自身与用户之间交接控制流。这篇论文的读者价值不在具体实现，而在它把「上下文是稀缺资源、要显式调度」这件事讲成了架构问题。[arXiv:2310.08560](https://arxiv.org/abs/2310.08560)

**CoALA 是词汇表的来源。** 它把语言 agent 的记忆按认知科学分成工作记忆、情景记忆、语义记忆、程序性记忆，此后的大量「记忆类型」设计——包括 MIRIX 的六类——都是这套词汇的变体。[arXiv:2309.02427](https://arxiv.org/abs/2309.02427)

**Reflexion、Voyager、ExpeL 构成「经验派」的三种写法。** Reflexion 存的是关于失败的自然语言反思，下一次 trial 读回，不改权重；Voyager 存的是可执行代码，技能必须通过自我验证才能入库，因此「记忆」天然是可运行、可复用的；ExpeL 从轨迹里抽 insight 汇成池子再注入提示，同样免微调。[arXiv:2303.11366](https://arxiv.org/abs/2303.11366)、[arXiv:2305.16291](https://arxiv.org/abs/2305.16291)、[arXiv:2308.10144](https://arxiv.org/abs/2308.10144)

### 用户画像与会话记忆：最贴近「贴合用户习性」的一条线

**MemoryBank 是这条线上最早成形的作品。** 它明确面向长期陪伴与心理咨询这类「用户是谁很重要」的场景，让模型从过往交互里综合出用户性格，并用艾宾浩斯遗忘曲线来决定哪些记忆随时间淡出、哪些因反复出现而强化。遗忘在这里不是清理垃圾，而是模拟人类的记忆衰减。[arXiv:2305.10250](https://arxiv.org/abs/2305.10250)

**SeCom 提供了关于粒度最干净的一组实验结论。** 它的两条发现是：一、记忆单元的粒度（turn / session / 摘要）本身就决定了检索准确率与召回内容的语义质量；二、提示压缩方法（如 LLMLingua-2）可以当好用的去噪器，跨粒度提升检索准确率。它自己的方案是用一个分段模型把长期对话切成话题连贯的 segment 来建记忆库。如果你只能读一篇关于「记忆怎么切」的论文，就是这篇。[arXiv:2502.05589](https://arxiv.org/abs/2502.05589)

**PersonaMem-v2 把「隐式偏好」做成了可测的东西。** 数据规模是 1000 段真实感交互、300+ 场景、20000+ 偏好、128k 上下文，关键在于「多数偏好是隐含表露的」——而这一点让前沿模型只有 37–48% 的准确率，说明瓶颈在推理而非上下文长度。它的两个结论对工程直接有用：强化微调能把 Qwen3-4B 推到 53%（论文自报超过 GPT-5）；它的 agentic memory 框架用一条 2k token 的人类可读记忆替代 32k 完整对话，取得 55% 的最高准确率且输入 token 少 16 倍。这是本次调研里「小记忆 + 好推理」胜过「大上下文」最直接的一份证据。[arXiv:2512.06688](https://arxiv.org/abs/2512.06688)

**Think-in-Memory 针对的是一个很具体的病：重复推理不一致。** 同一段历史为不同问题被反复回忆，会得到互相矛盾的结论。它的做法是把历史 thoughts 留在记忆里，生成前 recall、生成后 post-think，让记忆承载「已经想清楚的结论」而不是每次都重推。[arXiv:2311.08719](https://arxiv.org/abs/2311.08719)

**MIRIX 与 MemInsight 代表两种「让记忆更结构化」的路线。** MIRIX 走的是分类 + 多 agent：六类记忆各司其职，由多个 agent 协调更新与检索，并且把视觉与多模态纳入进来。MemInsight 走的是自动增广：让 agent 自主为历史交互挖出属性来做语义结构化，在 LLM-REDIAL 上把推荐说服力提升最多 14%。[arXiv:2507.07957](https://arxiv.org/abs/2507.07957)、[arXiv:2503.21760](https://arxiv.org/abs/2503.21760)

### 检索与结构：图赢了多跳，但要看住事实

**HippoRAG 的贡献是「用一个随机游走替代迭代检索」。** 它用 LLM 建开放知识图谱，再用 Personalized PageRank 做单步检索，相当于让图结构承担多跳推理的路径搜索。结果是在多跳问答上比当时最好方法高至多 20%，同时单步检索比 IRCoT 便宜 10–30 倍、快 6–13 倍。[arXiv:2405.14831](https://arxiv.org/abs/2405.14831)

**HippoRAG 2 的价值主要在于它承认了前一版的代价。** 出发点是：加了图结构的 RAG 在「更基础的事实记忆任务」上反而明显低于标准 RAG（原文称 unintended deterioration）。HippoRAG 2 的目标是同时在事实记忆、意义建构、联想记忆三类任务上全面不输标准 RAG。凡是打算给记忆加图的人，都应该先读这一篇的动机段。[arXiv:2502.14802](https://arxiv.org/abs/2502.14802)

**Zep 把它做成了服务，并挑明了企业场景的需求。** 核心组件 Graphiti 是一个时间感知的知识图引擎，同时吸收非结构化对话与结构化业务数据，并保留历史关系。它的评测叙事很有策略性：在 MemGPT 团队自己确立为首要指标的 Deep Memory Retrieval 上超过 MemGPT，再说明 DMR 不足以反映真实企业用例。[arXiv:2501.13956](https://arxiv.org/abs/2501.13956)

**A-MEM 与 Mem0 是「通用记忆层」的两种答案。** A-MEM 借 Zettelkasten 方法：新记忆入库时生成含上下文描述、关键词、标签的结构化笔记，并主动分析历史记忆建立链接；更进一步，新记忆会触发旧记忆属性与上下文的更新，作者称之为 memory evolution。Mem0 则从生产可用性出发，动态提取、合并、检索对话中的显著信息，另有一个图表示变体来捕捉关系结构，在 LOCOMO 上与六类基线（含全上下文和不同 chunk、不同 k 的 RAG）对比。[arXiv:2502.12110](https://arxiv.org/abs/2502.12110)、[arXiv:2504.19413](https://arxiv.org/abs/2504.19413)

**另外三篇值得知道边界在哪。** EM-LLM 用 Bayesian surprise 加图论边界细化在线切出「事件」，再用相似度 + 时间连续性两阶段检索，目标是不微调也能处理近似无限上下文；Larimar 把知识更新做成分布式事件记忆的写入，单次更新即可生效，是「记忆当参数改写」这条路的代表；MemoRAG 先用全局记忆产生线索，再用线索引导精确定位，是长上下文策略而非长期记忆策略。[arXiv:2407.09450](https://arxiv.org/abs/2407.09450)、[arXiv:2403.11901](https://arxiv.org/abs/2403.11901)、[arXiv:2409.05591](https://arxiv.org/abs/2409.05591)

### 记忆管理本身变成学习问题

**MemoryOS 是「工程派」的极致。** 三层存储（短期、中期、长期个人记忆）对应四种模块（存储、更新、检索、生成）；短期到中期按对话链 FIFO 迁移，中期到长期用分段页组织。报告在 LoCoMo 上 GPT-4o-mini 平均 F1 提升 49.11%、BLEU-1 提升 46.18%。这套设计的价值是它把「迁移」写成了显式规则——可以直接对着实现，代价是规则是人定的。[arXiv:2506.06326](https://arxiv.org/abs/2506.06326)

**Memory-R1 与 Memp 则把规则换成学出来的策略。** Memory-R1 训练两个 agent：Memory Manager 学 ADD / UPDATE / DELETE / NOOP 四种结构化操作，Answer Agent 先筛选相关条目再推理，两者都用结果驱动的 RL（PPO、GRPO）微调，且只需要 152 条训练问答。Memp 关注程序性记忆：把过往轨迹蒸成步骤级指令与脚本级抽象两种粒度，系统研究 Build、Retrieval、Update 各自策略的影响，并配一套持续更正与废弃内容的动态机制，在 TravelPlanner 与 ALFWorld 上随记忆库精炼而稳定提升。[arXiv:2508.19828](https://arxiv.org/abs/2508.19828)、[arXiv:2508.06433](https://arxiv.org/abs/2508.06433)

**ACE 和 Dynamic Cheatsheet 是低成本那一档。** ACE 针对「简洁偏置」——整段重写会让细节丢失——改成增量 delta 演化上下文条目；Dynamic Cheatsheet 更轻，让黑盒 LM 跨样本积累一份持久 cheatsheet，不需要真值标签也不需要训练。[arXiv:2510.04618](https://arxiv.org/abs/2510.04618)、[arXiv:2504.07952](https://arxiv.org/abs/2504.07952)

### 评测：从闲聊记忆到工作经验

**LoCoMo 是这条评测线的起点**，它评估的是模型在超长期开放域对话里的表现，并指出此前的工作大多只考察不超过五个会话的上下文。[arXiv:2402.17753](https://arxiv.org/abs/2402.17753)

**LongMemEval 把它变成可诊断的基准**，明确针对聊天助手在持续交互中的长期记忆能力（ICLR 2025）。[arXiv:2410.10813](https://arxiv.org/abs/2410.10813)

**LongMemEval-V2 在 2026 年 5 月把评测对象换掉了：从「用户的闲聊史」换成「专业 web 环境里的工作经验」**——界面可供性、状态动态、工作流、反复出现的失败模式。这个转向值得注意：它说明「记忆要有用」的标准正在从「像人一样记得」变成「像熟练同事一样知道该怎么做」。[arXiv:2605.12493](https://arxiv.org/abs/2605.12493)

**另外两篇评测工作补了两个维度。** Harness the Memory 做的是受控对比：不同的记忆基质（memory substrate，即记忆用什么媒介表示和存储）在不同运行条件下各自适合什么，之前缺乏这类指导。What Eviction Destroys 提出 restore counterfactual，把「驱逐导致的不可逆损失」与「可恢复的检索失败」分开计量——现有预算-准确率曲线把这两者混为一谈。[arXiv:2608.15008](https://arxiv.org/abs/2608.15008)、[arXiv:2609.08279](https://arxiv.org/abs/2609.08279)

### 综述、立场与安全

**四份综述/立场论文各管一段。** Zhang 等人的综述（ACM TOIS 2025）是「记忆机制」的标准文献回顾入口；From Human Memory to AI Memory 从「编码-存储-检索」的人类记忆框架映射到 LLM 记忆；From Storage to Experience（ACL 2026 Findings）的摘要把当前研究的状态概括为「碎片化，在操作系统工程与……之间摇摆」，主张记忆研究的重心应从存储转向经验；Position: Episodic Memory is the Missing Piece 则提出情景记忆的五个关键属性并给出路线图。[arXiv:2404.13501](https://arxiv.org/abs/2404.13501)、[arXiv:2504.15965](https://arxiv.org/abs/2504.15965)、[arXiv:2605.06716](https://arxiv.org/abs/2605.06716)、[arXiv:2502.06975](https://arxiv.org/abs/2502.06975)

**最锋利的一篇是 Contextual Agentic Memory is a Memo, Not True Memory。** 它的论点：向量库、RAG、草稿纸、上下文管理实现的都是「查表」而不是记忆；把两者混同是一个范畴错误，后果可证——按相似度泛化与按抽象规则泛化是两种不同的能力，混同会让 agent 无限积累笔记却长不出专长，在组合新颖任务上存在任何上下文长度或检索质量都无法突破的泛化上限，并且结构性地易受持久记忆投毒。它援引神经科学的互补学习系统理论指出生物智能的解法是「快慢两套系统配对」。对 Jarvis 的直接含义：**如果只做检索式记忆，就不要声称它是学习**。[arXiv:2604.27707](https://arxiv.org/abs/2604.27707)

**安全线已经独立成篇。** Memory Poisoning Attack and Defense on Memory Based LLM-Agents 研究的是仅通过查询交互注入、污染长期记忆并影响后续回答的攻击（承接 MINJA 一线）及其防御；A Survey on Long-Term Memory Security in LLM Agents 把威胁面归纳为三性——持久性、有状态性、传播性——并按记忆生命周期组织攻击、防御与治理。[arXiv:2601.05504](https://arxiv.org/abs/2601.05504)、[arXiv:2604.16548](https://arxiv.org/abs/2604.16548)

### 2026 年的散点

| 论文 | 日期 | 一句话 |
| --- | --- | --- |
| [GAM](https://arxiv.org/abs/2604.12285) | 2026-04 | 分层图式 agentic memory，针对「统一流式记忆易被瞬态噪声干扰」的问题 |
| [MAGMA](https://doi.org/10.18653/v1/2026.acl-long.1709) | ACL 2026 | 多图（multi-graph）架构的 agentic memory |
| [Omni-SimpleMem](https://arxiv.org/abs/2604.01007) | 2026-04 | 用自动化研究流程搜索多模态终身记忆的设计空间 |
| [PersonaMem-v2](https://arxiv.org/abs/2512.06688) | 2025-12 | 见上文；隐式偏好基准 + 单条可读记忆 |
| [Personalized Benchmarking](https://arxiv.org/abs/2604.18943) | 2026-04 | 现有基准把偏好跨用户平均掉了，改为按个体偏好评测 |
| [PSPA-Bench](https://arxiv.org/abs/2603.29318) | 2026-03 | 手机 GUI agent 的个性化基准，基于真实个人使用习惯 |
| [From Storage to Experience](https://arxiv.org/abs/2605.06716) | 2026-05 | ACL 2026 Findings 综述，主张从存储转向经验 |

## 与 Jarvis 的接口点

沿用开源侧那份文件的写法：只列「如果要做跨会话记忆绕不开的决策，以及论文里各自给出的选项」，不构成推荐。Jarvis 现状是只有单会话轨迹持久化与压缩 checkpoint，跨会话记忆被 ADR 0001/0002 明确推迟。

| 决策 | 论文侧可参照的工作 |
| --- | --- |
| 记忆单元粒度 | SeCom 的粒度对照实验；SimpleMem 的多视图压缩单元；MemoryBank 的记忆条目 + 画像 |
| 写入时机 | Generative Agents / Think-in-Memory 的每轮追加；Reflexion、ExpeL、Memp 的任务后沉淀；PersonaMem-v2 的训练式写入 |
| 谁决定记住什么 | Memory-R1 的 RL 操作策略；MemoryOS 的 FIFO 与分段页规则；MemoryBank 的遗忘曲线阈值 |
| 用户画像如何形成 | MemoryBank 的画像综合；PersonaMem-v2 的隐式偏好；Second Me 的个人记忆 |
| 检索 | HippoRAG 的 PPR 单步检索；Zep 的时序图；A-MEM 的链接扩展；MemoRAG 的线索引导 |
| 驱逐与遗忘 | MemoryBank 的遗忘曲线；MemoryOS 的分层迁移；What Eviction Destroys 的驱逐审计 |
| 评测 | LoCoMo、LongMemEval（含 V2）、PersonaMem-v2、Harness the Memory |
| 风险 | 记忆投毒攻击与防御；长时记忆安全综述的三性框架；Memo-not-True-Memory 的能力上限论证 |

有三处与 Jarvis 的既有决策直接相关：

**一是「单一人类可读记忆」在论文侧有了量化支持。** PersonaMem-v2 用 2k token 的可读记忆替代 32k 完整对话，报告输入 token 少 16 倍且准确率最高。这与 Jarvis 现有的「会话记录是事件日志、归档原文可回查」取向相容，也是纯文件方案在论文侧最接近的一次背书。

**二是「检索式记忆的泛化上限」这条论证值得在写 ADR 前先处理。** 如果最终方案是「把提炼出的事实存成条目 + 按需检索」，那么按 Memo-not-True-Memory 的说法，它解决的问题是查得准，不是学得会。这不影响先做它，但决定了术语：把它叫记忆还是叫索引，会影响后面能不能在此基础上加学习层。

**三是评测在自建 harness 上，所以 Jarvis 需要自己的最小判定集。** 论文侧的 LoCoMo / LongMemEval / PersonaMem-v2 都需要特定数据集与指标口径；对一个个人助手来说，更现实的是先定几条可复现的场景（新开会话后应该多知道什么），这正好也是决策层最先要回答的那个问题。

## 学习路径建议

如果目标是先看懂这个领域而不是选型，建议按这个顺序读六篇：

1. **Generative Agents 与 MemGPT**：一篇定义「记忆流 + 打分召回 + 反思」，一篇定义「记忆是上下文调度问题」。这两个起点覆盖了后来几乎所有设计。
2. **CoALA**：拿到词汇表。之后读任何一篇记忆论文，先看它说的「记忆」落在 working / episodic / semantic / procedural 的哪一格。
3. **SeCom**：关于记忆粒度唯一一组干净的对照实验，读完能判断「切多细」这件事。
4. **HippoRAG 2 的动机段 + Zep**：一个讲清楚加结构的代价，一个讲清楚时序图能做到什么。
5. **PersonaMem-v2**：直接对应你想做的事情，并且是唯一给出「隐式偏好」可测口径的工作。
6. **Contextual Agentic Memory is a Memo, Not True Memory**：作为反方观点收尾，避免把检索当记忆。

## 未核实与局限

- 全部描述来自各论文的 arXiv 摘要、arXiv comment 字段与 OpenAlex 元数据；**本次未通读任何一篇全文**，因此论文内部的实现细节、消融实验与失败案例均不在本文件覆盖范围内。
- 所有性能数字（+20%、+49.11%、+26.4%、53%、55%、16 倍等）都是论文自报，未复现，且在不同 harness、不同模型、不同检索预算下不可横向比较。PersonaMem-v2 与 Qwen3-4B 超过 GPT-5 的说法是原文表述。
- 引用量来自 OpenAlex 2026-09-12 快照。该库同时收录 arXiv 预印本、会议论文集与 Zenodo 等来源，本次检索中出现过若干来源可疑或与本主题弱相关的高引用条目，本文件已按「是否有明确会议/期刊场所或 arXiv 条目」筛选，但仍可能存在遗漏。
- 场所字段只在 arXiv comment 或 OpenAlex 记录能核对到时才标注；未标注的（MemGPT、Reflexion、MemoryOS、SimpleMem 等）表示本次未核实其正式发表场所，不代表没有。
- 本次没有覆盖：参数化记忆与知识编辑的更大分支（Larimar 只作为代表点提及）、推荐系统与角色扮演方向的个性化工作、以及多模态记忆除 MIRIX 与 Omni-SimpleMem 之外的部分。
- 2026 年的部分条目（4 月至 9 月）非常新，其中若干为工作稿或尚未通过同行评审（例如 LongMemEval-V2 标注为 Work in Progress），采用前应自行确认版本。
