# Jarvis 第一阶段

在 `.env` 中设置 `SYSTEM_PROMPT="你是 Jarvis，请用中文简洁回答。"` 可替换内置角色与回答风格提示。使用单行文本，留空沿用默认提示词，修改后重启生效；同名进程环境变量优先。工作区信息、工具协议及 self.md / memory.md 记忆前缀仍由程序附加。

这是一个可观察的命令行个人 Agent 基线。它支持 OpenAI 兼容的 Chat Completions 接口，并提供五个稳定工具：`read`、`edit`、`bash`、`tool_search`、`list_directory`，同时保留旧文件工具名称作为兼容别名。对话历史写入状态目录下的会话记录，每次启动默认新建会话，用 `--resume` 显式接上上一段。

`bash` 实际使用 Windows AppContainer 中的 `PowerShell`，不提供 Bash 语法。列目录使用 `list_directory`（可逐层查看子目录），读取文件使用 `read`，避免依赖沙箱内可能拒绝访问的 `dir`。在 AppContainer 中，PowerShell provider 对卷根和部分绝对路径的 `Set-Location`/`Remove-Item` 可能被拒绝；需要删除或写入时使用 .NET 文件 API（例如 `[IO.File]::Delete()`、`[IO.File]::WriteAllText()`）。Python 命令由受控的 .NET child process bridge 启动，支持带空格、Unicode、引号、空参数和尾反斜杠的参数；其它原生命令仍受 PowerShell resolver 和 AppContainer 限制。Shell 输出优先按 UTF-8 解码，无法解码时使用 Windows OEM 编码；失败预览包含工具名、退出码和命令输出。

Memory tools are discovered with `tool_search`: `memory_search` and `memory_manage` (`remember`, `correct`, `forget`). Retrieval reads `STATE_DIR/memory/memory.db` through SQLite FTS5 and sqlite-vec; it never reads `memory.md`. Configure an OpenAI-compatible embedding endpoint with `EMBEDDING_BASE_URL`, `EMBEDDING_API_KEY`, `EMBEDDING_MODEL`, and `EMBEDDING_DIMENSIONS`. Without these settings, FTS5 results remain available and the response reports vector retrieval as unavailable. Direct `edit` calls are blocked for a configured in-workspace memory directory; a generic shell remains able to mutate files, so this is a runtime guard rather than a filesystem sandbox. Query rewrites and HyDE passages are ephemeral and are never stored as Memory facts.

`memory_manage` separately verifies the proposed change against the current original user message using the configured chat model; mentioning information alone is not an explicit remember request. Ambiguous, unavailable or malformed authorization results reject the write. This semantic check is model-dependent. Memory writes also honor runtime cancellation and permission revocation. Retrieval returns at most eight whole facts with provenance, bounded to an estimated 1,000 tokens (UTF-8 bytes / 4); oversized facts are omitted rather than truncated. Fact and keyword-index writes commit together. Embedding failures preserve facts; the application-owned background worker retries missing vectors, including after restart, without making searches wait for a whole-library build. Existing matching vectors continue to participate during backfill.

Embedding is optional: all four `EMBEDDING_*` settings above must be nonblank before the HTTP adapter is enabled; dimensions must be an integer from 1 to 65536. No provider, model, dimension or key is selected by default. Injected embedding clients supply their own transport/authentication but still require a model identifier and dimensions. `memory_search.vector_status` (also `MemoryService.vector_status()`) reports `disabled`, `incomplete`, `unavailable` (vector extension unavailable), `backfilling`, `retrying`, or `ready`, plus missing configuration names and ready/pending counts. `vector_available` describes usable vector recall in that response; `vector_failed` reports query or background failure, so partial backfill failures can coexist with usable vectors. Query rewriting and BM25 remain available without embedding; HyDE runs only with usable vectors and a successful first vector pass. Query embedding still waits for its own provider request/timeout, but never performs fact backfill. Standalone `MemoryService` hosts explicitly call `start_worker(client)` (or `start_worker(None)` for vectors alone) and `close(wait=True)`; `Application` manages these automatically. Shutdown waits for in-flight calls, whose results are discarded after stop.

