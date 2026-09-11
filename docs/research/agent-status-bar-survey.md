# Agent 状态栏：主流项目的横向调研

研究日期：2026-09-11。只读源码与官方发布物，未运行这些项目、未调用付费模型，也未在其 issue 区提问。固定版本：[earendil-works/pi@f3c6722](https://github.com/earendil-works/pi/tree/f3c672245d25ef2283ffc0d9cdec8a5482651103)、[openai/codex@624ccf7](https://github.com/openai/codex/tree/624ccf794703e2d84e748fc3ef547d6191a8c0a4)、[sst/opencode@193de13](https://github.com/sst/opencode/tree/193de13a88d62a6409c6d385831180f1def527dc)、[google-gemini/gemini-cli@ed2ac40](https://github.com/google-gemini/gemini-cli/tree/ed2ac40df67a319bf348bd7e3d10494696b31b38)、[block/goose@846cbea](https://github.com/block/goose/tree/846cbeaf5157f9be8a22aec93bd2ba9c5ddad983)、[cline/cline@a7c1dfc](https://github.com/cline/cline/tree/a7c1dfc2987e25a6132370ac563c36b05bd5822e)、[Aider-AI/aider@5dc9490](https://github.com/Aider-AI/aider/tree/5dc9490bb35f9729ef2c95d00a19ccd30c26339c)。以下是这些版本的行为，不保证其他版本相同。

## 结论

把 Jarvis 计划中的状态栏拆成五项分别看，普遍程度完全不同，"很多项目没有这个状态栏"这个印象需要分开表述：

| 分项 | 主流项目里的普遍程度 | 代表实现 |
| --- | --- | --- |
| 待办列表（TODO） | 高，接近标配 | Claude Code、Gemini CLI、opencode、Goose；pi 明确拒绝（Cline 本次未核实到 todo 实现） |
| 环境状态（工作目录/平台/时间） | 高，但形态不同 | 多数写进系统提示的稳定前缀；Codex 做成可增量注入的对话片段 |
| 每轮时间戳 | 低 | 只找到 Codex `current_time_reminder`，且带节流 |
| 工具调用计数、重复调用计数 | 未发现先例 | 没有任何项目把它注入模型上下文 |
| 错误修复建议（机制化 hint） | 未发现先例 | 工具返回错误文本，但没有系统性的修复建议映射 |

也就是说：给模型"状态信息"很常见，但把它做成一条同时含**计数、重复计数、错误建议**的复合状态消息，在调研范围内没有同类。状态栏里最有共识的是待办和环境状态，最没有先例的是计数类字段。这与观察一致，但不是"项目都不做状态"，而是"项目都做状态，只是没有做成 Jarvis 这种复合形态"。

## 逐项证据

### pi：明确不做，并把理由写进 README

- README 的 Philosophy 段直接列黑名单："No plan mode"、"**No built-in to-dos.** They confuse models. Use a TODO.md file, or build your own with extensions"。同一段还有 No MCP、No sub-agents、No permission popups、No background bash。[README](https://github.com/earendil-works/pi/blob/f3c672245d25ef2283ffc0d9cdec8a5482651103/packages/coding-agent/README.md#L505)
- pi 只在系统提示末尾写一行工作目录，没有时间、没有计数：`Current working directory: ${promptCwd}`。[system-prompt.ts:70](https://github.com/earendil-works/pi/blob/f3c672245d25ef2283ffc0d9cdec8a5482651103/packages/coding-agent/src/core/system-prompt.ts#L70)、[system-prompt.ts:165](https://github.com/earendil-works/pi/blob/f3c672245d25ef2283ffc0d9cdec8a5482651103/packages/coding-agent/src/core/system-prompt.ts#L165)
- 在 `packages/` 下检索 `system-reminder`、`reminder`、`injectContext` 等模式没有命中，即 pi 不在对话里周期性插入合成状态消息。pi 的"状态栏"是 UI 概念：扩展可以注册 "Status lines, headers, footers"（README:392），那是终端显示，不进模型上下文。
- pi 内部有消息时间戳（`AgentMessage.timestamp`），但发给供应商时不带：`openai-completions.ts` 全文件除构造响应外不引用 `timestamp`，`convertMessages` 只映射 role/content/tool_calls。即 pi 有时间元数据，但选择不让模型看到。[messages.ts:27](https://github.com/earendil-works/pi/blob/f3c672245d25ef2283ffc0d9cdec8a5482651103/packages/agent/src/harness/messages.ts#L27)、[convertMessages:1178](https://github.com/earendil-works/pi/blob/f3c672245d25ef2283ffc0d9cdec8a5482651103/packages/ai/src/api/openai-completions.ts#L1178)
- pi 把注入通道留给了扩展：消息类型含 `role: "custom"` + `customType`，扩展可以据此把内容塞进对话；自带示例 `examples/extensions/todo.ts` 就是一个可选的 todo 工具，官方不默认装。[messages.ts:47](https://github.com/earendil-works/pi/blob/f3c672245d25ef2283ffc0d9cdec8a5482651103/packages/coding-agent/src/core/messages.ts#L47)、[todo.ts](https://github.com/earendil-works/pi/blob/f3c672245d25ef2283ffc0d9cdec8a5482651103/packages/coding-agent/examples/extensions/todo.ts)
- 取舍明确：pi 认为清单会干扰模型，建议用户改用 `TODO.md` 文件。

### OpenAI Codex CLI：环境与时间是两块增量片段，仍不是状态栏

- **环境状态**：`EnvironmentsState` 实现 `WorldStateSection`，渲染 `<environment_context>`，内容含每个环境的 cwd/shell/status、`<current_date>`、`<timezone>`、`<network>`、`<filesystem>`（权限档）。关键在结构：它是**快照 + diff**，`render_diff` 只在与上一份快照不同时产生新的 user 角色片段，无变化不重复注入。[environment.rs:104](https://github.com/openai/codex/blob/624ccf794703e2d84e748fc3ef547d6191a8c0a4/codex-rs/core/src/context/world_state/environment.rs#L104)、[render_diff:134](https://github.com/openai/codex/blob/624ccf794703e2d84e748fc3ef547d6191a8c0a4/codex-rs/core/src/context/world_state/environment.rs#L134)、[环境字段渲染:317](https://github.com/openai/codex/blob/624ccf794703e2d84e748fc3ef547d6191a8c0a4/codex-rs/core/src/context/world_state/environment.rs#L317)
- **时间戳**：`<current_time_reminder>It is ... UTC.</current_time_reminder>`，role 是 `developer`，内容是"当前时间"而不是"本轮时间"。[current_time_reminder.rs:28](https://github.com/openai/codex/blob/624ccf794703e2d84e748fc3ef547d6191a8c0a4/codex-rs/core/src/context/current_time_reminder.rs#L28)、[:41](https://github.com/openai/codex/blob/624ccf794703e2d84e748fc3ef547d6191a8c0a4/codex-rs/core/src/context/current_time_reminder.rs#L41)
- **节流**：`take_reminder_due` 同时受 `reminder_interval_seconds` 最小间隔和 delivery mode 控制，可选"仅在用户输入或工具输出边界之后"插入；不是每轮必发。[time_reminder.rs](https://github.com/openai/codex/blob/624ccf794703e2d84e748fc3ef547d6191a8c0a4/codex-rs/core/src/session/time_reminder.rs)
- **计划**：`update_plan` 工具，由 `config.update_plan_enabled` 控制开关，与 Jarvis 的 `write_todos` 同类。[spec_plan.rs](https://github.com/openai/codex/blob/624ccf794703e2d84e748fc3ef547d6191a8c0a4/codex-rs/core/src/tools/spec_plan.rs)
- 仍然没有工具调用计数、重复计数和错误修复建议。

### Claude Code / Agent SDK：TodoWrite 是官方工具之一

- 官方 npm 包 `@anthropic-ai/claude-code@2.1.268` 附带的 `sdk-tools.d.ts` 定义 `TodoWriteInput { todos: { content: string; status: "pending" | "in_progress" | "completed"; activeForm: string }[] }`，是全量覆盖式写入。同一份类型里还有 `TaskCreate`、`TaskGet`、`TaskUpdate`、`TaskList` 和 `EnterPlanMode`。
- 与 Gemini/opencode 相比少一个 `cancelled`（或 `blocked`）状态，只有三态；输入多一个 `activeForm`（进行中条目的动词形式，供 UI 展示）。
- 边界：CLI 本体是闭源二进制，只能从官方 SDK 类型确认工具存在，无法确认它是否在对话里注入类似的合成状态消息。

### opencode：全量覆盖 todowrite + 合成 reminder

- `todowrite` 工具描述给出触发条件"3+ distinct steps"，四态 `pending/in_progress/completed/cancelled`，规则含"Keep exactly one `in_progress` while work remains"和"Update status in real time; don't batch completions"。[todowrite.txt:3](https://github.com/sst/opencode/blob/193de13a88d62a6409c6d385831180f1def527dc/packages/opencode/src/tool/todowrite.txt#L3)、[:19](https://github.com/sst/opencode/blob/193de13a88d62a6409c6d385831180f1def527dc/packages/opencode/src/tool/todowrite.txt#L19)、[:27](https://github.com/sst/opencode/blob/193de13a88d62a6409c6d385831180f1def527dc/packages/opencode/src/tool/todowrite.txt#L27)
- 存储是会话级 SQLite：每次写入先按 session 删除再按 position 插入，即"当前完整清单"语义。[session/todo.ts](https://github.com/sst/opencode/blob/193de13a88d62a6409c6d385831180f1def527dc/packages/opencode/src/session/todo.ts)
- 框架会往最后一条 user 消息里追加 `synthetic: true` 的文本片段（plan、build-switch、plan-mode 提醒），这是"往对话里塞合成消息"的现成做法，但内容只与 plan 模式有关，不含计数。[reminders.ts](https://github.com/sst/opencode/blob/193de13a88d62a6409c6d385831180f1def527dc/packages/opencode/src/session/reminders.ts)

### Gemini CLI：write_todos，会话级，五态

- `write_todos` 参数是**完整列表**（覆盖语义），状态为 `pending / in_progress / completed / cancelled / blocked`，只允许一个 `in_progress`，状态"scoped to the current session"，UI 在输入框上方显示进度并支持 Ctrl+T 展开。[docs/tools/todos.md](https://github.com/google-gemini/gemini-cli/blob/ed2ac40df67a319bf348bd7e3d10494696b31b38/docs/tools/todos.md)
- 工具返回值把渲染后的清单回给模型（`Successfully updated the todo list. The current list is now: ...`），与 Jarvis 计划里"write_todos 返回渲染后的清单让模型确认"一致。[write-todos.ts](https://github.com/google-gemini/gemini-cli/blob/ed2ac40df67a319bf348bd7e3d10494696b31b38/packages/core/src/tools/write-todos.ts)
- 比 Jarvis 计划多一个 `blocked` 状态。

### Goose：自由文本 checklist + 自动进上下文

- `todo` 扩展的参数只有一个 `content: String`（自由格式 checklist，示例是 Markdown 复选框），工具说明写着 "Your todo content is automatically available in your context"，并有 `GOOSE_TODO_MAX_CHARS` 上限（默认 50000）。[todo.rs:38](https://github.com/block/goose/blob/846cbeaf5157f9be8a22aec93bd2ba9c5ddad983/crates/goose/src/agents/platform_extensions/todo.rs#L38)
- 这是"注入模型上下文"的另一种形态：不是工具结果，而是把 todo 内容每轮拼进上下文。

### Cline：环境信息在系统提示里

- 系统提示含 `<env>` 块：Platform、Date、IDE、Working Directory，用 `{{CURRENT_DATE}}`、`{{CWD}}` 等占位符在组装时替换。[system.ts:7](https://github.com/cline/cline/blob/a7c1dfc2987e25a6132370ac563c36b05bd5822e/sdk/packages/shared/src/prompt/system.ts#L7)
- 需要区分：Cline 历史版本在每条用户消息后追加 `<environment_details>`（cwd、可见文件、打开的标签页、当前时间）。当前版本的仓库里没有定位到该模块，本次无法确认它是否仍然存在。上面的 env 块是能确认的部分。

### Aider：不做

- `aider/` 下没有 todo / plan / status 相关模块，它不使用工具调用，靠 edit format 与 repo map 工作。可作为"不引入这套机制"的代表。

## 对 Jarvis 的设计含义（建议，非已实现事实）

1. **待办工具属于主流做法，不是自创。** Jarvis 计划的全量覆盖 + 至多一个进行中，与 opencode、Gemini CLI 的形态基本一致；可直接照搬这套已被反复使用的规则。需要单独决定的是是否加入 `blocked` 状态（Gemini 有，opencode 与 Claude 没有）。
2. **环境状态有先例，但主流放在稳定前缀或变化时才注入。** Codex 只在环境快照变化时产生新片段；Jarvis 计划每轮重渲染状态栏并带当前时间。因为状态栏在上下文末尾，这不破坏前面的缓存前缀，但会让"最后一条"每轮都不同——Codex 的做法提示可以把每轮必变的时间字段单独拆出并节流。
3. **计数与错误建议没有先例可参照。** 工具调用次数、重复调用次数、机制化修复建议在调研范围内都没有同类实现，因此既不能说"大家都这么做"，也不能引用别人的效果数据。这部分成本低，但收益只能由 Jarvis 自己测量。
4. **pi 的反面意见值得记录在案。** pi 主动不做 todo，理由是"会干扰模型"；Jarvis 自己的冻结核对里 TODO 组也是负向。这不构成"不该做"，但说明 TODO 的收益不是显然的，验收时不应预设它会带来改进。

**本次决定（2026-09-11，已按 A 实施）：移除状态栏。** 不再向每轮请求注入 `<agent_status>`，模型看不到逐工具计数、错误建议、环境状态、预算和证据索引；静态信息（工作目录）继续放在系统提示，压缩仍由代码按阈值触发，只在压缩发生时把摘要注入对话。待办从一开始就不实现，pi 的"用 TODO.md 文件代替"暂不可行，因为 Jarvis 只有三个只读工具。判断依据见上面第 1–4 条与 pi 的 Philosophy：复合状态栏在调研范围内没有同类，计数与错误修复建议零先例，时间戳仅 Codex 一处且带节流，而 pi 的最小核心明确不做这些。五项都随时可以按本页记录重新提出来。

## 未验证事项

- Claude Code CLI 本体闭源，只能依据官方 SDK 类型；无法确认它在运行时注入哪些合成消息。
- Cline 当前版本是否仍追加 `<environment_details>`，本次未定位到源码。
- 未运行任何被调研项目，未做模型侧实测；以上全部是源码与官方文档的静态结论。
