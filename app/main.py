from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

from pydantic import ValidationError

from jarvis.config import load_config, write_example_config
from jarvis.logging import setup_logging
from jarvis.runtime.bootstrap import build_runtime


def main() -> None:
    args = sys.argv[1:]
    command = args[0] if args else "run"
    if command == "init":
        run_init()
        return
    if command == "doctor":
        run_doctor()
        return
    if command == "run":
        asyncio.run(run_bot())
        return
    raise SystemExit(f"Unknown command: {command}")


def run_init() -> None:
    target = Path("config.toml")
    if not target.exists():
        write_example_config(target)
    for path in (Path("data"), Path("logs")):
        path.mkdir(parents=True, exist_ok=True)
    print("Initialized config.toml, data/, and logs/")


def run_doctor() -> None:
    try:
        config = load_config()
    except ValidationError as exc:
        print("FAIL config: config.toml is incomplete or invalid")
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"])
            print(f"FAIL {field}: {error['msg']}")
        raise SystemExit(1)

    checks = [
        ("config", True, "config.toml loaded"),
        ("telegram_token", bool(config.telegram.bot_token), "telegram token present"),
        ("llm_key", bool(config.llm.api_key), "llm api key present"),
        ("workspace", config.workspace_path.exists(), f"workspace exists: {config.workspace_path}"),
        ("python", shutil.which("python") is not None, "python executable found"),
    ]
    failed = False
    for name, ok, detail in checks:
        status = "OK" if ok else "FAIL"
        print(f"{status} {name}: {detail}")
        failed = failed or not ok
    if failed:
        raise SystemExit(1)


async def run_bot() -> None:
    from jarvis.channels.telegram import TelegramBotRuntime

    try:
        config = load_config()
    except ValidationError as exc:
        print("config.toml is incomplete or invalid:")
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"])
            print(f"- {field}: {error['msg']}")
        print("Fill the missing values and rerun `py -3.12 -m app.main doctor`.")
        raise SystemExit(1)

    setup_logging(config.log_dir_path, config.runtime.log_level)
    runtime = build_runtime(config)
    bot = TelegramBotRuntime(
        token=config.telegram.bot_token,
        allowed_chat_ids=config.telegram.allowed_chat_ids,
        app_runtime=runtime.app,
    )
    await bot.run()


if __name__ == "__main__":
    main()