Profile publication uses a durable SQLite outbox: fact IDs and profile versions commit before `memory.md` is replaced. Interrupted writes recover at startup or the next task. If a newer human edit conflicts with an interrupted publication, it is preserved and reported: save that edit separately, restore the committed content returned by `MemoryService.profile_snapshot()`, then reapply the edit against those committed IDs.

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
- `read` 与兼容工具 `read_file` 的成功读取均记录文件及实际返回行号；截断结果只引用已返回范围，重复读取复用同一证据标识，失败结果不产生证据引用。证据索引在压缩和会话恢复后保留，原始结果仍在会话归档中。
- 同一轮的独立工具可并行；依赖调用、同文件操作和声明串行的工具顺序执行，结果按调度顺序记录。
- 同一次运行可以追问；退出默认开启新会话，`--resume` 才会接上上一段。
- 工具失败会作为结果交回模型；模型请求失败会结束当前请求并回到输入状态。
- 达到 `MAX_ROUNDS` 时，最后一轮结合已有上下文和工具结果直接回答，说明未完成或无法确认的部分，不再调用工具。
- 模型请求不再注入框架生成的状态栏；工作目录等静态信息保留在系统提示中，证据索引和上下文预算由代码在进程内维护。
- Jarvis 按接口地址和模型精确匹配容量。`CONTEXT_WINDOW_TOKENS` 显式覆盖优先，其次是自定义/内置模型目录，目录未命中时尝试上游模型元数据；仍未知时要求补充配置后启动，不套用其他模型的容量。
- `估计输入 > 窗口 − CONTEXT_RESERVE_TOKENS` 时触发压缩：从切点开始把更旧的完整 turns（含助手正文）压成一条累积 checkpoint，切点之后的近期原文保留。切点优先落在任务边界（user），任务内部退到轮边界（assistant），tool 结果不单独切断。压缩写进会话记录，原始结果保留在会话归档中，摘要保留来源引用。
- 压缩有三种触发，走同一个服务、产出相同的状态效果：预算驱动的自动压缩、用户输入的 `/compact`、以及服务端报告上下文超限后的溢出恢复。`/compact` 不带参数时退休比当前任务更旧的全部原文，带 token 数时按该数字保留最近内容（仍只落在任务或轮边界上）。溢出恢复只重试一次；服务端错误无法稳定识别时退回拒绝发送。
- 每次模型调用显示服务端输入 token、缓存命中 token 和占比；任务结束后按主模型/压缩模型分别汇总。缺失或不一致的缓存字段标为“未知”，明确返回 0 才算未命中；总占比按总命中量除以总输入量计算，存在未知调用时不展示误导性的完整占比。
- 终端默认只显示工具结果摘要和短预览；完整结果仍发送给模型。设置 `VERBOSE_TOOL_OUTPUT=true` 可临时显示完整工具结果，`TOOL_OUTPUT_PREVIEW_CHARS` 控制预览长度。

`MAX_ROUNDS` 按主模型调用次数计数。`read` 和 `read_file` 支持读取工作区外的文本文件：绝对路径直接解析，相对路径以 `ROOT_DIR` 为基准。`edit`、`list_directory` 和 `search_file_content` 限制在 `ROOT_DIR` 内；`bash` 以该目录为起点，但没有操作系统沙箱，获准的命令能访问进程有权限访问的位置。

## 工具运行时

固定工具为 `read`、`edit`、`bash`、`tool_search`。动态工具通过 `tool_search` 搜索并激活，定义追加在搜索结果中，固定 `tools` 列表不改变；下一任务必须重新搜索，历史定义保留但不授予执行权限。压缩退休搜索结果时，仅补回当前任务已激活的定义，不插入空标记或任务边界标记。这里保证确定性序列化，不承诺供应商一定命中缓存。

