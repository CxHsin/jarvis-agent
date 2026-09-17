# Agent Tool Call 工程实践调研

## 核心结论
Tool call 看似是“模型输出 JSON→执行函数→回传结果”，工程质量取决于契约、状态机、失败恢复和可观测性。建议把每次调用建模为可追踪的 command：`proposed → validated → authorized → running → succeeded/failed/cancelled`，并将模型决策与工具副作用隔离。

## 1. Schema 与契约
- 使用 JSON Schema 描述参数，字段尽量小而明确；服务端必须再次校验，不能信任模型生成的 JSON。OpenAI Structured Outputs 可令参数匹配 schema（仍需处理业务语义错误）。[OpenAI Structured Outputs](https://platform.openai.com/docs/guides/structured-outputs)
- Anthropic 建议在 `tools` 中提供清晰 name/description/input_schema，并在结果中返回 `tool_result`；错误也应作为结果反馈给模型以便恢复。[Anthropic Tool Use](https://docs.anthropic.com/en/docs/build-with-claude/tool-use)
- 工具版本化（name 或 schema version），保持向后兼容；拒绝未知字段或明确采用扩展字段策略。

## 2. 执行循环与失败处理
- 采用有限步数/总时限，防止模型陷入循环；每轮记录 tool_call_id、输入、输出和状态。
- 将错误分类为参数错误（可让模型修正）、瞬时错误（重试/退避）、权限错误（停止并请求授权）、业务拒绝（直接反馈）。不要把异常堆栈原样暴露给模型。
- OpenAI Responses API 的工具调用是多轮过程：模型返回 function call，应用执行后用 `function_call_output` 回传，再继续生成。[OpenAI Function Calling](https://platform.openai.com/docs/guides/function-calling)

## 3. 并发、幂等与副作用
- 只并行执行互不依赖、且无冲突副作用的调用；依赖关系由 call id/显式 DAG 表达。LangChain 对并行 tool calls 提供统一抽象，但应用仍需处理竞争与顺序。[LangChain Tool Calling](https://python.langchain.com/docs/concepts/tool_calling/)
- 为写操作设置 idempotency key（通常使用 conversation/run/tool_call_id 的稳定哈希），服务端去重；重试只对已知安全操作启用。
- 读操作可缓存并设置 TTL；写操作采用事务、幂等 upsert 或 outbox，避免模型重复调用造成重复扣款/发送。

## 4. 授权与安全
- 工具按最小权限拆分；高风险操作（删除、转账、外发消息）在执行前要求人工确认或策略引擎批准。OpenAI Agents SDK 将 guardrails、human-in-the-loop 作为独立机制。[OpenAI Agents SDK](https://openai.github.io/openai-agents-python/guardrails/)
- 将用户内容、工具输出视为不可信输入，防御 prompt injection；工具输出做长度限制、脱敏和内容类型校验。Anthropic 的安全最佳实践强调对工具输入/输出实施验证。[Anthropic Building Effective Agents](https://www.anthropic.com/research/building-effective-agents)
- 凭据只在执行器侧注入，模型永不直接读取；网络、文件系统、SQL 均采用 allowlist 与沙箱。

## 5. 可观测性
- 每次 run 生成 trace/span，关联 model turn、tool call、重试、审批和最终结果；记录 latency、token、错误类型、成本、重试次数。
- OpenTelemetry GenAI 语义约定提供 agent/tool span 的通用字段，便于跨供应商分析。[OpenTelemetry GenAI](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
- 生产日志需哈希或截断敏感参数；保留可重放的结构化事件，支持审计与故障复盘。

## 6. 评估与测试
- 用固定任务集覆盖：正确选工具、参数边界、错误恢复、重复调用、越权尝试和长链路超时。评分同时看任务成功率、工具准确率、无效调用率、p95 延迟和成本。
- OpenAI Evals 提供可重复评测框架；Anthropic 建议用真实失败样本持续扩充 eval 集。[OpenAI Evals](https://github.com/openai/evals) · [Anthropic Building Effective Agents](https://www.anthropic.com/research/building-effective-agents)
- 对副作用工具使用 dry-run/fake executor；在集成环境验证幂等和取消语义。

## 推荐最小架构
`ModelGateway`（schema/预算）→ `Policy`（权限/审批）→ `ToolExecutor`（超时、重试、幂等）→ `EventSink`（trace/audit）。工具注册表保存 schema、风险级别、超时、重试和幂等策略；执行器只接受已校验的结构化参数。

## 参考资料
见各节内链接；优先采用官方 API 文档与官方工程文章，链接访问日期 2026-09-14。
