import logging
import sys
from pathlib import Path

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
from app.drift import DriftConfig, DriftRunner
from app.llm_client import OpenAICompatibleClient
from app.memory_store import MemoryStore, MemoryStoreError
from app.plugins import PluginError, PluginHost
from app.proactive import ProactiveConfig, ProactiveRuntimeState, ProactiveScheduler
from app.setup_checks import verify_openai_compatible, verify_telegram_token
from app.telegram_bot import TelegramBot, TelegramOffsetStoreError
from app.tools import ToolExecutor, ToolLoop, build_builtin_tool_registry


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
    tool_registry = build_builtin_tool_registry(
        workspace_root=Path.cwd(),
        memory_root=settings.memory_root_dir,
    )
    plugin_host = PluginHost(
        registry=tool_registry,
        enabled_plugins=settings.enabled_plugins,
        disabled_plugins=settings.disabled_plugins,
        plugin_configs=settings.plugin_configs or {},
    )
    try:
        plugin_host.initialize()
    except PluginError as exc:
        logging.error("%s", exc)
        return 1
    logging.info(
        "Runtime plugin state: loaded=%s proactive=%s",
        ",".join(plugin_host.loaded_plugin_ids) or "(none)",
        ",".join(plugin_host.proactive_plugin_ids) or "(none)",
    )
    proactive_state = ProactiveRuntimeState()
    agent_service = AgentService(
        llm_client=llm_client,
        system_prompt=settings.system_prompt,
        conversation_store=conversation_store,
        memory_store=memory_store,
        tool_loop=ToolLoop(
            registry=tool_registry,
            executor=ToolExecutor(tool_registry),
            max_tool_steps=settings.tool_max_steps,
        ),
        plugin_host=plugin_host,
    )
    bot = TelegramBot(
        bot_token=settings.bot_token,
        agent_service=agent_service,
        poll_timeout_seconds=settings.poll_timeout_seconds,
        request_timeout_seconds=settings.request_timeout_seconds,
        offset_path=settings.memory_root_dir / "telegram-offset.txt",
        runtime_state=proactive_state,
    )
    proactive_scheduler = _build_proactive_scheduler(
        settings=settings,
        plugin_host=plugin_host,
        memory_store=memory_store,
        telegram_bot=bot,
        runtime_state=proactive_state,
    )
    drift_runner = _build_drift_runner(
        settings=settings,
        plugin_host=plugin_host,
        memory_store=memory_store,
        activity_state=proactive_state,
    )
    if proactive_scheduler is not None:
        logging.info(
            "Proactive runtime enabled: chat_id=%s interval=%ss cooldown=%ss",
            settings.proactive_chat_id,
            settings.proactive_tick_interval_seconds,
            settings.proactive_cooldown_seconds,
        )
        proactive_scheduler.start()
    else:
        logging.info("Proactive runtime disabled")
    if drift_runner is not None:
        logging.info(
            "Drift runtime enabled: interval=%ss dedupe=%ss",
            settings.drift_tick_interval_seconds,
            settings.drift_dedupe_window_seconds,
        )
        drift_runner.start()
    else:
        logging.info("Drift runtime disabled")
    try:
        bot.run_forever()
    except TelegramOffsetStoreError as exc:
        logging.error("%s", exc)
        return 1
    finally:
        if proactive_scheduler is not None:
            proactive_scheduler.stop()
        if drift_runner is not None:
            drift_runner.stop()
    return 0


def _build_proactive_scheduler(
    *,
    settings,
    plugin_host: PluginHost,
    memory_store: MemoryStore,
    telegram_bot: TelegramBot,
    runtime_state: ProactiveRuntimeState,
) -> ProactiveScheduler | None:
    if not settings.proactive_enabled:
        return None
    if settings.proactive_chat_id is None:
        logging.warning("Proactive scheduler enabled but proactive.chat_id is missing; skipping startup")
        return None
    config = ProactiveConfig(
        enabled=True,
        chat_id=settings.proactive_chat_id,
        delivery_log_path=settings.memory_root_dir / "proactive_delivery_log.json",
        tick_interval_seconds=settings.proactive_tick_interval_seconds,
        cooldown_seconds=settings.proactive_cooldown_seconds,
        user_active_grace_seconds=settings.proactive_user_active_grace_seconds,
        candidate_limit=settings.proactive_candidate_limit,
        max_sends_per_tick=settings.proactive_max_sends_per_tick,
    )
    return ProactiveScheduler(
        config=config,
        plugin_host=plugin_host,
        memory_store=memory_store,
        telegram_bot=telegram_bot,
        runtime_state=runtime_state,
    )


def _build_drift_runner(
    *,
    settings,
    plugin_host: PluginHost,
    memory_store: MemoryStore,
    activity_state: ProactiveRuntimeState,
) -> DriftRunner | None:
    if not settings.drift_enabled:
        return None
    config = DriftConfig(
        enabled=True,
        execution_log_path=settings.memory_root_dir / "drift_execution_log.json",
        tick_interval_seconds=settings.drift_tick_interval_seconds,
        idle_grace_seconds_after_user_message=settings.drift_idle_grace_seconds_after_user_message,
        idle_grace_seconds_after_proactive_send=settings.drift_idle_grace_seconds_after_proactive_send,
        dedupe_window_seconds=settings.drift_dedupe_window_seconds,
        max_task_runtime_seconds=settings.drift_max_task_runtime_seconds,
        max_task_cost=settings.drift_max_task_cost,
    )
    return DriftRunner(
        config=config,
        plugin_host=plugin_host,
        memory_store=memory_store,
        activity_state=activity_state,
    )


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
