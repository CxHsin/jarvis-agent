# Jarvis

一个面向个人工作流的本地命令行 Agent。Jarvis 通过兼容 OpenAI Chat Completions 的模型接口完成多轮对话，并在用户授权下读取、编辑和执行工作区工具。会话事件和权限状态保存在本地；当前不提供跨会话个人记忆。

> 当前项目是可运行的本地 CLI 基线，默认使用离线可测试的本地存储；它不是 Web 服务，也不要求部署数据库或后台服务。

## 功能

- 多轮模型交互与工具调用，支持并行独立工具和依赖调用。
- 工作区文件工具：`read`、`edit`、`bash`、`list_directory`，以及动态 `tool_search`。
- 会话列表与恢复：`--list`、`--resume`，恢复不会重新执行历史工具调用。
- 上下文预算、`/compact` 压缩和溢出恢复；原始事件保留，可重新投影模型上下文。
- 会话内近期任务选择由上下文投影负责，不依赖个人记忆。
- 三档工具权限：`approve-all`、`approve-dangerous`、`broad-access`。
- Windows shell 使用 AppContainer 和 Job object 约束进程树；Python 参数通过固定 launcher 与 JSON manifest 传递。

## 快速开始

### 环境要求

- Python 3.12+
- 一个兼容 OpenAI Chat Completions 的模型接口
- Windows 上的 shell 隔离测试需要 Windows AppContainer；其他功能可在支持的 Python 平台运行

### 安装

```powershell
git clone https://github.com/CxHsin/jarvis-agent.git
cd jarvis-agent
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

编辑 `.env`，至少填写：

```dotenv
BASE_URL=https://api.deepseek.com/v1
API_KEY=your-api-key
MODEL=deepseek-v4-flash
ROOT_DIR=C:\path\to\your\workspace
```

启动：

```powershell
.\.venv\Scripts\python.exe .\jarvis_agent.py
```

不要把 `.env`、API key 或包含交互记录的 `STATE_DIR` 提交到 Git。`.env` 已被 Git 忽略；生产使用时建议把状态目录放在工作区之外。

## CLI

```text
python jarvis_agent.py [--env-file PATH] [--list] [--resume [SESSION_ID]]
```

- `--env-file PATH`：使用指定配置文件。
- `--list`：列出当前工作区的会话。
- `--resume`：恢复最近一个会话；也可以传入会话 ID。
- `exit`：退出交互模式。
- `/compact`：压缩当前任务之前的历史；`/compact 2000` 指定保留的 token 数。
- `/permissions`、`/all`、`/safe`、`/wide`：查看或调整工具权限模式。

## 配置

完整模板见 [.env.example](.env.example)。常用配置如下：

| 变量 | 作用 | 默认值 |
| --- | --- | --- |
| `BASE_URL` | 模型接口地址 | 无 |
| `API_KEY` | 模型接口密钥 | 无 |
| `MODEL` | 主模型名称 | 无 |
| `ROOT_DIR` | 工作区根目录 | 无 |
| `STATE_DIR` | 会话状态目录 | Windows 为 `%LOCALAPPDATA%\jarvis` |
| `TOOL_PERMISSION_MODE` | `approve-all`、`approve-dangerous` 或 `broad-access` | `approve-dangerous` |
| `MAX_ROUNDS` | 单次请求最多模型轮数 | `5` |
| `REQUEST_TIMEOUT` | 模型请求超时秒数 | `60` |
| `COMPRESSION_MODEL` | 可选的上下文压缩模型 | 主模型 |

## 工具与安全边界

工具运行在当前权限策略下。`broad-access` 只应在明确理解风险后使用；`bash` 会被视为高风险操作。

Windows 下 `bash` 的实现是 PowerShell，不是 Bash。PowerShell provider 对卷根和部分绝对路径的 `Set-Location`、`Remove-Item` 可能被 AppContainer 拒绝；删除或写入工作区文件请使用 .NET API，例如：

```powershell
[IO.File]::Delete('C:\path\to\file.txt')
[IO.File]::WriteAllText('C:\path\to\file.txt', 'content')
```

Python 命令由沙箱临时目录中的固定 launcher 启动，参数通过 UTF-8 JSON manifest 传递，因此支持空格、Unicode、引号、空参数和尾反斜杠。其他原生命令仍受 PowerShell resolver 和 AppContainer 限制。

## 开发与测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

测试默认使用临时目录、受控模型客户端，不调用真实付费模型服务。Windows shell isolation 测试会验证 ACL、Python 参数、进程树、取消和状态目录保护。

项目领域术语见 [CONTEXT.md](CONTEXT.md)，架构决策见 [docs/adr](docs/adr)。

## 项目结构

```text
jarvis_agent.py          CLI：启动、命令解析与交互
application.py           应用装配与共享生命周期
configuration.py         配置解析与用户默认设置
agent/
  agent.py               Agent 任务执行与模型—工具循环
context/
  context_manager.py     上下文状态与请求组装
  context_projection.py  从事件选择近期模型上下文
  context_budget.py      输入预算与 token 估算
  compaction.py          压缩、切点与溢出恢复
session/
  session_store.py       会话事件、持久化与恢复
  session_migration.py   旧会话迁移
models/
  model_client.py        模型通信与请求处理
  model_capabilities.py  模型容量查询
  model_capabilities.json 模型能力目录
  cache_metrics.py       模型用量与缓存统计
tools/
  definitions.py         内置工具定义
  workspace.py           工作区文件操作与 shell 工具入口
  tool_runtime.py        工具注册、发现、权限与审计
  tool_execution.py      工具执行、取消与依赖调度
  shell_sandbox.py       Windows shell 隔离
  shell_launcher.py      沙箱 Python 启动桥接
tests/                  外部行为与持久化契约测试
benchmarks/             离线性能基线
docs/adr/               架构决策记录
```

按职责定位代码：`session` 保存完整运行事件，`context` 从事件选择近期上下文并决定本次发送给模型的内容；`agent` 协调任务，`tools` 执行具体操作，`models` 处理通信、容量和用量。应用负责装配与关闭资源；程序不会自动清理旧状态数据。

Python 调用方从所属模块导入，例如 `from agent.agent import Agent`、`from context.context_projection import TaskHistory`。CLI 仍使用 `python jarvis_agent.py`。

## 许可

本项目采用 [GNU General Public License v3.0](LICENSE)（GPL-3.0）。你可以自由使用、修改和分发，但衍生作品必须以相同的 GPL-3.0 许可开源。

Copyright (C) 2026 CxHsin
