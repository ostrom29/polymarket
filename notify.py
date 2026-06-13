"""
Telegram notifications. Silently skips if TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID are not set.
"""
import os
import logging
import requests
from pathlib import Path

log = logging.getLogger(__name__)

_TOKEN: str | None = None
_CHAT_ID: str | None = None

trades_fired: int = 0


def _load() -> tuple[str | None, str | None]:
    global _TOKEN, _CHAT_ID
    if _TOKEN and _CHAT_ID:
        return _TOKEN, _CHAT_ID

    # Load .env if not already in environment
    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

    _TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or None
    _CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip() or None
    return _TOKEN, _CHAT_ID


def send(message: str) -> None:
    token, chat_id = _load()
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception as e:
        log.debug("Telegram notification failed: %s", e)
