# Jarvis 第一阶段

这是一个可观察的命令行个人 Agent 基线。它支持 OpenAI 兼容的 Chat Completions 接口，并提供三个文件工具：列目录、搜索文本内容、读取文本文件。同一次程序运行会保留对话历史、证据索引和进程内归档，退出后不保存。

## 启动

1. 复制 `.env.example` 为 `.env`，填写 `MODEL` 和 `API_KEY`；`BASE_URL` 可以指向任何兼容 Chat Completions 的服务。可选的 `COMPRESSION_MODEL` 用于上下文摘要，未填写时使用主模型。
2. 确认 `ROOT_DIR` 指向允许 Agent 访问的目录。当前验收目录是 `D:\Course\Study\matt video`。
3. 使用 Python 3.12 创建的项目虚拟环境运行：

```powershell
.\.venv\Scripts\python.exe .\jarvis_agent.py
```

如果尚未创建虚拟环境：

```powershell
& 'C:\Users\Cx\AppData\Local\Programs\Python\Python312\python.exe' -m venv .venv
```

输入 `exit` 退出，`Ctrl+C` 取消当前请求。

## 第一阶段验收

- 不需要文件信息的请求可以直接回答。
- 模型可以连续调用工具，并依据工具结果继续或结束。
- 同一轮的多个工具调用按顺序执行，日志显示工具名、参数和结果。
- 同一次运行可以追问；退出后不会恢复历史。
- 工具失败会作为结果交回模型；模型请求失败会结束当前请求并回到输入状态。
- 达到 `MAX_ROUNDS` 时最后一轮只生成回答，不再执行工具。
- 每次模型请求末尾会附加由代码维护的 `<agent_status>` 状态栏，其中包含任务目标、工具调用、证据索引和上下文预算。
- 设置 `CONTEXT_WINDOW_TOKENS` 后，估算上下文达到 `CONTEXT_COMPRESSION_THRESHOLD`（默认 86%）会批量压缩旧工具结果，目标降至 `CONTEXT_COMPRESSION_TARGET`（默认 65%）。原始结果仅保留在本进程归档，摘要保留来源引用。

`MAX_ROUNDS` 按模型调用次数计数。文件工具只允许访问 `ROOT_DIR` 下的文本文件；目录工具可以列出所有直接子项。上下文窗口未配置时仍会显示本地估算，但不会自动压缩。

## 结构

`Workspace` 负责工作区边界和三个工具，`ContextManager` 负责状态栏、证据索引、预算估算和压缩，`ChatCompletionsClient` 负责兼容接口，`Agent.run_request` 展示完整的模型-工具循环。后续阶段可以在不改动命令行入口的情况下替换搜索、加入记忆或增加其他工具。
