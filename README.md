# Jarvis 第一阶段

这是一个可观察的命令行个人 Agent 基线。它支持 OpenAI 兼容的 Chat Completions 接口，并提供三个文件工具：列目录、搜索文本内容、读取文本文件。对话历史写入状态目录下的会话记录，每次启动默认新建会话，用 `--resume` 显式接上上一段。

## 启动

1. 复制 `.env.example` 为 `.env`，填写 `MODEL` 和 `API_KEY`；`BASE_URL` 可以指向任何兼容 Chat Completions 的服务。可选的 `COMPRESSION_MODEL` 用于上下文摘要，未填写时使用主模型。
2. 确认 `ROOT_DIR` 指向允许 Agent 访问的目录。当前验收目录是 `D:\Course\Study\matt video`。会话记录默认写到系统用户状态目录（Windows 为 `%LOCALAPPDATA%\jarvis`），用 `STATE_DIR` 指定其他位置；它放在工作区之外，避免被文件工具读到。
3. 使用 Python 3.12 创建的项目虚拟环境运行：

```powershell
.\.venv\Scripts\python.exe .\jarvis_agent.py
```

如果尚未创建虚拟环境：

```powershell
& 'C:\Users\Cx\AppData\Local\Programs\Python\Python312\python.exe' -m venv .venv
```

输入 `exit` 退出，`Ctrl+C` 取消当前请求。`--list` 列出当前工作区的会话，`--resume` 接上最近一段，`--resume <id>` 接上指定会话。

## 第一阶段验收

- 不需要文件信息的请求可以直接回答。
- 模型可以连续调用工具，并依据工具结果继续或结束。
- 每次启动默认新建会话，`--list` 能列出本工作区的全部会话；`--resume` 接上最近一段（`--resume <id>` 指定）。
- 会话记录写穿落盘：进程被杀后 `--resume` 仍能接上，缺失结果的工具调用补写「中断未执行」占位而不重新执行。
- 模型请求失败时，磁盘与内存一起回滚，不留下半轮记录。
- 压缩过的历史段落恢复后，仍能从会话归档取回原文，摘要里的来源引用保持可核对。
- 同一轮的多个工具调用按顺序执行，日志显示工具名、参数和结果。
- 同一次运行可以追问；退出默认开启新会话，`--resume` 才会接上上一段。
- 工具失败会作为结果交回模型；模型请求失败会结束当前请求并回到输入状态。
- 达到 `MAX_ROUNDS` 时最后一轮只生成回答，不再执行工具。
- 模型请求不再注入框架生成的状态栏；工作目录等静态信息保留在系统提示中，证据索引和上下文预算由代码在进程内维护。
- Jarvis 按接口地址和模型精确匹配容量。`CONTEXT_WINDOW_TOKENS` 显式覆盖优先，其次是自定义/内置模型目录，目录未命中时尝试上游模型元数据；仍未知时要求补充配置后启动，不套用其他模型的容量。
- `预计输入 + 输出预留 + 安全余量 > 窗口 × CONTEXT_COMPRESSION_THRESHOLD` 时触发压缩：从切点开始把更旧的完整 turns（含助手正文）压成一条累积 checkpoint，切点之后的近期原文保留。切点优先落在任务边界（user），任务内部退到轮边界（assistant），tool 结果不单独切断。压缩写进会话记录，原始结果保留在会话归档中，摘要保留来源引用。
- 每次模型调用显示服务端输入 token、缓存命中 token 和占比；任务结束后按主模型/压缩模型分别汇总。缺失或不一致的缓存字段标为“未知”，明确返回 0 才算未命中；总占比按总命中量除以总输入量计算，存在未知调用时不展示误导性的完整占比。
- 终端默认只显示工具结果摘要和短预览；完整结果仍发送给模型。设置 `VERBOSE_TOOL_OUTPUT=true` 可临时显示完整工具结果，`TOOL_OUTPUT_PREVIEW_CHARS` 控制预览长度。

`MAX_ROUNDS` 按主模型调用次数计数。文件工具只允许访问 `ROOT_DIR` 下的文本文件；目录工具可以列出所有直接子项。

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
- `CONTEXT_SAFETY_MARGIN=0.02`：窗口的 2%，向上取整；用于吸收估算误差，不保证估算始终准确。
- `CONTEXT_COMPRESSION_THRESHOLD=0.90`：含预留的压缩触发线。
- `CONTEXT_RESERVE_TOKENS`：留空时沿用上面的触发线与安全余量；设置后触发线与发送上限重合为一个预留值，输出上限按剩余空间收敛。
- `CONTEXT_KEEP_RECENT_TOKENS=20000`：压缩后保留原文的软目标（绝对 token）；吸附到合法边界时允许略超。
- `CONTEXT_COMPACTION_FAILURE_LIMIT=3`：连续摘要失败达到该次数后熔断，停止重试。
- `COMPRESSION_CONTEXT_WINDOW_TOKENS`、`COMPRESSION_MAX_OUTPUT_TOKENS`：单独覆盖压缩模型。不同压缩模型单独解析容量，不继承主模型的窗口。小窗口模型需要相应降低输出上限；输出预留或保留窗口无法容纳的配置会在启动时报错。

可用输入预算是窗口扣除输出预留和安全余量，并受服务输入上限约束。预算估算以最近一次实际输入 usage 为基础，加上新增内容的估算；历史被改写、工具定义改变或模型改变后回退到本地估算。缓存 token 仍然占窗口。压缩按切点收回旧历史段落、生成结构化 checkpoint，并保留最近窗口原文；压缩后仍超过触发线会提示，超过可用输入预算会拒绝发送，避免已知超限请求。压缩请求本身也做预算检查，失败时使用本地降级摘要，连续失败触发熔断器。

DeepSeek 缓存默认开启。Jarvis 读取 `prompt_cache_hit_tokens`、`prompt_cache_miss_tokens`，并兼容 `prompt_tokens_details.cached_tokens`，不重复相加。缓存构建和命中由服务端决定，重复请求不保证命中。指标不会添加到模型历史中；当前任务原始 usage 保存在进程内 `Agent.usage_ledger.records`，下个任务开始清空。

## 结构

`Workspace` 负责工作区边界和三个工具，`ContextManager` 负责证据索引、预算估算和压缩，`SessionStore` 负责会话记录的追加、截断、加锁和加载，`ChatCompletionsClient` 负责兼容接口，`Agent.run_request` 展示完整的模型-工具循环。后续阶段可以在不改动命令行入口的情况下替换搜索、加入记忆或增加其他工具。
