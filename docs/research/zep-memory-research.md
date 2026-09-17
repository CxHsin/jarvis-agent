# Zep 记忆系统调研（2026-09-16）

## 一手资料
- Zep 官方文档总览：https://help.getzep.com/
- Zep 开源服务（历史版本）：https://github.com/getzep/zep
- Zep Graphiti（时序知识图谱引擎）：https://github.com/getzep/graphiti
- Zep Cloud 记忆概念：https://help.getzep.com/concepts

## 与当前设计的对应关系
Zep 的核心抽象不是固定的 `memory/pending/recent` Markdown 目录，而是 **session + episode/message + derived facts**。消息或外部事件作为 episode 写入；后台流程从 episode 提取实体、关系和事实，并将事实放入时序知识图谱。每条事实带有效时间/失效时间，因此新事实可以使旧事实失效，支持时间点查询与冲突覆盖。

Zep 同时提供会话级摘要（summary）和检索结果拼接，用于构造 prompt。摘要会随着新消息更新，长对话通过摘要/窗口控制 token；原始消息仍保留在存储中，压缩不会删除事实来源。应用通常按最近消息窗口 + 摘要 + 相关事实生成上下文。

Zep 的检索接口面向自然语言 query：服务端对 query 做语义检索并返回相关事实/消息，结果可带时间过滤、分页和相关性分数。Graphiti 文档描述其搜索为关键词（BM25/全文）与向量相似度结合，并可按 RRF 等方式融合；具体权重和实现随部署版本而异，不应假设固定参数。

冷数据策略不是 Zep 的主要用户抽象。原始 episode、节点/边及失效事实通常长期保留，由保留策略或数据库生命周期管理归档；“事实失效”与“物理删除/归档”是两件事。

## 对本项目的建议
1. 将 `recent` 视为可配置消息窗口；窗口溢出时生成摘要并写入 `pending`/候选事实队列。
2. `pending` 不必按固定夜间任务才合并；可在事实达到置信度、重复确认或窗口关闭时异步固化。夜间批处理可作为降成本补偿。
3. 将稳定记忆建模为带 `valid_from/valid_to` 的事实，冲突时结束旧事实有效期，保留来源 episode，避免直接覆盖文本。
4. Prompt 压缩只改变摘要/呈现层，不应改写已固化事实；事实抽取应引用原始消息或不可变 episode。
5. SQLite 可保存 episodes、facts、entities、relations、FTS5（BM25）和 sqlite-vec 向量；混合召回后用 RRF，去重键建议为规范化主语-谓语-宾语+时间区间。
6. Query Rewrite/HyDE 属于应用侧增强：仅在查询较短、歧义高或首轮召回不足时触发，避免每次请求增加延迟和 token 成本。

## 结论
用户的四层设想与 Zep 的“原始事件→派生事实/时序图谱→摘要与检索上下文”高度同构；差异在于 Zep 以事实和时间有效性为中心，而不是以 Markdown 层级为中心。可以保留 Markdown 作为可读导出/缓存层，把 SQLite+向量作为事实索引与状态真源。
