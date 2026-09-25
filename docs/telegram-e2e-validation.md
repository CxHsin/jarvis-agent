# Telegram 渠道实机验收（2026-09-25，Windows）

在独立测试 Bot 和单个授权私聊上测试本地 `feat/telegram-channel-entry`；不记录 Token、用户 ID、会话标识或私聊原文。工作区内 `.env` 采用 [ADR 0016](adr/0016-local-trust-for-workspace-credentials.md) 的本地信任取舍，**不提供工作区凭据隔离保证**。

- `getMe` 返回成功，确认独立 Bot 可连接；`getWebhookInfo` 显示无 webhook。
- 首轮真实文本请求由模型处理并回传，用户确认手机端收到；随后新的无工具文本请求收到即时回执与正确回复，频道记录显示 `completed/notified`，进程继续运行。
- 首轮真实 `/cancel` 使 `bash` 返回 `cancelled`，但暴露了 Windows AppContainer 执行时 `telegram-state.json` 原子替换遭 `WinError 5`；Bot 退出，**首轮不计通过**。
- 修复见 `e4949d9`：频道状态使用追加、换行结束并 fsync 的快照；实际在受限 shell 运行期间执行状态提交成功，Windows 回归测试可复现旧故障。
- 修复后用户再次在独立 Bot 发起 `Start-Sleep -Seconds 30` 并发送 `/cancel`：收到取消请求回执与任务取消通知；本地工具审计为 `cancelled`，没有“等待结束”的成功回复，频道分别记录任务 `unknown`（副作用状态不自动重放）和取消消息 `completed/notified`，Bot 进程保持运行，日志没有新 Traceback、发送失败或已知 Token 明文。
- 一次后续回执缺失诊断中，人工探测曾错误地推进本地 polling offset；已停止进程并核对后重新启动，**不将该次消息计入通过**。重新发送的无工具新消息再次完成往返。不得把无更新时的偏移推进当作正常生产诊断步骤。

自动化复验：`python -m pytest -q -rs`：**196 passed, 1 skipped**（本机无法创建文件符号链接）；新增的真实 AppContainer 并发状态提交用例通过。仍未证明文件检查后使用竞争完全消除，也未证明所有外网协议/地址均受隔离。真实 Bot 过程属于人工验收，不代替这些边界测试。
