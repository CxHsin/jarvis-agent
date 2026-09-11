# Pi 的 Prompt Cache 与上下文窗口

研究日期：2026-09-11。只读源码，未运行 Pi、未调用付费模型。固定版本为 [earendil-works/pi@d12cd92e45e308d4af000554292165ef1984253b](https://github.com/earendil-works/pi/tree/d12cd92e45e308d4af000554292165ef1984253b)。以下是此版本行为，不保证其他版本相同，也不把 Pi 的兼容性声明当成供应商服务承诺。

## 结论

Pi 把两件事分开：缓存由供应商适配器配置并从服务端 usage 读取；上下文容量由具体 provider/model 元数据决定。缓存 token 仍占上下文窗口，不能因为命中而从窗口用量中扣除。Pi 没有通用的“自动测出任意模型窗口”机制：常用模型读生成目录，自定义模型可覆盖，一部分供应商支持动态刷新。

## Prompt Cache：请求配置与真实测量

- Anthropic 适配器默认 `short`，支持 `none`、`long`；`long` 在兼容时带 `ttl: "1h"`。它给系统提示、最后一个工具定义添加 `cache_control`；转换后的末条消息为 user 时，在其最后一个受支持内容块上添加标记，包含工具结果块。不是把答案存在客户端复用。[保留策略](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/ai/src/api/anthropic-messages.ts#L53)、[系统提示](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/ai/src/api/anthropic-messages.ts#L1065)、[历史末端](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/ai/src/api/anthropic-messages.ts#L1374)、[工具](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/ai/src/api/anthropic-messages.ts#L1459)。
- OpenAI Responses 使用会话 ID 派生 `prompt_cache_key`，保留时间参数受兼容能力控制；此版本对较新模型还有 `prompt_cache_options` 分支。Chat Completions 的参数发送条件不同，不能直接给任何中转服务塞同一套参数。缓存 key/标记只是请求配置，源码不能证明实际服务已命中。[Responses 参数](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/ai/src/api/openai-responses.ts#L301)、[Completions 参数](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/ai/src/api/openai-completions.ts#L804)。
- Pi 明确关注前缀稳定性：支持原生延迟工具定义的模型，把新工具放到工具结果之后；不支持的模型仍更新初始工具列表，因此可能破坏缓存。工具附带的 promptSnippet/promptGuidelines 导致系统提示重建，也会影响前缀。[官方扩展文档](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/coding-agent/docs/extensions.md#L2375)。
- 一次性压缩/分支摘要调用设置 `cacheRetention: "none"`；缺少调用者路由 ID 时生成新 ID。是否真的禁止写缓存仍依赖供应商实现。[摘要调用入口](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/coding-agent/src/core/compaction/compaction.ts#L580)。

| 协议 | Pi 读取的真实缓存字段 | 内部归一化 |
| --- | --- | --- |
| OpenAI Chat Completions 及兼容接口 | `prompt_tokens_details.cached_tokens`，其次 `prompt_cache_hit_tokens`、顶层 `cached_tokens` | `input = prompt_tokens - cacheRead - cacheWrite`，下限为 0 |
| OpenAI Responses | `input_tokens_details.cached_tokens` | 从 `input_tokens` 扣除缓存读写形成未缓存 input |
| Anthropic Messages | `cache_read_input_tokens`、`cache_creation_input_tokens` | 服务端 input 与缓存读写分别记录 |

来源：[Completions usage](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/ai/src/api/openai-completions.ts#L1510)、[Responses usage](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/ai/src/api/openai-responses-shared.ts#L560)、[Anthropic usage](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/ai/src/api/anthropic-messages.ts#L607)。

Pi 状态栏累计显示缓存读写 token，最近一次的缓存比例为 `cacheRead / (input + cacheRead + cacheWrite)`；注意这里的 input 是上述归一化后的未缓存输入，不能把这个公式原样用于 OpenAI 原始 prompt_tokens，再次加缓存就会重复计数。[状态栏](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/coding-agent/src/modes/interactive/components/footer.ts#L89)。

限制：上述适配器通常将缺失缓存字段归零。因此它的 0 不能充分区分“服务端明确未命中”和“没有上报”；Jarvis 的真实测量需求应保留这一区别。token 缓存比例也不是“有命中的请求数 / 请求总数”，两者应分别命名。

## 模型窗口从哪里来

1. **生成目录。** `generate-models.ts` 拉取 models.dev、OpenRouter 等模型目录，归一化上下文和输出限制，同时有人工修正及缺失值回退；例如 OpenRouter 使用 top_provider.context_length/context_length。生成结果按 provider/model 读取，普通 OpenAI provider 直接使用生成数据。[目录生成](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/ai/scripts/generate-models.ts#L1141)、[models.dev](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/ai/scripts/generate-models.ts#L1510)、[修正示例](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/ai/scripts/generate-models.ts#L2470)、[OpenAI provider](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/ai/src/providers/openai.ts#L6)、[精确键读取](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/ai/src/providers/all.ts#L61)。这是一份可更新的静态快照，不是每次请求自动发现窗口。
2. **自定义与覆盖。** `models.json` 支持每个模型的 `contextWindow`、`maxTokens` 和 modelOverrides；文档声明打开 `/model` 时重载。自定义模型省略容量时默认 **128000**，最大输出默认 **16384**。这部分确实有默认常数，不能认为 Pi 完全解决了未知模型容量。[自定义模型文档](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/coding-agent/docs/models.md#L193)。
3. **供应商专属动态刷新。** Radius provider 实现 refreshModels，先恢复持久目录，再按 allowNetwork 和鉴权状态拉 gateway config、发布模型列表。框架允许静态和动态 provider 共存；不能推广为任何 OpenAI-compatible `/models` 都提供窗口。[Radius 实现](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/ai/src/providers/radius.ts#L19)、[刷新接口](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/ai/src/models.ts#L46)。

目录里的 contextWindow 还可能是产品策略限制而非模型物理极限，例如 Pi 文档说明某些模型默认取短上下文计费档的边界，可手动提高。这提示 Jarvis 应区分供应商容量与用户选择的使用预算。[逐模型覆盖说明](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/coding-agent/docs/models.md#L351)。

## 上下文用量、输出空间与压缩

- 用量从最近有效 assistant usage 起算，加上尚未计入 usage 的新增消息估算；没有可用 usage 时使用字符数约除以 4 的启发式。API 层还估算系统提示和工具定义。字符估算不是精确 tokenizer，不能保证对中文、代码、图片始终保守。[压缩用量](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/coding-agent/src/core/compaction/compaction.ts#L198)、[API 估算](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/ai/src/utils/estimate.ts#L13)。
- 上下文 token 包含 `input + output + cacheRead + cacheWrite`，或可信 totalTokens。**缓存只减少重复计算/费用，不扩大窗口。**[计算公式](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/ai/src/utils/estimate.ts#L17)。
- 压缩条件是 `contextTokens > contextWindow - reserveTokens`。默认 reserveTokens=16384，keepRecentTokens=20000；压缩保留近期消息并摘要较旧内容，切点避免从工具结果开始。参数允许按 provider/model 覆盖。[默认值](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/coding-agent/src/core/compaction/compaction.ts#L126)、[阈值](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/coding-agent/src/core/compaction/compaction.ts#L235)、[切点](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/coding-agent/src/core/compaction/compaction.ts#L388)、[逐模型参数](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/coding-agent/src/core/settings-manager.ts#L852)。
- API simple-options 另把请求输出上限限制为不超过 `contextWindow - estimatedContext - 4096`，最少 1；这与压缩预留是两个层次。[输出限制](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/ai/src/api/simple-options.ts#L13)。
- 若仍发生上下文溢出或可恢复截断，移除失败/截断回复、压缩后只重试一次；检查同一模型及压缩边界，防止拿旧错误重复压缩。[恢复路径](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/coding-agent/src/core/agent-session.ts#L2154)。

## 对 Jarvis 的建议（尚未实施）

现有 [ADR](../adr/0001-context-management-and-compression.md) 已定义稳定前缀、86% 触发与约 65% 目标、显式窗口优先和上游元数据次之。当前不是没有上下文管理，而是窗口解析与用量闭环还不完整。

本地源码核对：`jarvis_agent.py:355` 把总窗口和 max_input_tokens/input_token_limit 放在同一组候选字段；`discover_context_window` 在退回模型列表时递归取第一个容量值，没有先按当前模型 ID 筛选，存在取错模型容量的风险。`context_manager.py:235` 以本地估算驱动阈值，未知窗口时不触发压缩。这些是现状，不应误认为 Pi 研究已修复。

建议后续规格明确：

1. 模型能力按 endpoint/provider/model ID 绑定，分别保存总窗口、输入上限、输出上限和实际使用预算，记录来源与刷新时间；精确匹配元数据，避免按名字猜或从列表取首个值。
2. 保留显式配置优先；可验证的服务元数据次之；是否增加版本化模型目录需要确认并更新 ADR。未知模型保持未知、提示配置，不照搬 Pi 的 128000 默认值。
3. 实际 usage 用作当前请求的用量锚点，新增内容继续估算；压缩、前缀变化和模型切换后使旧锚点失效。窗口约束同时考虑输出空间、输入限制和工具结果增长。
4. 每次调用记录原始 usage 与归一化缓存字段，区分缺失与零，主模型和压缩模型分开；只有服务端字段能作为本任务的真实命中证据。缓存占比必须使用包含缓存的总输入作分母。
5. 保持系统指令、工具定义及历史前缀稳定，动态状态追加到末尾；压缩会改写历史，应标记为潜在缓存失效事件。先测量现状，再通过真实重复请求验证改变的收益。

不确定边界：未运行任何供应商请求，无法确认用户实际中转服务返回哪些字段、是否透传缓存参数、服务端缓存保留多久及当前部署真实窗口。这些需要服务元数据或受控实测，不能从 Pi 源码推定。