`TOOL_PERMISSION_MODE` 支持 `approve-all`（每次工具调用确认）、`approve-dangerous`（默认，高风险调用确认）和 `broad-access`（显式宽授权）。`bash` 一律视为高风险；文件编辑为中风险。命令行遇到需确认的操作会提示，非交互使用应传入 `Agent(confirm_tool=...)`，否则返回 `confirmation_required`。工具本身不能确认或升级权限。`agent.tool_runtime.policy.change_mode(...)` 可直接收紧，升级必须由宿主完成用户确认后传入 `confirmed=True`；`revoke()` 阻止新的副作用，变更与确认均写入会话审计。恢复已有会话时，启动配置只能进一步收紧已保存的授权。

命令行使用 `/permissions` 查看当前全局默认值；`/all`、`/safe` 和 `/wide` 分别保存 `approve-all`、`approve-dangerous` 和 `broad-access`，后续重启及其他工作区都会读取。升级权限会再次确认。设置保存在 `STATE_DIR/settings.json`；进程环境变量 `TOOL_PERMISSION_MODE` 存在时优先，用于部署时强制指定模式。

每个工具版本保存不可变 schema、SHA-256 指纹、风险、资源、副作用、超时、输出上限与并发声明。同版本不能替换处理函数。参数按 JSON Schema 2020-12 校验，远程 schema 引用不受支持。`TOOL_MAX_TIMEOUT` 是会话超时上限，工具自身的上限仍生效；输出同时受工具和权限策略上限约束。内置 shell 的超时与取消会终止进程树，文件编辑在提交前检查取消与策略版本。Python 扩展处理函数应使用 `contextual=True` 和 `ExecutionContext` 合作取消；无法中止的扩展返回 `uncertain` 并保留资源占用，运行时不会自动重试可能产生副作用的调用。

同批调用可在参数中使用 `_depends_on: ["call-id"]` 声明依赖，或使用 `{"$result":{"call_id":"call-id","path":["字段"]}}` 引用前置结果；引用会在参数校验前解析。前置失败会阻止依赖调用。`_version` 与 `_schema_fingerprint` 可显式校验搜索得到的版本；不存在的版本、环、冲突及取消均返回结构化错误。`read` 返回文件 `hash`，`edit(expected_hash=...)` 可拒绝过期写入；未指定时，同批编辑也会比较执行前快照，避免两个同文件写入静默覆盖。

供应商模式由 `PROVIDER_TOOL_MODE` 显式选择，与模型容量配置无关。通用 Chat Completions 客户端使用 `emulated`。原生供应商由宿主传入 `Agent(native_loader=...)`，协议为 `(measured_client, messages, stable_tools, active_definitions, tool_choice) -> assistant_message`；适配器负责供应商专有的 deferred/tool-reference 请求。可重试的网络/能力错误连续失败三次后，本会话只回退一次并重新发出 emulated 请求；失败调用的局部引用不会写入历史。模式、回退次数、历史版本与权限随会话恢复。没有原生适配器时配置 `native` 会按相同规则回退。

工具执行以 `ToolRuntime` 为边界：`register` / `install_tools` 管理版本注册，`begin_task` / `discover` 管理当前任务激活，`execute_batch` 统一参数协议、授权、依赖调度、工作区资源、编辑快照、结果转换与审计。Agent 只提交模型调用，并通过结果回调写入会话消息和上下文证据，不访问 registry/dispatcher 私有状态。结果在每个依赖波次内按请求顺序记录；执行审计保留实际发生顺序，不能把并行执行时间顺序当成模型结果顺序。

宿主可通过 `agent.tool_runtime.register(...)` 注册扩展；原有 `agent.tool_registry.register(...)` 保留兼容。注入 `Agent(tool_runtime=...)` 时保留运行时的确认回调与资源解析限制，显式 `confirm_tool` 可替换确认回调；工作区路径资源与宿主资源共同约束执行。会话通过 `configure` 接管审计持久化，运行时实例属于该会话，不应同时注入多个会话；共享不可变定义可使用同一 registry，其执行租约在超时后仍共享。模型协议、固定工具与兼容别名保持不变。

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

