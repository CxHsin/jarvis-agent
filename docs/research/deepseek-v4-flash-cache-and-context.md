# DeepSeek 官方 Flash：缓存与上下文容量

研究日期：2026-09-11。初始研究来源为当日官方文档；后续实施阶段的受控实测记录见文末。

## 模型名称与容量

- 当前推荐模型 ID 为 `deepseek-flash`，对应 DeepSeek-V4.1-Flash。旧名称 `deepseek-v4-flash` 仍被接受，但原模型已退役，请求实际由 V4.1-Flash 服务。因此模型 ID 并不保证永久绑定同一模型版本；本次未更改项目配置。[模型与价格](https://api-docs.deepseek.com/quick_start/pricing)
- 当前官方表列出上下文长度 **1M**、最大输出 **384K**。Chat Completions API 明确 `max_tokens` 上限为 **393216**；默认值依模式而异：非思考 8K、思考 64K、思考且 `reasoning_effort=max` 时 128K。输入与生成 token 总长受上下文长度限制。容量上限不能直接当作每次请求的输出预算。[模型与价格](https://api-docs.deepseek.com/quick_start/pricing)、[Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion)
- `GET /models` 的公开 schema 仅包含模型 `id`、`object`、`owned_by`，不提供窗口或最大输出。不能依靠该接口自动发现容量。以上页面只把窗口写作 1M，此处不擅自认定它等于 1000000 或 1048576。[模型列表](https://api-docs.deepseek.com/api/list-models)

## 前缀缓存与真实指标

- 上下文磁盘缓存对所有用户默认启用，不需要增加缓存开关；命中依赖此前已经持久化的缓存前缀单元完整匹配。请求输入末端、输出末端、共同前缀检测以及长输入/输出的固定 token 间隔均可产生单元。[上下文缓存](https://api-docs.deepseek.com/guides/kv_cache)
- 官方示例：先请求 `A+B`，再请求 `A+C`，第二次不能只凭共有 `A` 保证命中；系统检测并持久化公共单元 `A` 后，第三次 `A+D` 可以命中。缓存构建需要数秒，按 best effort 工作，不保证 100% 命中；不再使用的缓存通常数小时至数天后清除。[上下文缓存](https://api-docs.deepseek.com/guides/kv_cache)
- 服务端 `usage.prompt_cache_hit_tokens` 是输入命中量，`usage.prompt_cache_miss_tokens` 是未命中量；`usage.prompt_tokens` 等于两者之和。`usage.prompt_tokens_details.cached_tokens` 与 hit 字段含义相同，不能重复相加。单次缓存 token 比例可用 `hit / prompt_tokens`，分母为零或字段缺失时应标为不可计算/未知。[Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion)
- 当前流式文档规定最终内容 chunk 携带 usage，位于 `[DONE]` 前；该 chunk 的 choices 非空，具有非空 finish_reason。不能假设 usage 只出现在 choices 为空的专用 chunk。流中断可能无法取得最终统计。[Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion)

## 对 Jarvis 的设计含义（建议，非已实现事实）

1. 按供应商与模型 ID 解析容量；官方目录记录来源和更新时间，允许逐模型配置覆盖，未知容量保留未知。`/models` 用于模型可用性检查，不能补足 DeepSeek 的窗口容量。
2. 从服务端 usage 记录真实缓存用量，同时保留原始总输入量用于上下文预算。缓存命中减少重复计算与计费，不减少该输入在上下文中的占用。
3. 保持系统提示、工具定义和既有消息的前缀稳定；用实测指标观察压缩前后的缓存变化，不承诺第二次调用必命中。
4. 分开处理模型容量、用户选定的使用预算、实际输出预算和安全余量。模型别名发生路由变化时，可更新目录元数据，不应静默改写用户所选模型 ID。

## 未验证事项

官方网页会更新，本笔记是当日快照。尚未压测当前服务端完整容量边界。

## 实施阶段验证

2026-09-11，通过本地配置的官方 DeepSeek 接口和 `deepseek-v4-flash` 发送三次相同的合成文本请求，每次输出上限 64 token、间隔 5 秒，未发送工作区内容。新客户端读取的服务端 usage 如下：

| 调用 | 输入 token | 缓存命中 | 未命中 | 缓存 token 占比 |
| --- | ---: | ---: | ---: | ---: |
| 1 | 1248 | 0 | 1248 | 0% |
| 2 | 1248 | 1024 | 224 | 82.1% |
| 3 | 1248 | 1024 | 224 | 82.1% |

这是合成重复请求的实测，不代表日常工具循环也能达到相同比例。容量目录按官方 V4.1-Flash [模型配置](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash/raw/main/config.json) 的 `text_config.max_position_embeddings=1048576` 解释 API 文档的 1M；模型配置不是对 API 硬边界的实测证明，目录保留来源说明且支持覆盖。
