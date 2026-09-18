# Jarvis

一个面向个人工作流的本地命令行 Agent。Jarvis 通过兼容 OpenAI Chat Completions 的模型接口完成多轮对话，并在用户授权下读取、编辑和执行工作区工具。会话、来源、记忆和权限状态保存在本地。

> 当前项目是可运行的本地 CLI 基线，默认使用离线可测试的本地存储；它不是 Web 服务，也不要求部署数据库或后台服务。

## 功能

- 多轮模型交互与工具调用，支持并行独立工具和依赖调用。
- 工作区文件工具：`read`、`edit`、`bash`、`list_directory`，以及动态 `tool_search`。
- 会话列表与恢复：`--list`、`--resume`，恢复不会重新执行历史工具调用。
- 上下文预算、`/compact` 压缩和溢出恢复，保留原始事件与可核对的工具证据。
- 本地个人记忆：事实、来源、有效期、修正、忘记和画像发布均写入 SQLite。
- 未配置 embedding 时仍支持关键词召回；配置后可在后台补建向量，不阻塞整库补建。
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

不要把 `.env`、API key 或包含个人资料的 `STATE_DIR` 提交到 Git。`.env` 已被 Git 忽略；生产使用时建议把状态目录放在工作区之外。

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
| `STATE_DIR` | 会话和记忆状态目录 | Windows 为 `%LOCALAPPDATA%\jarvis` |
| `TOOL_PERMISSION_MODE` | `approve-all`、`approve-dangerous` 或 `broad-access` | `approve-dangerous` |
| `MAX_ROUNDS` | 单次请求最多模型轮数 | `5` |
| `REQUEST_TIMEOUT` | 模型请求超时秒数 | `60` |
| `COMPRESSION_MODEL` | 可选的上下文压缩模型 | 主模型 |

### Embedding（可选）

四个变量必须同时填写才会启用向量适配器：

```dotenv
EMBEDDING_BASE_URL=https://example.com/v1
EMBEDDING_API_KEY=your-embedding-key
EMBEDDING_MODEL=your-embedding-model
EMBEDDING_DIMENSIONS=1536
```

留空时使用关键词召回，事实仍会保存并立即可检索。不要为了启用基础记忆功能填写虚假的 embedding 配置。

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

测试默认使用临时目录、受控模型客户端和本地 SQLite，不调用真实付费模型服务。Windows shell isolation 测试会验证 ACL、Python 参数、进程树、取消和状态目录保护。

项目领域术语见 [CONTEXT.md](CONTEXT.md)，架构决策见 [docs/adr](docs/adr)。

## 项目结构

```text
jarvis_agent.py       CLI 与 Agent 交互入口
application.py        应用装配与共享生命周期
session_store.py      会话事件、恢复和迁移
context_manager.py    上下文投影、预算和压缩
memory_*.py           事实、检索、画像和 Pending
tool_runtime.py       工具注册、权限、调度和审计
shell_sandbox.py      Windows shell 隔离
tests/                外部行为与持久化契约测试
docs/adr/             架构决策记录
```

## 许可

当前仓库未声明开源许可证。除非仓库补充许可证文件，否则请不要将代码视为授予了再分发或商用许可。
