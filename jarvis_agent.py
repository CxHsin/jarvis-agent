"""Command-line entry point for Jarvis."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from agent.agent import Agent
from configuration import Config, ConfigurationError
from session.session_store import SessionStore, SessionNotFoundError, SessionLockedError

def parse_compact_argument(argument: str) -> int | None:
    """Return the optional keep target for the /compact command."""

    text = argument.strip()
    if not text:
        return None
    if not text.isdigit() or int(text) < 1:
        raise ValueError("用法：/compact [保留的 token 数]，例如 /compact 2000；不带参数时保留当前任务的原文。")
    return int(text)


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
        agent = Agent(config, resume=args.resume)
    except ConfigurationError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    except (SessionNotFoundError, SessionLockedError) as exc:
        print(f"会话错误: {exc}", file=sys.stderr)
        return 2

    print("Jarvis 已启动。输入 exit 退出，/compact [保留token] 主动压缩上下文，Ctrl+C 取消当前请求。")
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
            agent.run_request(user_text)
    finally:
        agent.close()


if __name__ == "__main__":
    raise SystemExit(main())
