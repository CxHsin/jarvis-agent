# Jarvis 第一阶段

这是一个可观察的命令行个人 Agent 基线。它支持 OpenAI 兼容的 Chat Completions 接口，并提供四个稳定工具：`read`、`edit`、`bash`、`tool_search`，同时保留旧文件工具名称作为兼容别名。对话历史写入状态目录下的会话记录，每次启动默认新建会话，用 `--resume` 显式接上上一段。

Memory tools are discovered with `tool_search`: `memory_search` and `memory_manage` (`remember`, `correct`, `forget`). Retrieval reads `STATE_DIR/memory/memory.db` through SQLite FTS5 and sqlite-vec; it never reads `memory.md`. Configure an OpenAI-compatible embedding endpoint with `EMBEDDING_BASE_URL`, `EMBEDDING_API_KEY`, `EMBEDDING_MODEL`, and `EMBEDDING_DIMENSIONS`. Without these settings, FTS5 results remain available and the response reports vector retrieval as unavailable. Direct `edit` calls are blocked for a configured in-workspace memory directory; a generic shell remains able to mutate files, so this is a runtime guard rather than a filesystem sandbox. Query rewrites and HyDE passages are ephemeral and are never stored as Memory facts.

## 启动

1. 复制 `.env.example` 为 `.env`，填写 `MODEL` 和 `API_KEY`；`BASE_URL` 可以指向任何兼容 Chat Completions 的服务。可选的 `COMPRESSION_MODEL` 用于上下文摘要，未填写时使用主模型。
2. 确认 `ROOT_DIR` 指向默认工作区。当前验收目录是 `D:\Course\Study\matt video`。会话记录默认写到系统用户状态目录（Windows 为 `%LOCALAPPDATA%\jarvis`），用 `STATE_DIR` 指定其他位置；它放在工作区之外。
3. 使用 Python 3.12 创建的项目虚拟环境运行：

```powershell
.\.venv\Scripts\python.exe .\jarvis_agent.py
```

如果尚未创建虚拟环境：

