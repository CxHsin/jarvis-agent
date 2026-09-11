不确定需求时先问我

## Agent skills

### Issue tracker
任务和规格使用 GitHub Issues；操作前读取 `docs/agents/issue-tracker.md`。

### Triage labels
分流时使用五个默认标签；映射见 `docs/agents/triage-labels.md`。

### Domain docs
采用 single-context 布局；探索代码前读取 `docs/agents/domain.md`。

## 提交规格

分支与标题遵循 Conventional Commits：分支 `<type>/<描述>`，标题 `<type>(<scope>): <summary>`；scope 取模块名，如 agent、session、context、cache、capacity、docs、tests。

正文写清问题、结果和做法不显然时的原因，篇幅以几段为限，超出说明改动该拆。用 `Fixes #N` 关闭对应 Issue，`Refs #N` 表示仅作背景。

验证段写实际跑过的检查和结果，含测试条数；修复缺陷时附上去掉修复就会失败的用例。

agent 的实质贡献在相应提交加 `Generated-by: <tool>` trailer。人始终是贡献责任人：agent 可以提交和推送，评审与合并由人决定。
