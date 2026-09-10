# Domain docs

采用 single-context 布局：
- 根目录 CONTEXT.md：领域术语和模型。
- docs/adr/：架构决策记录。

探索代码前，读取 CONTEXT.md 和与当前工作相关的 ADR。
如果以后存在 CONTEXT-MAP.md，则按其指引读取相关上下文。

文档不存在时继续工作；由 domain-modeling 技能在术语或
决策明确后按需创建。

任务、设计、假设和测试中的领域概念使用 CONTEXT.md 定义的术语。
发现术语缺口时，记录供 domain-modeling 完善。
提议与已有 ADR 冲突时，明确指出相关 ADR 和重新考虑的理由。
