"""Периодическая проверка цен и рассылка горящих билетов."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html import escape
from statistics import median

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from .config import Config, today
from .db import Database, UserSettings
from .travelpayouts import Ticket, TravelpayoutsClient

# «Обычная» цена маршрута — медиана минимальных цен по дням на BASELINE_DAYS вперёд
BASELINE_DAYS = 30
BASELINE_MIN_DAYS = 7
BASELINE_TTL = timedelta(hours=24)

# Сколько билетов максимум присылать одному пользователю за одну проверку (одним сообщением)
MAX_DEALS_PER_MESSAGE = 10

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Deal:
    ticket: Ticket
    baseline: int | None

    @property
    def discount(self) -> int | None:
        """На сколько процентов билет дешевле обычной цены."""
        if not self.baseline:
            return None
        return round(100 * (1 - self.ticket.price / self.baseline))


@dataclass
class _OriginQuery:
    """Что нужно запросить по одному городу вылета, чтобы покрыть всех пользователей."""

    days_ahead: int = 0
    everywhere: bool = False
    destinations: set[str] = field(default_factory=set)


class Checker:
    def __init__(self, config: Config, db: Database, tp: TravelpayoutsClient, bot: Bot):
        self.config = config
        self._db = db
        self.tp = tp
        self._bot = bot

    async def run_forever(self) -> None:
        while True:
            try:
                await self.check_and_notify()
            except Exception:
                log.exception("Ошибка при проверке цен")
            await asyncio.sleep(self.config.check_interval_minutes * 60)

    async def find_deals(self, user: UserSettings) -> list[Deal]:
        """Горящие билеты по настройкам пользователя."""
        return await self._evaluate(user, await self._fetch_tickets([user]))

    async def cheapest_on_routes(self, user: UserSettings) -> list[Deal]:
        """Самый дешёвый билет по каждому маршруту пользователя, даже если он не горящий."""
        best: dict[tuple[str, str], Ticket] = {}
        for t in await self._fetch_tickets([user]):
            route = (t.origin, t.destination)
            if self._matches(user, t) and (route not in best or t.price < best[route].price):
                best[route] = t
        return [Deal(t, await self._baseline(t.origin, t.destination)) for t in best.values()]

    async def check_and_notify(self) -> None:
        users = [await self._db.settings(chat_id) for chat_id in await self._db.subscribers()]
        if not users:
            return

        tickets = await self._fetch_tickets(users)
        log.info("Получено билетов: %d", len(tickets))

        for user in users:
            new = [
                d
                for d in await self._evaluate(user, tickets)
                if await self._db.is_new_or_cheaper(user.chat_id, d.ticket.key, d.ticket.price)
            ][:MAX_DEALS_PER_MESSAGE]
            if not new:
                continue
            # Все новые билеты — одним сообщением, чтобы не упираться в лимиты Telegram
            if await self._send(user.chat_id, format_deals(new, self.tp)):
                for d in new:
                    await self._db.mark_sent(user.chat_id, d.ticket.key, d.ticket.price)
                log.info("Пользователю %s отправлено билетов: %d", user.chat_id, len(new))
            await asyncio.sleep(0.1)

    async def _fetch_tickets(self, users: list[UserSettings]) -> list[Ticket]:
        """Билеты, нужные всем переданным пользователям, — без повторных запросов по одному маршруту."""
        if not self.config.tp_token:
            raise RuntimeError("TRAVELPAYOUTS_TOKEN не задан в .env")

        queries: dict[str, _OriginQuery] = {}
        for user in users:
            for origin in user.origins:
                q = queries.setdefault(origin, _OriginQuery())
                q.days_ahead = max(q.days_ahead, user.days_ahead)
                q.everywhere |= not user.destinations
                q.destinations |= set(user.destinations)

        start = today()
        tickets: dict[str, Ticket] = {}
        for origin, q in queries.items():
            for offset in range(q.days_ahead + 1):
                day = start + timedelta(days=offset)
                found = await self.tp.cheapest(origin, day) if q.everywhere else []
                for dest in q.destinations - {origin}:
                    found += await self.tp.cheapest(origin, day, dest)
                for t in found:
                    tickets[t.key] = t

        now = datetime.now().astimezone()
        return [t for t in tickets.values() if t.departure_at > now]

    async def _evaluate(self, user: UserSettings, tickets: list[Ticket]) -> list[Deal]:
        """Оставляет горящие для пользователя билеты, самые выгодные — первыми."""
        deals = []
        for t in tickets:
            if not self._matches(user, t):
                continue
            if user.max_price and t.price > user.max_price:
                continue
            baseline = await self._baseline(t.origin, t.destination)
            if baseline:
                hot = t.price <= baseline * (1 - user.min_discount / 100)
            else:
                # Нет данных об обычной цене — ориентируемся только на потолок цены
                hot = bool(user.max_price)
            if hot:
                deals.append(Deal(t, baseline))
        return sorted(deals, key=lambda d: (-(d.discount or 0), d.ticket.price))

    @staticmethod
    def _matches(user: UserSettings, t: Ticket) -> bool:
        """Подходит ли билет пользователю по городам и датам."""
        last_day = today() + timedelta(days=user.days_ahead)
        return (
            t.origin in user.origins
            and (not user.destinations or t.destination in user.destinations)
            and t.departure_at.date() <= last_day
        )

    async def _baseline(self, origin: str, destination: str) -> int | None:
        found, price = await self._db.get_baseline(origin, destination, BASELINE_TTL)
        if found:
            return price

        start = today()
        end = start + timedelta(days=BASELINE_DAYS)
        prices: dict[date, int] = {}
        try:
            for month in sorted({start.strftime("%Y-%m"), end.strftime("%Y-%m")}):
                prices |= await self.tp.daily_min_prices(origin, destination, month)
        except Exception as e:
            log.warning("Не удалось получить обычную цену %s-%s: %s", origin, destination, e)
            return None

        window = [p for day, p in prices.items() if start <= day <= end]
        price = int(median(window)) if len(window) >= BASELINE_MIN_DAYS else None
        await self._db.save_baseline(origin, destination, price)
        log.info("Обычная цена %s-%s: %s ₽ (по %d дням)", origin, destination, price, len(window))
        return price

    async def _send(self, chat_id: int, text: str) -> bool:
        for _ in range(3):
            try:
                await self._bot.send_message(chat_id, text, disable_web_page_preview=True)
                return True
            except TelegramRetryAfter as e:
                log.warning("Telegram просит подождать %s с", e.retry_after)
                await asyncio.sleep(e.retry_after)
            except TelegramForbiddenError:
                log.info("Пользователь %s заблокировал бота — отписываю", chat_id)
                await self._db.remove_subscriber(chat_id)
                return False
        return False


def format_deal(deal: Deal, tp: TravelpayoutsClient, hot: bool = True) -> str:
    t = deal.ticket
    when = t.departure_at
    day = "сегодня" if when.date() == today() else when.strftime("%d.%m")
    transfers = "прямой" if t.transfers == 0 else f"пересадок: {t.transfers}"
    hours, minutes = divmod(t.duration_minutes, 60)

    price_line = f"💰 <b>{rub(t.price)}</b>"
    if deal.baseline:
        usual = f"обычно ~{rub(round(deal.baseline, -2))}"
        price_line += f" ({usual}, −{deal.discount}%)" if deal.discount > 0 else f" ({usual})"

    return (
        f"{'🔥' if hot else '✈️'} <b>{escape(tp.city_name(t.origin))} → {escape(tp.city_name(t.destination))}</b>\n"
        f"📅 {day}, {when:%H:%M} · {escape(tp.airline_name(t.airline))} · {transfers}"
        + (f" · {hours} ч {minutes} мин" if t.duration_minutes else "")
        + f"\n{price_line}\n"
        + f'<a href="{escape(t.link)}">Купить на Aviasales</a>'
    )


def rub(amount: int) -> str:
    return f"{amount:,} ₽".replace(",", " ")


def format_deals(deals: list[Deal], tp: TravelpayoutsClient, hot: bool = True) -> str:
    return "\n\n".join(format_deal(d, tp, hot) for d in deals)
