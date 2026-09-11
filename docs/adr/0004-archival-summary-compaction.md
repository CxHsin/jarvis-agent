# 分层压缩：Pi 式切点、累积 checkpoint 与 span 重放

上下文压缩从「只压旧 tool 消息的单条 squash」改为「按切点收回完整 turns，产出累积 checkpoint，并把被收走的 span 作为 compact 记录持久化重放」。触发公式保留（预计输入 + 输出预留 + 安全余量 > 窗口 × 阈值），但压缩机制改为 Pi 式：切点优先落在任务边界、任务内部退到轮边界、tool 结果不单独切断；保留窗口由 `CONTEXT_KEEP_RECENT_TOKENS` 控制；第二次压缩把上一次 summary 作为上下文传入，活上下文只保留最新 checkpoint。

选择这一方案是因为旧的 tool-only squash 无法覆盖助手正文、摘要不能再次归纳，README 已承认「无法保证所有历史都能压至目标」；Pi 的「增量输入 + 累积输出」在缓存破坏局部化和状态连续性之间取得折中，代价是 turn 级细节被折进 checkpoint，需要回看时从会话归档取原文。原 `CONTEXT_COMPRESSION_TARGET` 被 `CONTEXT_KEEP_RECENT_TOKENS` 取代。
