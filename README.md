# Jarvis

一个面向个人工作流的本地 Agent，提供 CLI 和 Windows 下单用户 Telegram 私聊入口。Jarvis 通过兼容 OpenAI Chat Completions 的模型接口完成多轮对话，使用工作区内文件工具及 Windows AppContainer 沙箱中的命令。会话事件保存在本地；当前不提供跨会话个人记忆。

> Telegram 使用本机 long polling，不需要公网服务；进程退出时 Bot 不工作。模型只使用 `read`、`write`、`edit`、`bash`。默认工作区是启动命令时所在目录；会话仍按工作区分区。

## 功能

- 多轮模型交互与工具调用，支持并行独立工具和依赖调用。
- 工作区文件工具：`read`、`write`、`edit`、`bash`（Windows PowerShell）。
- 会话列表与恢复：`--list`、`--resume`，恢复不会重新执行历史工具调用。
- 上下文预算、`/compact` 压缩和溢出恢复；原始事件保留，可重新投影模型上下文。
- 会话内近期任务选择由上下文投影负责，不依赖个人记忆。
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

在 `.env` 中至少填写：

```dotenv
BASE_URL=https://api.deepseek.com/v1
API_KEY=your-api-key
MODEL=deepseek-v4-flash
```

在希望作为工作区的目录启动 CLI，例如：

```powershell
Set-Location C:\path\to\your\workspace
python C:\path\to\jarvis-agent\jarvis_agent.py
```

不要把配置文件、API key、Bot Token 或包含交互记录的 `STATE_DIR` 提交到 Git。生产使用时建议把状态目录放在工作区之外。

## Telegram 私聊入口（Windows）

可在项目 `.env` 设置 `TELEGRAM_BOT_TOKEN` 和数字 `TELEGRAM_ALLOWED_USER_ID`（不是用户名），在项目目录启动：

```powershell
python telegram_bot.py
```

**安全取舍（参照 Pi 的本地信任模型）**：工作区内的 `.env` 可被模型驱动的 `read` 和沙箱内 `bash` 读取；Telegram Token 和模型 API Key **不能保证不进入模型请求、工具结果或会话记录**。启动 Bot 即意味着信任当前工作区与所用模型服务，不适合不可信内容或无人值守的高风险环境。如需真正隔离凭据，应将配置移出工作区或使用系统级隔离，不能依靠提示词、文本脱敏或 `.gitignore`。Bot 对已知 Token 的消息文本做最佳努力替换，但这不是凭据安全边界。

只接收配置用户的私聊文本，其他聊天和文件消息不触发模型。消息串行处理；`/cancel` 请求取消当前任务，但无法撤销已发生的修改。重复投递不再次执行；重启后执行状态未知的任务不会自动重跑。同一私聊在不同工作区有不同的会话；回到旧目录启动可恢复该目录的会话。当前不支持越界确认放行；工作区外文件访问被拒绝。Bot 进程关闭后需要重新启动才能继续收消息。没有真实 Bot 凭据时只能进行离线模拟测试，不能视为真实 Telegram 验收。

## CLI

```text
python jarvis_agent.py [--env-file PATH] [--list] [--resume [SESSION_ID]]
```

- `--env-file PATH`：使用指定配置文件。
- `--list`：列出当前工作区的会话。
- `--resume`：恢复最近一个会话；也可以传入会话 ID。
- `exit`：退出交互模式。
- `/compact`：压缩当前任务之前的历史；`/compact 2000` 指定保留的 token 数。

## 配置

完整模板见 [.env.example](.env.example)。常用配置如下：

| 变量 | 作用 | 默认值 |
| --- | --- | --- |
| `BASE_URL` | 模型接口地址 | 无 |
| `API_KEY` | 模型接口密钥 | 无 |
| `MODEL` | 主模型名称 | 无 |
| `ROOT_DIR` | 不再覆盖启动目录；请从目标目录启动 | 当前目录 |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token，仅 Bot 入口需要 | 无 |
| `TELEGRAM_ALLOWED_USER_ID` | 允许的数字 Telegram 用户 ID，仅 Bot 入口需要 | 无 |
| `STATE_DIR` | 会话状态目录 | Windows 为 `%LOCALAPPDATA%\jarvis` |
| `MAX_ROUNDS` | 单次请求最多模型轮数 | `5` |
| `REQUEST_TIMEOUT` | 模型请求超时秒数 | `60` |
| `COMPRESSION_MODEL` | 可选的上下文压缩模型 | 主模型 |

## 工具与安全边界

`.env` 可直接放在项目工作区，工具也能读取它；这是一种本地信任取舍，不是凭据隔离。工作区外的文件和状态目录仍受现有工具与 Windows 沙箱边界限制。

文件工具仅允许访问启动工作区；在此范围内 `read/write/edit` 不逐次询问。越界路径默认拒绝，目前没有通过 Telegram 确认后提权的通道。`bash` 只能在 Windows AppContainer 沙箱内执行；沙箱初始化失败时不会以普通权限重试。

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
  definitions.py         内置四工具定义
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