个人历史位于跨工作区共享的 `STATE_DIR/memory/`：`history/YYYY-MM-DD.md` 从原始任务事件重建，仅呈现用户输入、事件时间、UTC 记录时间和任务/事件来源标识；未知事件时间为空，不以接收时间代替。新会话以 `sessions/<工作区>/<会话标识>.jsonl` 作为消息、任务、工具结果与审计的共同原始记录；每条事件带版本、稳定身份、会话与任务关联、顺序和时间，写穿落盘后再更新派生视图。首次 `--resume` 旧会话会自动迁移，原文件与 `memory/trajectories/<会话标识>.jsonl` 原样保留；新记录及来源映射位于同工作区会话目录的 `migrations/<会话标识>/`。模型失败回滚及上下文压缩不会删除新会话的原始证据。`recent/<会话标识>/recent.md` 每条消息后更新，保存在途任务和最近 `RECENT_TASK_COUNT` 个已结束任务（默认 5）。失败且没有工具执行的请求保留审计记录，但不占 Recent 窗口。恢复使用同一会话的窗口；新会话独立开始。 History/Recent 的删除或编辑不会改变原始证据；恢复或 `MemoryService.rebuild_projections()` 会重建这些派生文件，并幂等补入已移出窗口的 completed 任务。后台启动也扫描其他会话的统一记录以补回中断的入队。已提交提取结果不会因重复投影或 Pending 文件发布失败再次提取；未提交结果的失败调用仍可重试。Recent 上下文切换只引用原始消息事件，已提交 checkpoint 始终优先。

下一任务从 Recent 的完整消息建立上下文，并经过既有压缩与 token 硬上限检查；可读 Recent 文件和原始轨迹保持完整，即使模型请求需要压缩。Recent 是可重建视图，不能通过编辑它改变原始记录。

`STATE_DIR/memory/self.md` 是用户维护的 Agent 身份、原则、能力和工具约束；只在初次创建时提供默认内容，不从经历自动生成。`memory.md` 是有效用户事实的画像投影，六个固定栏目为 `identity`、`work_preferences`、`communication`、`long_term_goals`、`constraints` 和 `current_state`，空栏目省略。画像按 UTF-8 字节数除以 4 的估算限制在 1,500 token 内；超额事实仍在数据库保留，不截断事实行。自动提取批次完成、夜间 consolidation 完成或用户修正后更新画像；夜间调度为本地凌晨 03:00，错过会补跑。相同内容不新增版本，SQLite 的 `profile_versions` 保留内容、来源事实 ID、token 估算及创建/激活时间。每个任务开始时一次性读取 self 和画像，任务内所有模型轮次使用相同前缀，新版本下个任务生效。召回只查数据库，不使用 Markdown。

人工修改 `memory.md` 时保留标题及栏目，每行采用 `- {"fact_id":"原 ID","predicate":"原属性","object":"新值"}`。现有行只修改 `object`；删除整行表示忘记该事实。新增行使用 `"fact_id":null`，填写明确的属性及值。下一任务前整份校验并作为高置信度用户修正导入，旧事实失效但不删除，完整编辑原文保存在数据库来源记录。格式错误、未知 ID 或修改既有属性/栏目会明确报错，保留文件且不部分导入；后台更新不会覆盖尚未导入的人工修改。此结构化格式避免用字符串猜测用户修正的语义。

完成任务移出 Recent 时，会立即在 `memory.db` 创建幂等 Pending 批次；`pending.md` 是批次和候选的可读视图，不进入模型上下文，也不复制原始对话。后台提取使用主模型的接口与模型配置，通过独立客户端读取完整任务轨迹，提取带任务、事件来源引用的候选。失败每 60 秒重试，重启后继续；中断的提取租约最多 10 分钟后可重新领取。候选在原始证据接收时间之后 30 天没有新证据或晋级则过期，保留来源和原因。提取失败不阻塞前台请求或 Recent 淘汰。

