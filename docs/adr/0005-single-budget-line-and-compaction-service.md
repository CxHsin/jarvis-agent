# 单条预算线与压缩服务

上下文预算收敛为一个预留值：触发条件改为「估算输入 > 窗口 − reserve」，不再叠加「窗口 × 阈值」与按窗口比例计算的安全余量；拒绝发送用同一个值判定，输出上限按剩余空间动态收敛。预留默认 16384 token，输出下限 4096。选择这一方案是因为同一套公式原本写在三处（`ContextManager.input_budget`、`ContextManager.should_compress`、`ChatCompletionsClient.complete`），阈值与预留容易被读成重复留白，而实测显示估算误差的方向和量级与窗口比例无关：同一份合成英文文本，本地估算 1,853,732 token，服务端实际 1,166,687 token，高估 59%；小样本中文高估 12.4%、英文高估 25.7%、JSON 工具结果高估 1.5%，只有 Python 代码低估 8.8%。误差跟随内容类型而不是窗口大小，因此安全余量不应按窗口比例定义；扁平 reserve 只承担压缩触发与拒绝发送之间的最后一道缓冲。

压缩从 `ContextManager` 内部的方法提为服务，对外一个入口，三种触发——自动（预算驱动）、主动（用户命令）、溢出恢复——产出完全一致的状态效果：同一切点规则、同一条累积 checkpoint、同一条 compact 记录与归档标记，只差触发原因。主动压缩跳过触发阈值，但仍受保留窗口与可用输入预算约束，且不构成任务。选择这一方案是因为溢出恢复本身就是第三种触发，把它写成第二份压缩实现会让切点与归档语义分叉。

溢出恢复依赖服务端错误可识别，因此本 ADR 记录受控实测。2026-09-12，`deepseek-v4-flash`，`https://api.deepseek.com`，`max_tokens=1`，messages 为 7,414,292 字符合成英文文本：HTTP 400，响应体 `{"error":{"message":"This model's maximum context length is 1048576 tokens. However, you requested 1166688 tokens (1166687 in the messages, 1 in the completion). Please reduce the length of the messages or completion.","type":"invalid_request_error","param":null,"code":"invalid_request_error"}}`。三点结论：窗口值是 1048576，与能力目录一致；`type` 与 `code` 都只是 `invalid_request_error`，与 `max_tokens` 越界（`Invalid max_tokens value, the valid range of max_tokens is [1, 393216]`）同类，恢复路径只能匹配 message 文本，不能只看错误码；服务端按「messages + completion」之和判定，输出预留确实占用窗口。同一实测还显示拒绝耗时 72.4 秒，长于默认 `REQUEST_TIMEOUT=60`，因此只按超时无法区分溢出，识别失败时必须退回「拒绝发送」的保守行为而不是猜测重试。另有一次 976,579 token 的越界前请求被接受（HTTP 200），说明能力目录的数值是配置事实而非服务端边界，与 ADR 0002 的判断一致。

重试会改写历史前缀，属于潜在的缓存失效事件，一次溢出最多重试一次，避免拿旧错误重复压缩。本 ADR 替代 ADR 0002 中「预计输入 + 输出预留 + 安全余量 > 窗口 × 阈值」的触发公式与 2% 安全余量，保留其按供应商与模型维护容量、未知不猜、输入与输出分别定义的部分。
