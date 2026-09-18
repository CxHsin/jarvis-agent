# #43 性能对照结论

本结论复用 #42 修复后的 `benchmarks/baseline.py`（数据集版本 2），在同一合成数据、临时 SQLite、受控 embedding client、固定重复次数和目标提交上测量。脚本记录实际 embedding 调用、向量可用性及 History 投影维护耗时；不再报告推断的扫描计数。

| 操作 | 观察结果 | 结论 |
|---|---|---|
| memory search / no-vector | 0 次 embedding，关键词召回返回目标事实 | 无向量时已有可用的关键词路径 |
| memory search / rebuilding | 前台查询未产生 embedding 调用，返回目标事实 | 后台补建不会阻塞前台查询 |
| memory search / ready | 每次查询产生 1 次 embedding，向量召回可用 | 查询 embedding 仍是外部服务延迟边界 |
| history projection maintenance | 每个规模均实际重建 History/Recent，并记录墙钟耗时与投影内容 | 投影维护已被测量，未发现足够证据支持额外缓存或中间件 |

在相同环境中分别对原始基线提交和当前提交运行脚本，JSON 中包含目标 commit、规模、重复次数、实际输出和计数。结果用于相对比较；单机、临时存储、合成数据和小样本不能代表生产 SLA。脚本不把固定数据规模当作扫描次数，也不据此宣称 SQLite 扫描优化。

基于可观测证据，本 ticket 不引入未经证实的优化，也不修改 shell。若未来需要优化，应先扩大规模并采集真实数据库 profile，同时继续验证事实来源、召回预算、History/Recent/Pending 生命周期和 embedding worker 语义。
