# Issue tracker: GitHub

任务和规格存放在 GitHub Issues，使用 gh CLI 操作。

## 仓库定位
通过 git remote -v 确定目标仓库。
当前尚未配置远程仓库；首次操作前确认 owner/repo，
并使用 --repo owner/repo 显式指定目标。

## 操作约定
- 发布任务或规格：gh issue create。
- 读取任务：gh issue view <number> --comments，同时检查标签。
- 查询任务：gh issue list，按状态和标签筛选。
- 添加评论：gh issue comment <number>。
- 添加或移除标签：gh issue edit <number> --add-label / --remove-label。
- 关闭任务：gh issue close <number>。
- 多行正文写入临时文件，通过 --body-file 传入。
- 发送评论等对外消息前，确认已有用户明确授权。

## Pull requests as a triage surface
PRs as a request surface: no.

## Wayfinding
使用带 wayfinder:map 标签的 Issue 记录总览和决策。
子任务通过 GitHub sub-issues 关联；不可用时，在总览中使用
任务列表，并在子任务正文注明 Part of #<map>。
子任务使用 wayfinder:research、wayfinder:prototype、
wayfinder:grilling 或 wayfinder:task 标签。

优先使用 GitHub 原生 Issue dependencies 记录阻塞关系；
不可用时，在正文记录 Blocked by: #<number>。
所有阻塞任务关闭后，任务才可开始。
按总览顺序选择未关闭、无阻塞且无人认领的子任务。
认领时分配给当前开发者；完成后记录结果、关闭任务，
并将结果摘要和链接补充到总览。
