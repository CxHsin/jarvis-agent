import logging
import sys

from app.agent import AgentService
from app.config import (
    DEFAULT_CONFIG_PATH,
    ConfigError,
    _load_config_file,
    load_settings,
    run_setup_wizard,
    settings_from_config,
)
from app.conversation_store import ConversationStore
from app.llm_client import OpenAICompatibleClient
from app.memory_store import MemoryStore, MemoryStoreError
from app.setup_checks import verify_openai_compatible, verify_telegram_token
from app.telegram_bot import TelegramBot


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if args:
        command = args[0]
        if command == "setup":
            return _handle_setup(overwrite=False)
        if command == "init":
            return _handle_setup(overwrite=True)
        print(f"未知命令: {command}")
        print("可用命令: setup, init")
        return 1

    configure_logging()
    try:
        settings = load_settings()
    except ConfigError as exc:
        logging.error("%s", exc)
        return 1

    llm_client = OpenAICompatibleClient(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        model=settings.openai_model,
        timeout_seconds=settings.request_timeout_seconds,
    )
    conversation_store = ConversationStore(max_rounds=settings.conversation_max_rounds)
    memory_store = MemoryStore(root_dir=settings.memory_root_dir)
    try:
        memory_store.ensure_initialized()
    except MemoryStoreError as exc:
        logging.error("%s", exc)
        return 1
    agent_service = AgentService(
        llm_client=llm_client,
        system_prompt=settings.system_prompt,
        conversation_store=conversation_store,
        memory_store=memory_store,
    )
    bot = TelegramBot(
        bot_token=settings.bot_token,
        agent_service=agent_service,
        poll_timeout_seconds=settings.poll_timeout_seconds,
        request_timeout_seconds=settings.request_timeout_seconds,
    )
    bot.run_forever()
    return 0


def _handle_setup(*, overwrite: bool) -> int:
    try:
        path = run_setup_wizard(overwrite=overwrite)
    except EOFError:
        print("")
        print("当前环境无法进行交互输入，请在正常终端里运行该命令。")
        return 1
    except KeyboardInterrupt:
        print("")
        print("已取消配置。")
        return 1
    except ConfigError as exc:
        print(exc)
        return 1

    action = "已更新" if overwrite else "已创建"
    print("")
    print(f"{action}配置文件: {path}")
    _run_setup_checks()
    print("")
    print("启动机器人命令: python -m app.main")
    return 0


def _run_setup_checks() -> None:
    print("")
    print("开始进行配置连通性检查...")
    try:
        settings = settings_from_config(_load_config_file(DEFAULT_CONFIG_PATH))
    except ConfigError as exc:
        print(f"读取配置失败，无法继续检查: {exc}")
        return

    ok, message = verify_telegram_token(settings)
    prefix = "[通过]" if ok else "[失败]"
    print(f"{prefix} {message}")

    ok, message = verify_openai_compatible(settings)
    prefix = "[通过]" if ok else "[失败]"
    print(f"{prefix} {message}")


if __name__ == "__main__":
    raise SystemExit(main())
