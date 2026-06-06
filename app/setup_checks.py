import requests

from app.config import Settings
from app.llm_client import ChatMessage, LLMClientError, OpenAICompatibleClient


def verify_telegram_token(settings: Settings) -> tuple[bool, str]:
    try:
        response = requests.get(
            f"https://api.telegram.org/bot{settings.bot_token}/getMe",
            timeout=settings.request_timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        return False, f"请求 Telegram 失败: {exc}"
    except ValueError:
        return False, "Telegram 返回了无法解析的响应。"

    if not data.get("ok"):
        description = data.get("description", "未知错误")
        return False, f"Telegram token 校验失败: {description}"

    username = ""
    result = data.get("result")
    if isinstance(result, dict):
        username = str(result.get("username", "")).strip()
    if username:
        return True, f"Telegram 连接成功，机器人用户名：@{username}"
    return True, "Telegram 连接成功。"


def verify_openai_compatible(settings: Settings) -> tuple[bool, str]:
    client = OpenAICompatibleClient(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        model=settings.openai_model,
        timeout_seconds=settings.request_timeout_seconds,
    )
    try:
        reply = client.chat(
            [
                ChatMessage(role="system", content="Reply with OK only."),
                ChatMessage(role="user", content="ping"),
            ]
        )
    except LLMClientError as exc:
        return False, f"模型接口校验失败: {exc}"

    return True, f"模型接口连接成功，测试回复: {reply}"
