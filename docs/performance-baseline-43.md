# #43 性能对照结论

使用 #42 的同一基线脚本、版本 1 合成数据集（32 条事实/任务、5 次重复）、临时存储和 Windows 11 / Python 3.12.10 / sqlite-vec 0.1.9，对原始提交 `88443ea` 与当前提交 `c2a391d` 进行对照。计时为 `perf_counter` 墙钟，重复查询在同一进程内 warm；embedding 使用受控离线客户端。

| 操作 | 原始均值（范围） | 当前均值（范围） | 调用/扫描观察 |
|---|---:|---:|---|
| memory search / no-vector | 5.00 ms (4.41–5.83) | 6.03 ms (5.25–6.78) | 两者均 0 次 embedding，32 条扫描 |
| memory search / rebuilding | 47.09 ms (11.29–182.51) | 6.75 ms (5.53–8.03) | 原始查询触发 37 次 embedding；当前 0 次，后台补建不阻塞前台 |
| memory search / ready | 12.10 ms (10.91–13.20) | 9.58 ms (8.74–10.78) | 两者每次 1 次查询 embedding，32 条扫描 |
| History projection readback | 0.0253 ms (0.0179–0.0298) | 0.0253 ms (0.0170–0.0426) | 脚本报告 96 个事件规模；History 仅含用户输入，Recent 保留 assistant |

重建场景的长尾已消失，证明向量补建从前台移到应用拥有的后台 worker 是实际收益来源。ready 场景仍受查询 embedding 服务延迟影响，但当前结果略快；no-vector 与投影 readback 没有显示稳定的规模瓶颈。脚本中的 `scan_operations` 是固定数据规模的观测标签，并非 SQLite profile 的实际扫描计数，因此不据此声称扫描优化。基于这些观测，本任务不增加缓存、中间件或其它未经证实的优化，也不修改 shell。

shell preparation 仅作比较测量：当前 checkout 的一次 `echo baseline` 准备耗时约 7,955 ms，执行约 25 ms；没有把该单次 Windows 安全策略观测外推为 SLA，也没有修改 shell。原始 #42 报告记录过约 22,285 ms 的准备观测，机器策略波动很大，不能据此优化。

限制：单机、单进程、临时本地 SQLite、固定合成数据和受控客户端；样本量小，结果用于验证相对行为，不能代表生产 SLA。事实、来源、动态召回预算、History/Recent/Pending 语义及 worker 生命周期均由现有契约测试继续约束。

现有 #42 replay 脚本没有独立计时后台 vector backfill 完成或投影维护写入；本次只报告其对前台查询和投影 readback 的可观察影响，并明确将实际扫描数视为未测量。没有证据支持进一步局部优化，后续若需要独立维护计时应扩展基准脚本后再比较。
