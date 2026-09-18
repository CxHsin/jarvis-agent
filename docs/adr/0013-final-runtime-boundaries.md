# 最终运行时边界与兼容契约

本记录收敛 #32–#44 完成后的公开边界。`Application` 是默认生命周期入口：它装配模型客户端、记忆服务和会话，并在关闭时停止后台 worker、释放会话及客户端。`Agent`、`SessionStore.create` 和 `SessionStore.resume` 仍是兼容入口；它们不会改变旧会话格式，也不会把共享客户端变成共享会话状态。直接使用 `SessionStore` 的宿主必须自行关闭注入的 `MemoryService`，需要完整生命周期时应使用 `Application`。

`Agent` 只负责模型—工具循环、会话消息和交互呈现。配置解析属于 `configuration.py`，工作区边界属于 `Workspace`，工具发现、授权、依赖调度、资源和审计属于 `ToolRuntime`，模型通信属于 `model_client.py`，记忆及其投影属于 `MemoryService`。这些边界是行为契约；将类拆到其他文件不构成验收条件。

兼容性验证覆盖固定 CLI 工具和动态工具搜索、工作区权限策略、旧版本会话迁移、迁移中断恢复、跨工作区共享 Memory、无 embedding 的关键词召回，以及模型和工具客户端的生命周期。旧 JSONL、来源映射和迁移备份始终保留，失败恢复从原始事件和 SQLite 记录重建，不重放可能产生副作用的工具调用。

“中断”表示执行状态未知或持久化尚待重建，不表示工具未执行；只有没有开始工具执行的模型失败才会从活上下文回退。`context_rollback` 只改变可见上下文投影，原始事件、审计、权限状态和来源仍保留。History、Recent、Pending 是可重建投影，不能作为原始事实来源。

shell 在 Windows AppContainer 中通过 PowerShell 启动受限进程；PowerShell provider 对工作区卷根的 `Set-Location`/`Remove-Item` 可能被系统拒绝。受支持的删除和写入使用 .NET 文件 API，测试不会把 provider 命令误报为通用能力。Python bridge 仅承诺经过回归测试的 Windows 参数转义；真实模型服务、硬件掉电持久性和未覆盖的 provider 行为仍是环境限制。
