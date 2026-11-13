"""Настройки из файла .env."""
import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    telegram_token: str
    allowed_user_id: int | None
    timezone: ZoneInfo
    db_path: str
    claude_model: str


def load_config() -> Config:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Не задан TELEGRAM_BOT_TOKEN в файле .env")
    if not os.getenv("ANTHROPIC_API_KEY", "").strip():
        raise SystemExit("Не задан ANTHROPIC_API_KEY в файле .env")
    uid = os.getenv("ALLOWED_USER_ID", "").strip()
    return Config(
        telegram_token=token,
        allowed_user_id=int(uid) if uid else None,
        timezone=ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow")),
        db_path=os.getenv("DB_PATH", "food.db"),
        claude_model=os.getenv("CLAUDE_MODEL", "claude-opus-5-5"),
    )
