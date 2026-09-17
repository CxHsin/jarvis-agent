# Agent 记忆系统：商业产品调研

研究日期：2026-09-12。依据只有官方帮助中心、官方文档与官方发布说明；**未逆向任何客户端、未采集用户数据、未做产品实测**。下面每条描述都是对官方页面的转述，链接指向该页面。部分站点（OpenAI 帮助中心、openai.com）对普通 UA 返回 403，抓取时改用浏览器 UA；抓不到正文的条目标注为未核实。

同批调研的另两份：开源实现见 `docs/research/agent-memory-oss-survey.md`，论文与基准见 `docs/research/agent-memory-papers-survey.md` 与 `docs/research/agent-memory-papers-survey-arxiv-verified.md`。本文件只覆盖商业产品。

## 结论

七个产品在这五个维度上的选择几乎全不相同。唯一真正的共识是"记忆必须对用户可见、可关、可删"——但"可见"的实现程度差了一个数量级。

| 产品 | 记忆怎么产生 | 用户能看到什么 | 作用域 | 开关 |
| --- | --- | --- | --- | --- |
| [ChatGPT](https://help.openai.com/en/articles/8590148-memory-faq) | 自动提炼（聊天、文件、连接的应用）+ 显式要求记住；自定义指令是**独立机制** | 一张会自动更新的**记忆摘要**（不是条目列表）+ 回答下方的**来源清单** | 账号全局；受监管工作区可开项目级隔离 | 总开关，外加 saved memories / chat history 两个独立开关；Temporary Chat 不读不写 |
| [Claude](https://www.anthropic.com/news/memory) | 自动（官方定位于专业上下文与工作模式）+ 可要求忽略某类内容 | 设置里的 **memory summary**，可查看、可通过对话更新 | **项目级隔离**：每个 project 一份独立记忆 | 功能完全可选；Incognito chat 不写记忆；企业管理员可对整个组织关闭 |
| [Gemini](https://support.google.com/gemini/answer/16598469) | 自动，基于过去聊天与已连接的应用 | **没有可枚举的列表**；只能问它"你用过去聊天里的信息了吗" | 账号级；Gems 与 Live chats 不适用 | Settings & help → Personal Intelligence → Memory；需 18+、个人账号、Keep Activity 开启 |
| [Microsoft Copilot](https://support.microsoft.com/en-us/microsoft-365-copilot/manage-copilot-memory-in-microsoft-365-copilot) | 自动（默认开启，遇到它认为重要的信息**先问你要不要保存**）+ 显式要求记住 | Chat settings → Personalization → **Saved memories** 列表 | 账号级（Microsoft 365 账号） | 可关闭保存能力；可逐条删除 |
| [GitHub Copilot](https://docs.github.com/en/copilot/concepts/agents/copilot-memory) | 自动，分**仓库级事实**与**用户级偏好**两类，每条都带引用 | 个人设置里可看自己的偏好；仓库 owner 可看该仓库的事实 | 仓库级与用户级**双重隔离**，按计费实体归属 | 按用户而非按仓库开启；个人计划默认开，企业需管理员先开策略 |
| [Cursor](https://cursor.com/docs/rules) | **不自动记忆**。持久上下文由用户写的 Rules 与 Skills 承担 | `.cursor/rules/*.mdc` 文件在仓库里，可版本控制、可评审 | 项目级 / 用户级 / 团队级 | 没有"记忆开关"这个概念，因为记忆不是自动学来的 |
| [Windsurf / Devin](https://docs.windsurf.com/windsurf/cascade/memories) | Memories 由 Cascade 自动生成；Rules 由用户手写 | 提供记忆管理页 | global / workspace / system 三级 | 新的 Devin Local agent **不持久化记忆**，官方建议迁移到 skills |

## 逐产品证据

### ChatGPT：综合摘要 + 回答级溯源

OpenAI 现在把记忆拆成两条路：**saved memories**（你直接告诉它要记住的，或它判断值得留的细节）与 **chat history**（从过去所有对话里提炼的见解），两者可以分别关闭。这个二分法写在 2025-04-10 的更新说明里；2025-06-03 起免费用户拿到的是"轻量版记忆"，只提供跨会话的短期连续性，Plus/Pro 才是"对用户的长期理解"。[官方发布说明](https://openai.com/index/memory-and-new-controls-for-chatgpt/)

时间线（同一页面与[发布日志](https://help.openai.com/en/articles/6825453-chatgpt-conversation-history)）：2024-02-13 上线记忆与临时聊天；2024-09-05 开放给 Free 用户，并开始提示 "Memory updated"，可悬停后进入 "Manage memories" 查看；2026-01-15 起，开启 chat history 后 ChatGPT 能更可靠地找到过去对话里的具体细节，**任何被用来回答的过去对话都会作为来源出现，可以点开核对原文**。

用户看到的主体是一张**记忆摘要**，而不是条目清单：它自动更新、顶部标出最近更新时间；可以直接在底部输入框写"把 X 改成 Y"整段更新，也可以**高亮摘要里任意一段做定点修正**。官方明说这张摘要"不会包含 ChatGPT 记住的全部内容"，因为记忆是对过去上下文的持续综合，比摘要能展示的更宽。[Memory FAQ](https://help.openai.com/en/articles/8590148-memory-faq)

溯源做在回答层：回答下方的书签图标会列出这次个性化用到的来源（自定义指令、过去聊天、文件、记忆），点某条记忆会解释"为什么用了它"，并可从三点菜单直接纠正。

删除是这套设计里最别扭的地方，官方也承认：要彻底删掉某个信息，必须删掉它出现过的**每一个来源**——过去聊天、归档聊天、文件、记忆摘要、以及断开可能含有该信息的外部应用。记忆摘要页面提供 "Delete and turn off memory"，但这不删除聊天记录；如果之后重新打开记忆，ChatGPT 可能**从还在的旧聊天里重新生成记忆**。legacy 的 saved memories 另有一套语义：它存在一个与聊天历史分离的"notepad"里，删聊天不会删记忆；已被删除的 saved memories 可能被保留日志**最长 30 天**，用于安全与调试。Temporary Chat 则不读取也不写入记忆。

企业侧：受监管工作区（如 ChatGPT for Healthcare）里 improved memory 默认关闭，需管理员放开，且**不在 BAA 覆盖范围内**；成员可启用"项目专属记忆"，把记忆的引用范围限制在单个项目内。

### Claude：项目级隔离 + 摘要

Claude 的记忆 2025-09-11 先给 Team 与 Enterprise，2025-10-23 扩到 Pro 与 Max。官方把它定位为"学习你的专业上下文与工作模式"——团队流程、客户需求、项目细节、优先级——也就是说它明确做的是**工作场景的用户建模**，而不是生活闲聊。控制项是"完全可选 + 细粒度"。如果用 Projects，**每个项目会生成一份彼此独立的记忆**，官方把这称为隔离敏感对话的"安全护栏"。[Bringing memory to Claude](https://www.anthropic.com/news/memory)

与 ChatGPT 一样，Claude 也用一个 **memory summary** 承载"它记得什么"，可以在设置里查看，并通过对话让它更新；你可以告诉它要关注或忽略什么。Incognito chat 提供"不写入、不读取记忆"的会话；企业管理员可以随时为整个组织关闭记忆。官方还提到支持**从其他 AI 工具导入记忆、以及导出自己的记忆**用于备份或迁移。

值得单独记一笔的是官方对安全的态度：上线前他们针对"记忆会不会强化有害模式、导致过度迎合（over-accommodation）、或帮助绕过安全措施"做了测试，并因此"对记忆的工作方式做了针对性调整"。这说明记忆带来的不只是召回问题，还有**迎合问题**。

Claude Projects（2024-06-25 上线）是另一条线：把相关文档、代码、访谈记录等组织进一个项目，让 Claude 不必每次从零开始，并支持每个项目单独的自定义指令。项目知识与记忆是两回事：前者是你放进去的资料，后者是它记住的关于你的事实。[Projects 发布说明](https://www.anthropic.com/news/projects)

### Gemini：靠"删除一切来源"来治理

Gemini 的记忆建立在对过去聊天的引用上，使用门槛写得非常明确：年满 18 岁、用个人 Google 账号（工作、学校、受监管账号不可用）、并且 **Keep Activity 必须开启**。范围仅限 Gemini 移动 App、Web、Chrome 里的 Gemini 和手表；Gems 与 Live chats 不适用。[官方帮助](https://support.google.com/gemini/answer/16598469)

它没有可见的记忆列表：想知道它是否用过过去聊天，官方给的办法是**直接问它"你用过去聊天里的信息了吗"**。纠正也是对话式的——开启 Memory 后直接在聊天里纠正它。删除则必须回到 Activity 里删掉**包含该信息的全部聊天**，而且删除后"可能有一小段延迟"才停止使用。

连接应用让这件事更复杂，官方明确要求**两步都做**：既要断开该应用的连接，又要删掉含该信息的聊天。只断开，信息可能还留在过去聊天里；只删聊天，它还可能从还连着的应用里再读到。应用内的数据被删除或更新后，Gemini 这边的体验"可能要几天后"才变化。

### Microsoft Copilot：默认开启，但写入前会问

Copilot 的保存记忆**默认开启**。它的行为里有一条少见的设计：当 Copilot 认为某条信息重要时，它会**先问你要不要保存到未来的对话里**，而不是直接写；你也可以显式让它记住某件事。写完之后聊天界面会显示 **"Memory updated"** 提示。它会把保存的记忆存在 Settings → Chat settings → Personalization → **Saved memories** 里，并自称会"智能地合并相关记忆、更新过时细节、或按要求删除"。[Manage Copilot Memory](https://support.microsoft.com/en-us/microsoft-365-copilot/manage-copilot-memory-in-microsoft-365-copilot)

另一个页面补充了一条容易踩坑的行为：如果把"基于聊天历史的个性化"关掉并删除记忆，**在 30 天内重新打开这个设置，Copilot 会把删掉的记忆从聊天历史里再加回来**。[Personalize what Copilot remembers](https://support.microsoft.com/en-us/microsoft-365-copilot/personalize-what-microsoft-365-copilot-remembers)

### GitHub Copilot：两类记忆、带引用、会过期

GitHub Copilot Memory 是本次调研里工程约束写得最清楚的一个。它把记忆分成两类，可见性与删除权各不相同：

- **仓库级事实**：只有对该仓库有写权限、且自己开启了 Copilot Memory 的用户的操作才会产生；产生后对仓库内有权限的人共享，但**只能在该仓库使用**；仓库 owner 可以查看并手工删除。
- **用户级偏好**：只对本人可见，捕获个人的编码风格与工作流习惯；用户可随时查看和删除自己的偏好。企业版管理员还可以导出或批量删除，并且记忆**归属到计费实体**——生成上下文时只取当前 active billing entity 名下的记忆，多许可的用户必须先在账号设置里选一个默认计费实体。

每条记忆都**带引用**。真正使用前，Copilot 会拿这条引用**回到当前分支校验信息是否仍然成立，只用通过校验的事实**；偏好则靠"自己判断是否仍然适用"。过期由机制保证而不是靠用户发现：**任何 28 天未被使用的条目会被自动删除**，成功校验并使用会重置这个计时。它甚至允许从"已关闭但未合并"的 PR 里捕获事实，前提是当前代码库仍然支持那条信息。

开关粒度是按用户而不是按仓库：个人计划默认开启，企业或组织管理的计划需要管理员先开策略，然后用户可以选择退出。[About GitHub Copilot Memory](https://docs.github.com/en/copilot/concepts/agents/copilot-memory)

### Cursor：明确拒绝"自动记忆"

Cursor 官方文档里有一句立场非常鲜明的话：**"Large language models don't retain memory between completions. Rules provide persistent, reusable context at the prompt level."** 也就是说，它不假装模型有记忆，而是让用户把持久上下文写成规则，在应用时放在模型上下文的开头。[Cursor Rules 文档](https://cursor.com/docs/rules)

规则分四类：Project Rules（存在 `.cursor/rules` 下的 `.mdc` 文件里，进版本控制，可用 glob 限定作用范围）、User Rules（对使用者的 Cursor 环境全局生效）、Team Rules（在 dashboard 里管理，Team/Enterprise 可用）、以及 AGENTS.md（纯 Markdown，零配置的简化替代）。文档还特别提醒：放在 `.cursor/rules` 里的普通 `.md` 会被忽略，因为它没有 frontmatter 来描述 `description`、`globs` 和 `alwaysApply`。

### Windsurf / Devin：把记忆与规则分开，并承认记忆会过时

Windsurf（现属 Devin Desktop）把跨会话上下文拆成两套：**Memories** 由 Cascade 在对话中自动生成、在相关时自动检索；**Rules** 由用户手写，分 global / workspace / system 三级，激活方式有 `always_on`、`glob`、`model_decision`、`manual` 四种。[Memories & Rules 文档](https://docs.windsurf.com/windsurf/cascade/memories)

官方给出的选用建议本身就很说明问题：**一次性事实用 Memories，持久知识优先用 Rules 或 AGENTS.md**。同时还标注了两条限制：Memories 只适用于 legacy 的 Cascade agent，新的 Devin Local agent 不持久化记忆，官方建议把依赖的记忆迁移成 skills。

## 产品设计上的共识与分歧

**共识**

1. **记忆必须可见、可关、可删**，七个产品没有一个例外，区别只在"可见"做到什么程度。
2. **读取与写入经常分开控制**。ChatGPT 把 saved memories 与 chat history 拆成两个开关；ChatGPT 的 Temporary Chat 与 Claude 的 Incognito chat 都是"不读也不写"的会话模式。
3. **删除被当成"删干净"问题处理，而不是"删一条"**。ChatGPT 要求删掉信息出现过的每一个来源；Gemini 要求删聊天并断开应用两步都做。两家都明确写出"删了这条记录，历史对话里可能还留着"。
4. **写入要能纠正，而不只是能删**。ChatGPT 支持高亮摘要局部改写，Gemini 与 Claude 都支持用对话纠正。

**分歧**

1. **记忆单元：条目 vs 摘要 vs 不透明**。Microsoft Copilot、GitHub Copilot 与 legacy ChatGPT 是可逐条查看的列表；ChatGPT 的新记忆与 Claude 是**综合摘要**，官方自己说摘要不等于全部；Gemini 干脆没有列表，只能通过提问确认。
2. **作用域：全局 vs 隔离**。ChatGPT 与 Gemini、Microsoft Copilot 是账号级；Claude 默认项目级隔离，ChatGPT 在受监管工作区提供项目级隔离；GitHub Copilot 同时做仓库级与用户级两道隔离，并额外用"计费实体"决定检索范围。
3. **自动提炼 vs 用户手写**。Cursor 明确站在"不自动记忆、由用户写规则"一侧，Windsurf 也建议持久知识优先用 Rules 或 AGENTS.md；其余产品都走自动提炼 + 事后管理。
4. **新鲜度**。只有 GitHub Copilot 把"过期"做成机制：28 天未使用自动删除，且每次使用前回到当前分支校验引用。其余产品把过期交给用户自己发现。
5. **写入时机**。Microsoft Copilot 明确会在写入前征询用户；ChatGPT、Claude、Gemini 都是先写、事后可改。

## 官方自己承认的问题

这一节只用官方文档里写出来的失效模式，比第三方抱怨更硬：

- **旧机制会变味**。OpenAI 这样描述被替换掉的 saved memories："often became stale and relied on users to manually manage updates. Memories could also contradict one another"，并举了"我在为马拉松训练"与"我扭伤了脚踝"两条互相矛盾的记忆作为例子。这等于承认自动记忆的典型失效是过期与自相矛盾。
- **摘要不等于全部**。ChatGPT 明说记忆摘要不会包含它记住的所有内容；Claude 同样用一个 summary 概括，用户能核对的粒度受限于这张摘要。
- **删除不彻底**。ChatGPT 要删掉信息出现过的全部来源；Gemini 要删光相关聊天并断开连接的应用；两家都提示删除可能有延迟或被历史重新带回。
- **删除后会被重新写回**。Microsoft Copilot 明确写了 30 天内重新开启聊天历史个性化会把删掉的记忆加回来；ChatGPT 也提示重新开启记忆后可能从旧聊天重新生成记忆。
- **记忆会强化迎合**。Anthropic 自述安全测试覆盖了"记忆是否导致 over-accommodation（过度迎合）以及回避拒绝"的问题，并因此调整了记忆的行为。

## 交互设计可借鉴的具体点

1. **写入要有即时反馈**。ChatGPT 从 2024-09 起显示 "Memory updated"，Microsoft Copilot 也显示 "Memory updated"。让用户知道"刚刚发生了一次写入"是这套设计里最便宜、收益最直接的一条。
2. **把溯源做在回答层，而不是只做在设置页**。ChatGPT 回答下方的来源清单会列出这次用到了哪些记忆、哪段过去聊天，并能解释"为什么用它"。这与 Jarvis 既有的证据引用、原文可核对是同一件事，落到 CLI 上就是"这次回答引用了哪几条记忆"。
3. **局部纠正优于整块重写**。ChatGPT 允许高亮摘要里任意一段做定点修正，而不是让你整份重写。记忆中一条事实写错时，定点改的成本远低于重写。
4. **写入前询问**。Microsoft Copilot 的"发现重要信息 → 问你要不要保存"是唯一明确采用确认制的产品，也是最贵的交互；对 Jarvis 这种单用户 CLI 来说，可以退化成"写入后告知 + 一条撤销命令"。
5. **不读不写的会话模式**。Temporary Chat / Incognito chat 提供了一次性的干净上下文，实现成本极低（一个开关），但对隐私与调试的价值很高。
6. **过期机制要有默认值**。GitHub Copilot 的"28 天未使用自动删除 + 使用前校验引用"表明：光靠用户清理是不够的，系统自己要有淘汰策略。
7. **按来源分类存储**。GitHub Copilot 把"仓库事实"与"用户偏好"分开存、分开授权、分开删除，这个划分方式比按内容主题分类更能回答"这条记忆该给谁看、该在哪个范围生效"。

## 对 Jarvis 的可迁移结论

前提：Jarvis 是单用户、纯 CLI、纯 Python 的个人助手，目前只有会话内的事件日志（JSONL 会话记录）与压缩 checkpoint，跨会话记忆被 ADR 明确推迟；团队倾向不引入新依赖、人类可读可手改的文件优先；已有"证据引用 / 原文可核对"的价值观。

**值得照搬的**

- **写入反馈 + 回答级溯源**。这是所有产品里投入产出最高的一对交互，而且 Jarvis 有天然优势：会话记录本身就是事件日志，每条记忆可以指向 `session_id` + 消息序号，做到 ChatGPT 那种"点开回到原文"，成本比它低得多。
- **定点纠正与显式删除/撤销**。记忆写错比忘掉更贵，给一条改一条、给一条删一条，比提供一份"记忆中转站"摘要更简单也更可核对。
- **不读不写的会话模式**。CLI 上加一个 `--no-memory` 之类的一次性开关，成本几乎为零。
- **条目式而不是摘要式**。ChatGPT 与 Claude 都用一个综合摘要承载记忆，两家官方都承认摘要不等于全部；条目 + 来源既能定点改，也能回答"这条是从哪来的"。这与开源侧 Markdown-as-truth 一派的结论一致，也是本项目"原文可核对"价值观的自然延伸。
- **过期/淘汰要有默认值**。可借鉴 GitHub Copilot 的"长期未使用即淘汰 + 使用前校验引用"：会话记录存在时，记忆可以很轻——只存指针与提炼结论，原文永远回会话记录里读。

**可以省掉的**

- 多租户与计费实体归属（GitHub Copilot 为企业场景付的复杂度成本）。
- 组织管理员的批量导出/删除、企业策略开关（Microsoft Copilot、Claude、ChatGPT 的企业侧设计）。
- 项目级隔离：Jarvis 当前只有单工作区，作用域问题退化成一个很小的选择。

**要一开始就想清楚的**

- **删除的语义边界**。所有产品都在"删了这条记忆，历史里还留着"上打补丁。Jarvis 如果让记忆指向会话记录，就必须明确：删记忆条目只删条目，还是连带删会话？这是两个动作，不能含糊。
- **读取路径不能破坏稳定前缀**。这些产品普遍把记忆打包进每轮上下文（Claude 的项目记忆、ChatGPT 的 saved memories 都是"always considered"）。Jarvis 的 ADR 0001 建立在稳定消息前缀上，把记忆做成按需检索的工具，比每次注入更贴合现有架构。
- **验收场景要选对**。本次调研里没有一个产品把"记住更多"当目标，它们的目标分别是"少让你重复交代"（ChatGPT、Claude）和"下次做事别犯同样的错"（GitHub Copilot）。Jarvis 应该先写清楚自己的判定场景，再决定记什么。

## 未核实与局限

- **国内产品未覆盖**：通义、Kimi、豆包的官方记忆说明本次未抓取到可引用的官方页面，故跳过，不做推测性描述。
- **Cursor 的记忆状态存疑**：官方文档索引（`https://cursor.com/docs/llms.txt`）里只有 rules 与 skills，没有 memories 页面，`/docs/context/memories` 返回的是 Rules 页面。因此本节只记录"Cursor 的官方立场是规则而非自动记忆"这一可核对事实，**无法确认 Cursor 是否仍有独立于 Rules 的 Memories 功能**。
- **Microsoft Copilot 只抓到两篇的支持页面主体**，页面导航很长，未逐条核对全部设置项与计划差异。
- **OpenAI 的帮助中心与 openai.com 对普通 UA 返回 403**，本次用浏览器 UA 抓取正文；未能核对登录后才可见的设置界面。
- **未做产品实测**：所有行为描述都来自官方文档，未在真实账号上验证记忆的写入时机、检索命中与删除延迟。
- **未系统采样用户抱怨**：第三方社区、应用商店评论、issue 区均未采集；本文的"官方自己承认的问题"一节只覆盖官方文档里自己写出来的失效模式。
