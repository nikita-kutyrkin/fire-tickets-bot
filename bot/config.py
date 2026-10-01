import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent

# На хостинге bothost.ru платформа задаёт DATA_DIR=/app/data — эта папка переживает обновления кода
DATA_DIR = Path(os.getenv("DATA_DIR") or ROOT_DIR / "data")

# Локально настройки лежат в .env в корне проекта. На хостинге — в переменных окружения панели
# или в data/.env (его можно загрузить файловым менеджером). Уже заданные переменные не перезаписываются.
load_dotenv(ROOT_DIR / ".env")
load_dotenv(DATA_DIR / ".env")

# Часовой пояс, в котором считаются «сегодня» и «завтра». Сервер обычно живёт в UTC.
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))


def today() -> date:
    return datetime.now(TZ).date()


@dataclass(frozen=True)
class Config:
    bot_token: str
    tp_token: str
    tp_marker: str
    origin: str
    max_price: int
    min_discount_percent: int
    days_ahead: int
    check_interval_minutes: int
    db_path: Path


def load_config() -> Config:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise SystemExit("BOT_TOKEN не задан: добавьте его в .env или в переменные окружения")

    return Config(
        bot_token=bot_token,
        tp_token=os.getenv("TRAVELPAYOUTS_TOKEN", "").strip(),
        tp_marker=os.getenv("TRAVELPAYOUTS_MARKER", "").strip(),
        origin=os.getenv("ORIGIN", "LED").strip().upper(),
        max_price=int(os.getenv("MAX_PRICE", "0")),
        min_discount_percent=int(os.getenv("MIN_DISCOUNT_PERCENT", "40")),
        days_ahead=int(os.getenv("DAYS_AHEAD", "1")),
        check_interval_minutes=int(os.getenv("CHECK_INTERVAL_MINUTES", "20")),
        db_path=DATA_DIR / "bot.db",
    )
