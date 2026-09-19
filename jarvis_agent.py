"""Command-line entry point for Jarvis."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from agent.agent import Agent
from configuration import Config, ConfigurationError, save_global_tool_permission_mode
from memory.memory_profile import ProfileEditError
from session.session_store import SessionStore, SessionNotFoundError, SessionLockedError, resolve_state_dir
from tools.tool_runtime import PermissionPolicy

def parse_compact_argument(argument: str) -> int | None:
    """Return the optional keep target for the /compact command."""

    text = argument.strip()
    if not text:
        return None
    if not text.isdigit() or int(text) < 1:
        raise ValueError("用法：/compact [保留的 token 数]，例如 /compact 2000；不带参数时保留当前任务的原文。")
    return int(text)


PERMISSION_COMMANDS = {
    "/all": "approve-all",
    "/safe": "approve-dangerous",
    "/wide": "broad-access",
}

def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Jarvis 第一阶段命令行 Agent")
    parser.add_argument("--env-file", type=Path, default=Path.cwd() / ".env", help="配置文件路径")
    parser.add_argument("--resume", nargs="?", const="", default=None,
                        help="恢复会话：不带值取当前工作区最近的会话，或指定会话标识")
    parser.add_argument("--list", action="store_true", dest="list_sessions",
                        help="列出当前工作区可恢复的会话")
    args = parser.parse_args(argv)
    try:
        config = Config.from_env(args.env_file)
    except ConfigurationError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    if args.list_sessions:
        sessions = SessionStore.list_sessions(config)
        if not sessions:
            print("当前工作区没有会话记录。")
            return 0
        print(f"当前工作区的会话（{config.root_dir}）：")
        for session in sessions:
            preview = session.last_user_text.replace("\n", " ")[:40]
            print(f"  {session.id}  {session.started_at}  消息 {session.message_count} 条"
                  + (f"  最后输入: {preview}" if preview else ""))
        return 0
    try:
        def confirm_tool(metadata, arguments):
            if not sys.stdin.isatty():
                return False
            try:
                return input(f"允许 {metadata.tool_id} ({metadata.risk}) {json.dumps(arguments, ensure_ascii=False)}? [y/N] ").strip().casefold() == "y"
            except (EOFError, KeyboardInterrupt):
                return False
        agent = Agent(config, resume=args.resume, confirm_tool=confirm_tool)
    except ConfigurationError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    except (SessionNotFoundError, SessionLockedError) as exc:
        print(f"会话错误: {exc}", file=sys.stderr)
        return 2

    print("Jarvis 已启动。输入 exit 退出，/compact [保留token] 主动压缩上下文，/all、/safe、/wide 设置全局工具权限，Ctrl+C 取消当前请求。")
    try:
        while True:
            try:
                user_text = input("\nYou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n已退出。")
                return 0
            if not user_text:
                continue
            if user_text.casefold() == "exit":
                print("已退出。")
                return 0
            command, _, argument = user_text.partition(" ")
            if command.casefold() == "/compact":
                try:
                    keep = parse_compact_argument(argument)
                except ValueError as exc:
                    print(str(exc))
                    continue
                agent.compact_now(keep)
                continue
            if command.casefold() == "/permissions":
                print(f"全局工具权限：{agent.tool_runtime.policy.mode}")
                continue
            mode = PERMISSION_COMMANDS.get(command.casefold())
            if mode:
                policy = agent.tool_runtime.policy
                widening = PermissionPolicy.MODES[mode] > PermissionPolicy.MODES[policy.mode]
                if widening:
                    try:
                        confirmed = input(f"将全局工具权限升级为 {mode}，后续启动均会使用此设置。确认? [y/N] ").strip().casefold() == "y"
                    except (EOFError, KeyboardInterrupt):
                        confirmed = False
                    if not confirmed:
                        print("未更改全局工具权限。")
                        continue
                try:
                    save_global_tool_permission_mode(resolve_state_dir(config), mode)
                except OSError as exc:
                    print(f"无法保存全局工具权限：{exc}")
                    continue
                outcome = policy.change_mode(mode, confirmed=widening)
                if not outcome["ok"]:
                    print(f"未更改全局工具权限：{outcome['error']['message']}")
                    continue
                print(f"全局工具权限已设为 {mode}。")
                continue
            try:
                agent.run_request(user_text)
            except ProfileEditError as exc:
                print(f"[记忆编辑未导入] {exc}；请修正 memory.md 后重试。原文件已保留。")
    finally:
        agent.close()


if __name__ == "__main__":
    raise SystemExit(main())
