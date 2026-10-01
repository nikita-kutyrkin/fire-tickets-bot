from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Настройки пользователя. Создаются при первом обращении из значений по умолчанию (.env).
CREATE TABLE IF NOT EXISTS user_settings (
    chat_id INTEGER PRIMARY KEY,
    min_discount INTEGER NOT NULL,
    max_price INTEGER NOT NULL,
    days_ahead INTEGER NOT NULL
);

-- Города вылета пользователя. Хотя бы один есть всегда.
CREATE TABLE IF NOT EXISTS user_origins (
    chat_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    PRIMARY KEY (chat_id, code)
);

-- Направления, которые интересны пользователю. Нет строк — интересны все.
CREATE TABLE IF NOT EXISTS user_destinations (
    chat_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    PRIMARY KEY (chat_id, code)
);

-- Какие билеты и по какой цене уже отправлены каждому пользователю
CREATE TABLE IF NOT EXISTS sent_notifications (
    chat_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    price INTEGER NOT NULL,
    sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chat_id, key)
);

-- «Обычная» цена маршрута. price = NULL — данных по маршруту не хватило.
CREATE TABLE IF NOT EXISTS route_baselines (
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    price INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (origin, destination)
);

-- Таблица из первого этапа, заменена на sent_notifications
DROP TABLE IF EXISTS sent_offers;
"""

CityKind = Literal["from", "to"]
CITY_TABLES = {"from": "user_origins", "to": "user_destinations"}
SETTING_FIELDS = {"min_discount", "max_price", "days_ahead"}


@dataclass(frozen=True)
class UserSettings:
    chat_id: int
    origins: list[str]
    destinations: list[str]  # пусто — все направления
    min_discount: int
    max_price: int  # 0 — без потолка
    days_ahead: int


@dataclass(frozen=True)
class Defaults:
    origin: str
    min_discount: int
    max_price: int
    days_ahead: int


class Database:
    def __init__(self, path: Path, defaults: Defaults):
        self._path = path
        self._defaults = defaults
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    # --- подписчики ---

    async def add_subscriber(self, chat_id: int) -> None:
        await self._conn.execute("INSERT OR IGNORE INTO subscribers (chat_id) VALUES (?)", (chat_id,))
        await self._conn.commit()

    async def remove_subscriber(self, chat_id: int) -> None:
        await self._conn.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))
        await self._conn.commit()

    async def subscribers(self) -> list[int]:
        async with self._conn.execute("SELECT chat_id FROM subscribers") as cur:
            return [row[0] async for row in cur]

    # --- настройки ---

    async def settings(self, chat_id: int) -> UserSettings:
        await self._ensure_settings(chat_id)
        async with self._conn.execute(
            "SELECT min_discount, max_price, days_ahead FROM user_settings WHERE chat_id = ?", (chat_id,)
        ) as cur:
            min_discount, max_price, days_ahead = await cur.fetchone()
        return UserSettings(
            chat_id=chat_id,
            origins=await self.cities(chat_id, "from"),
            destinations=await self.cities(chat_id, "to"),
            min_discount=min_discount,
            max_price=max_price,
            days_ahead=days_ahead,
        )

    async def update_setting(self, chat_id: int, field: str, value: int) -> None:
        if field not in SETTING_FIELDS:
            raise ValueError(f"Неизвестная настройка: {field}")
        await self._ensure_settings(chat_id)
        await self._conn.execute(f"UPDATE user_settings SET {field} = ? WHERE chat_id = ?", (value, chat_id))
        await self._conn.commit()

    async def _ensure_settings(self, chat_id: int) -> None:
        d = self._defaults
        cur = await self._conn.execute(
            "INSERT OR IGNORE INTO user_settings (chat_id, min_discount, max_price, days_ahead) VALUES (?, ?, ?, ?)",
            (chat_id, d.min_discount, d.max_price, d.days_ahead),
        )
        if cur.rowcount:
            await self._conn.execute(
                "INSERT OR IGNORE INTO user_origins (chat_id, code) VALUES (?, ?)", (chat_id, d.origin)
            )
        await self._conn.commit()

    # --- города вылета и назначения ---

    async def add_city(self, chat_id: int, kind: CityKind, code: str) -> None:
        await self._ensure_settings(chat_id)
        await self._conn.execute(f"INSERT OR IGNORE INTO {CITY_TABLES[kind]} (chat_id, code) VALUES (?, ?)", (chat_id, code))
        await self._conn.commit()

    async def remove_city(self, chat_id: int, kind: CityKind, code: str) -> None:
        await self._conn.execute(f"DELETE FROM {CITY_TABLES[kind]} WHERE chat_id = ? AND code = ?", (chat_id, code))
        await self._conn.commit()

    async def clear_cities(self, chat_id: int, kind: CityKind) -> None:
        await self._conn.execute(f"DELETE FROM {CITY_TABLES[kind]} WHERE chat_id = ?", (chat_id,))
        await self._conn.commit()

    async def cities(self, chat_id: int, kind: CityKind) -> list[str]:
        async with self._conn.execute(
            f"SELECT code FROM {CITY_TABLES[kind]} WHERE chat_id = ? ORDER BY rowid", (chat_id,)
        ) as cur:
            return [row[0] async for row in cur]

    # --- отправленные уведомления ---

    async def is_new_or_cheaper(self, chat_id: int, key: str, price: int) -> bool:
        """True, если билет ещё не присылали этому пользователю или он подешевел с прошлого раза."""
        async with self._conn.execute(
            "SELECT price FROM sent_notifications WHERE chat_id = ? AND key = ?", (chat_id, key)
        ) as cur:
            row = await cur.fetchone()
        return row is None or price < row[0]

    async def mark_sent(self, chat_id: int, key: str, price: int) -> None:
        await self._conn.execute(
            "INSERT INTO sent_notifications (chat_id, key, price) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id, key) DO UPDATE SET price = excluded.price, sent_at = CURRENT_TIMESTAMP",
            (chat_id, key, price),
        )
        await self._conn.commit()

    # --- обычные цены маршрутов ---

    async def get_baseline(self, origin: str, destination: str, max_age: timedelta) -> tuple[bool, int | None]:
        """(найдено ли свежее значение, цена). Цена может быть None — данных по маршруту нет."""
        async with self._conn.execute(
            "SELECT price, updated_at FROM route_baselines WHERE origin = ? AND destination = ?",
            (origin, destination),
        ) as cur:
            row = await cur.fetchone()
        if row is None or datetime.fromisoformat(row[1]) < datetime.now() - max_age:
            return False, None
        return True, row[0]

    async def save_baseline(self, origin: str, destination: str, price: int | None) -> None:
        await self._conn.execute(
            "INSERT INTO route_baselines (origin, destination, price, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(origin, destination) DO UPDATE SET price = excluded.price, updated_at = excluded.updated_at",
            (origin, destination, price, datetime.now().isoformat()),
        )
        await self._conn.commit()