`configuration.py` 负责环境配置和全局权限默认值，`model_client.py` 负责模型通信与容量解析；原来的 `jarvis_agent.Config`、`ChatCompletionsClient`、异常类型和 CLI 入口继续可用。`Application` 装配客户端与一个共享个人记忆服务，后台提取随应用启动首个会话而运行，关闭单个会话只释放该会话记录与锁。应用退出先关闭全部会话，再停止并等待记忆后台调用返回，把未完成批次留作可恢复状态，最后关闭各客户端；同一注入客户端只关闭一次。内置网络客户端受请求超时限制，宿主注入客户端也须保证调用最终返回。

多会话宿主使用同一个应用：

```python
from jarvis_agent import Config
from application import Application

with Application(Config.from_env(), client=model_client) as app:
    first = app.create_session()
    second = app.create_session()
    first.run_request("第一段对话")
    first.close()
    second.run_request("第二段对话仍可访问共享记忆")
```

`Application` 接受主模型、压缩、提取、embedding、查询改写及记忆授权客户端；`create_session` 接受既有工具运行时、权限策略、确认回调与原生供应商宿主注入。消息、Recent、上下文、权限与请求计量属于会话，不能通过共享模型客户端把其他会话消息混入请求。`Agent(config, ...)` 仍可直接使用，它为兼容调用创建独立应用并在 `Agent.close()` 时关闭。`SessionStore.create/resume` 的独立调用保留 `store.memory` 访问，由装配层提供无后台线程的记忆服务；`SessionStore.close()` 不负责关闭记忆。手动启动记忆 worker 的旧宿主须显式关闭它，或改用 `Application` 管理。旧磁盘格式仍可读取，会话跨进程锁与数据库并发保护保持不变；新会话事件格式见 [ADR 0008](docs/adr/0008-unified-session-events.md)。

`Workspace` 负责文件访问边界和读写工具，`ToolRuntime` 负责稳定工具注册、动态搜索、权限与执行审计，`ContextBudget` 是窗口、预留与发送上限的唯一来源，`ContextManager` 负责证据索引与预算判定，`CompactionService` 负责压缩（自动触发与 `/compact` 走同一入口、产出相同的状态效果），`SessionStore` 负责会话记录的追加、加锁和加载（旧格式保留兼容截断路径），`ChatCompletionsClient` 负责兼容接口，`Agent.run_request` 展示完整的模型-工具循环。后续阶段可以在不改动命令行入口的情况下替换搜索、加入记忆或增加其他工具。


### 旧数据自动迁移与失败恢复

沿用 `--resume [会话标识]`，无需手工导入。首次恢复旧会话先创建不可覆盖的 `migrations/<会话标识>/backup/`（会话、轨迹、Memory DB），验证恢复上下文、事件身份及来源后发布 `committed.json` 切换到 `events.jsonl`。记忆服务启动前另把现存数据库与人工 Markdown 保存到 `STATE_DIR/migrations/legacy-memory/`；数据库通过 SQLite 在线备份保护 WAL 中的已提交数据，事实不会重新提取。原有记忆来源路径仍保留，来源映射及冲突详情可在提交报告中核对。

进程中断或磁盘写入失败后，修复存储问题并再次运行相同 `--resume` 即可。提交前旧会话仍可列出、原文件可读取；原文件若在备份后被其他旧程序修改，迁移会拒绝切换以避免混合不同快照，此时保留整个状态目录交由人工核对。不要删除或覆盖备份。提交后若报告派生视图失败，新事件已经提交，再次恢复只重建视图；不要将旧日志覆盖回新事件。冲突保留双方并在恢复时报告；尾部半条记录保存在原文件和备份，中间损坏记录需人工核对后再重试。详情见 [ADR 0010](docs/adr/0010-legacy-session-migration.md)。
