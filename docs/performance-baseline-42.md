# #42 可重复性能基线

基线在隔离 checkout `C:/Users/Cx/AppData/Local/Temp/jarvis-ticket42-baseline`（提交 `88443ea4291a5ea4f042aa763f9ff98929a0c0fe`）运行。命令和合成数据脚本位于 [`benchmarks/baseline.py`](../benchmarks/baseline.py)，同一命令可在重构后 checkout 重跑；不读取个人数据、不调用网络或付费模型。

环境：Windows 11 10.0.26100，Python 3.12.10 AMD64，sqlite-vec 0.1.9。数据集版本 1，固定合成字面量，32 条事实/任务，5 次重复；每次使用临时目录。计时为 `perf_counter` 墙钟，包含 SQLite 与文件系统；每个临时数据集冷启动，重复查询在同一进程内为 warm。结果是观测值，不设门槛。

## 实测结果

| 操作/场景 | 次数 | 平均 ms | 范围 ms | 关键计数 |
|---|---:|---:|---:|---|
| 前台 memory search / no-vector | 5 | 6.130 | 4.996–7.180 | embedding 0；vector 不可用 |
| 前台 memory search / rebuilding | 5 | 53.050 | 11.626–215.411 | 查询触发补建，受控 embedding 调用可见 |
| 前台 memory search / ready | 5 | 11.010 | 10.444–11.685 | 就绪向量；查询仍有 1 次 query embedding |
| History 投影维护 | 5 | 0.025 | 0.017–0.032 | 32 tasks，用户输入进入 History，assistant 只在 Recent |
| Windows shell preparation | 1 | 22,285.273 | — | `echo baseline`，AppContainer/ACL 准备；进程执行时间未拆出 |

`rebuilding` 的宽范围直接显示前台同步扫描和 embedding 补建是瓶颈；ready 仍包含查询 embedding。History 行为验证了 History 只写用户 goal，并保留 Recent 的 assistant 内容。shell 测量把隔离准备和命令执行分开；当前基线测量到准备阶段，未把执行时间冒充准备成本。

## 限制

这是单机、单进程、临时本地存储的微基准；Windows 安全策略、文件系统缓存、SQLite/线程调度和受控客户端返回会造成波动。样本量小，不能外推生产规模；不能将一次 shell 准备时间视为所有机器的 SLA。脚本不预设优化、缓存或阈值，后续比较必须保留相同 scale、repeats、环境描述和 cold/warm 说明。

命令示例：

```powershell
$env:BASELINE_COMMIT='88443ea4291a5ea4f042aa763f9ff98929a0c0fe'
python benchmarks/baseline.py --target C:/path/to/checkout --output baseline.json --scales 32 --repeats 5 --sections memory,history,shell
```