```powershell
& 'C:\Users\Cx\AppData\Local\Programs\Python\Python312\python.exe' -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

输入 `exit` 退出，`Ctrl+C` 取消当前请求，`/compact` 主动压缩上下文（`/compact 2000` 指定本次保留的 token 数）。`--list` 列出当前工作区的会话，`--resume` 接上最近一段，`--resume <id>` 接上指定会话。

## 第一阶段验收

- 不需要文件信息的请求可以直接回答。
- 模型可以连续调用工具，并依据工具结果继续或结束。
- 每次启动默认新建会话，`--list` 能列出本工作区的全部会话；`--resume` 接上最近一段（`--resume <id>` 指定）。
- 会话记录写穿落盘：进程被杀后 `--resume` 仍能接上，缺失结果的工具调用补写「执行状态未知」占位，不重新执行。
- 尚未执行工具时，模型请求失败会回滚本轮对话；已经执行工具时保留结果与审计，避免遗失副作用证据。权限与供应商回退状态不随对话回滚。
- 压缩过的历史段落恢复后，仍能从会话归档取回原文，摘要里的来源引用保持可核对。
- 同一轮的独立工具可并行；依赖调用、同文件操作和声明串行的工具顺序执行，结果按调度顺序记录。
- 同一次运行可以追问；退出默认开启新会话，`--resume` 才会接上上一段。
- 工具失败会作为结果交回模型；模型请求失败会结束当前请求并回到输入状态。
- 达到 `MAX_ROUNDS` 时最后一轮只生成回答，不再执行工具。
- 模型请求不再注入框架生成的状态栏；工作目录等静态信息保留在系统提示中，证据索引和上下文预算由代码在进程内维护。
- Jarvis 按接口地址和模型精确匹配容量。`CONTEXT_WINDOW_TOKENS` 显式覆盖优先，其次是自定义/内置模型目录，目录未命中时尝试上游模型元数据；仍未知时要求补充配置后启动，不套用其他模型的容量。
- `估计输入 > 窗口 − CONTEXT_RESERVE_TOKENS` 时触发压缩：从切点开始把更旧的完整 turns（含助手正文）压成一条累积 checkpoint，切点之后的近期原文保留。切点优先落在任务边界（user），任务内部退到轮边界（assistant），tool 结果不单独切断。压缩写进会话记录，原始结果保留在会话归档中，摘要保留来源引用。
- 压缩有三种触发，走同一个服务、产出相同的状态效果：预算驱动的自动压缩、用户输入的 `/compact`、以及服务端报告上下文超限后的溢出恢复。`/compact` 不带参数时退休比当前任务更旧的全部原文，带 token 数时按该数字保留最近内容（仍只落在任务或轮边界上）。溢出恢复只重试一次；服务端错误无法稳定识别时退回拒绝发送。
- 每次模型调用显示服务端输入 token、缓存命中 token 和占比；任务结束后按主模型/压缩模型分别汇总。缺失或不一致的缓存字段标为“未知”，明确返回 0 才算未命中；总占比按总命中量除以总输入量计算，存在未知调用时不展示误导性的完整占比。
- 终端默认只显示工具结果摘要和短预览；完整结果仍发送给模型。设置 `VERBOSE_TOOL_OUTPUT=true` 可临时显示完整工具结果，`TOOL_OUTPUT_PREVIEW_CHARS` 控制预览长度。

`MAX_ROUNDS` 按主模型调用次数计数。`read` 和 `read_file` 支持读取工作区外的文本文件：绝对路径直接解析，相对路径以 `ROOT_DIR` 为基准。`edit`、`list_directory` 和 `search_file_content` 限制在 `ROOT_DIR` 内；`bash` 以该目录为起点，但没有操作系统沙箱，获准的命令能访问进程有权限访问的位置。

## 工具运行时

固定工具为 `read`、`edit`、`bash`、`tool_search`。动态工具通过 `tool_search` 搜索并激活，定义追加在搜索结果中，固定 `tools` 列表不改变；下一任务必须重新搜索，历史定义保留但不授予执行权限。压缩退休搜索结果时，仅补回当前任务已激活的定义，不插入空标记或任务边界标记。这里保证确定性序列化，不承诺供应商一定命中缓存。

`TOOL_PERMISSION_MODE` 支持 `approve-all`（每次工具调用确认）、`approve-dangerous`（默认，高风险调用确认）和 `broad-access`（显式宽授权）。`bash` 一律视为高风险；文件编辑为中风险。命令行遇到需确认的操作会提示，非交互使用应传入 `Agent(confirm_tool=...)`，否则返回 `confirmation_required`。工具本身不能确认或升级权限。`agent.tool_runtime.policy.change_mode(...)` 可直接收紧，升级必须由宿主完成用户确认后传入 `confirmed=True`；`revoke()` 阻止新的副作用，变更与确认均写入会话审计。重启配置只能进一步收紧已保存的授权。

每个工具版本保存不可变 schema、SHA-256 指纹、风险、资源、副作用、超时、输出上限与并发声明。同版本不能替换处理函数。参数按 JSON Schema 2020-12 校验，远程 schema 引用不受支持。`TOOL_MAX_TIMEOUT` 是会话超时上限，工具自身的上限仍生效；输出同时受工具和权限策略上限约束。内置 shell 的超时与取消会终止进程树，文件编辑在提交前检查取消与策略版本。Python 扩展处理函数应使用 `contextual=True` 和 `ExecutionContext` 合作取消；无法中止的扩展返回 `uncertain` 并保留资源占用，运行时不会自动重试可能产生副作用的调用。

同批调用可在参数中使用 `_depends_on: ["call-id"]` 声明依赖，或使用 `{"$result":{"call_id":"call-id","path":["字段"]}}` 引用前置结果；引用会在参数校验前解析。前置失败会阻止依赖调用。`_version` 与 `_schema_fingerprint` 可显式校验搜索得到的版本；不存在的版本、环、冲突及取消均返回结构化错误。`read` 返回文件 `hash`，`edit(expected_hash=...)` 可拒绝过期写入；未指定时，同批编辑也会比较执行前快照，避免两个同文件写入静默覆盖。

供应商模式由 `PROVIDER_TOOL_MODE` 显式选择，与模型容量配置无关。通用 Chat Completions 客户端使用 `emulated`。原生供应商由宿主传入 `Agent(native_loader=...)`，协议为 `(measured_client, messages, stable_tools, active_definitions, tool_choice) -> assistant_message`；适配器负责供应商专有的 deferred/tool-reference 请求。可重试的网络/能力错误连续失败三次后，本会话只回退一次并重新发出 emulated 请求；失败调用的局部引用不会写入历史。模式、回退次数、历史版本与权限随会话恢复。没有原生适配器时配置 `native` 会按相同规则回退。

验收：`python -m pytest -q`。`tests/test_tool_runtime_acceptance.py` 从 Agent 循环验证发现、权限、依赖、并发、编辑冲突、原生回退、恢复和压缩；供应商专有协议通过可注入适配器测试，未调用真实付费模型服务。

## 模型容量与缓存

内置 `model_capabilities.json` 当前覆盖 DeepSeek 官方 `deepseek-flash` 和 `deepseek-v4-flash`。旧别名保持原样发送；容量目录记录来源和核对日期，不会在启动时抓取网页。需要更新时编辑目录，或通过 `MODEL_CAPABILITIES_FILE` 指定相同结构的 JSON 文件。自定义条目优先于内置条目，仅对精确匹配的接口和模型生效：

```json
{
  "models": [{
    "base_url": "https://my-provider.example/v1",
    "model_ids": ["my-model"],
    "context_window_tokens": 131072,
    "max_output_tokens": 16384,
    "source": "供应商文档地址",
    "checked_at": "2026-09-11"
  }]
}
```

窗口、输入上限（可选 `max_input_tokens`）和输出上限分别记录。模型目录的数值是来源明确的配置，不能证明当前服务端边界；服务变更后需要更新。DeepSeek 的 `/models` 不返回容量，内置目录中的 1M 按官方模型配置的 1048576 表示，来源见目录备注。

- `MAX_OUTPUT_TOKENS=32768`：单次输出上限，受目录中的模型输出能力约束，并作为 `max_tokens` 发给服务端。
- `CONTEXT_RESERVE_TOKENS=16384`：压缩触发线与发送上限共用这一个预留值；输出上限按剩余空间收敛，并为估算误差留 4096 token 的地板。估算误差跟随内容类型而不是窗口大小，因此不再按窗口比例定义安全余量。
- `MAX_TOOL_RESULT_TOKENS=8192`：单条工具结果进入模型上下文前的 token 上限，超限按行或条目截断并标 `truncated`；`read_file` 同时返回 `next_start_line` 供模型续读。`MAX_DIRECTORY_ENTRIES=200` 限制目录列举的条目数。
- `CONTEXT_KEEP_RECENT_TOKENS=20000`：压缩后保留原文的软目标（绝对 token）；吸附到合法边界时允许略超。
- `CONTEXT_COMPACTION_FAILURE_LIMIT=3`：连续摘要失败达到该次数后熔断，停止重试。
- `COMPRESSION_CONTEXT_WINDOW_TOKENS`、`COMPRESSION_MAX_OUTPUT_TOKENS`：单独覆盖压缩模型。不同压缩模型单独解析容量，不继承主模型的窗口。小窗口模型需要相应降低输出上限；输出预留或保留窗口无法容纳的配置会在启动时报错。

可用输入预算是窗口扣除预留，并受服务输入上限约束。预算估算以最近一次实际输入 usage 为基础，加上新增内容的估算；历史被改写、工具定义改变或模型改变后回退到本地估算。缓存 token 仍然占窗口。压缩按切点收回旧历史段落、生成结构化 checkpoint，并保留最近窗口原文；压缩后仍超过触发线会提示，超过可用输入预算会拒绝发送，避免已知超限请求。压缩请求本身也做预算检查，失败时使用本地降级摘要，连续失败触发熔断器。单次输出上限仍是 `MAX_OUTPUT_TOKENS`，但实际发送的 `max_tokens` 会按剩余窗口收敛。

DeepSeek 缓存默认开启。Jarvis 读取 `prompt_cache_hit_tokens`、`prompt_cache_miss_tokens`，并兼容 `prompt_tokens_details.cached_tokens`，不重复相加。缓存构建和命中由服务端决定，重复请求不保证命中。指标不会添加到模型历史中；当前任务原始 usage 保存在进程内 `Agent.usage_ledger.records`，下个任务开始清空。

## 结构

个人历史位于跨工作区共享的 `STATE_DIR/memory/`：`history/YYYY-MM-DD.md` 只追加用户输入、事件时间、UTC 记录时间和任务/事件来源标识；未知事件时间为空，不以接收时间代替。`trajectories/<会话标识>.jsonl` 保存完整消息、工具结果和状态事件，模型失败回滚及上下文压缩不会删除这些原始证据。`recent/<会话标识>/recent.md` 每条消息后更新，保存在途任务和最近 `RECENT_TASK_COUNT` 个已结束任务（默认 5）。失败且没有工具执行的请求保留审计记录，但不占 Recent 窗口。恢复使用同一会话的窗口；新会话独立开始。

下一任务从 Recent 的完整消息建立上下文，并经过既有压缩与 token 硬上限检查；可读 Recent 文件和原始轨迹保持完整，即使模型请求需要压缩。Recent 是可重建视图，不能通过编辑它改变原始记录。

`STATE_DIR/memory/self.md` 是用户维护的 Agent 身份、原则、能力和工具约束；只在初次创建时提供默认内容，不从经历自动生成。`memory.md` 是有效用户事实的画像投影，六个固定栏目为 `identity`、`work_preferences`、`communication`、`long_term_goals`、`constraints` 和 `current_state`，空栏目省略。画像按 UTF-8 字节数除以 4 的估算限制在 1,500 token 内；超额事实仍在数据库保留，不截断事实行。自动提取批次完成、夜间 consolidation 完成或用户修正后更新画像；夜间调度为本地凌晨 03:00，错过会补跑。相同内容不新增版本，SQLite 的 `profile_versions` 保留内容、来源事实 ID、token 估算及创建/激活时间。每个任务开始时一次性读取 self 和画像，任务内所有模型轮次使用相同前缀，新版本下个任务生效。召回只查数据库，不使用 Markdown。

人工修改 `memory.md` 时保留标题及栏目，每行采用 `- {"fact_id":"原 ID","predicate":"原属性","object":"新值"}`。现有行只修改 `object`；删除整行表示忘记该事实。新增行使用 `"fact_id":null`，填写明确的属性及值。下一任务前整份校验并作为高置信度用户修正导入，旧事实失效但不删除，完整编辑原文保存在数据库来源记录。格式错误、未知 ID 或修改既有属性/栏目会明确报错，保留文件且不部分导入；后台更新不会覆盖尚未导入的人工修改。此结构化格式避免用字符串猜测用户修正的语义。

完成任务移出 Recent 时，会立即在 `memory.db` 创建幂等 Pending 批次；`pending.md` 是批次和候选的可读视图，不进入模型上下文，也不复制原始对话。后台提取使用主模型的接口与模型配置，通过独立客户端读取完整任务轨迹，提取带任务、事件来源引用的候选。失败每 60 秒重试，重启后继续；中断的提取租约最多 10 分钟后可重新领取。候选在原始证据接收时间之后 30 天没有新证据或晋级则过期，保留来源和原因。提取失败不阻塞前台请求或 Recent 淘汰。

`Workspace` 负责文件访问边界和读写工具，`ToolRuntime` 负责稳定工具注册、动态搜索、权限与执行审计，`ContextBudget` 是窗口、预留与发送上限的唯一来源，`ContextManager` 负责证据索引与预算判定，`CompactionService` 负责压缩（自动触发与 `/compact` 走同一入口、产出相同的状态效果），`SessionStore` 负责会话记录的追加、截断、加锁和加载，`ChatCompletionsClient` 负责兼容接口，`Agent.run_request` 展示完整的模型-工具循环。后续阶段可以在不改动命令行入口的情况下替换搜索、加入记忆或增加其他工具。
