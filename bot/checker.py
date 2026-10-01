"""Периодическая проверка цен и рассылка горящих билетов."""

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
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
# Для своего периода «обычная» цена — медиана по самому периоду и неделе до и после него:
# новогодние билеты надо сравнивать с новогодними ценами, а не с октябрьскими
RANGE_BASELINE_MARGIN = timedelta(days=7)
# По «всем направлениям» маршрутов сотни. За одну проверку считаем ограниченное число новых
# обычных цен (самые дешёвые билеты — первыми), остальные досчитываются в следующих проверках.
# Ручная проверка — меньше, чтобы пользователь не ждал долго.
BASELINE_FETCHES_PER_CHECK = 200
BASELINE_FETCHES_PER_MANUAL_CHECK = 10
# Фоновая проверка держит в очереди к API не больше стольких запросов сразу,
# чтобы запросы пользователей, нажавших «Проверить», не ждали за тысячами фоновых
BACKGROUND_CONCURRENCY = 4

# Сколько билетов максимум присылать одному пользователю за одну проверку (одним сообщением)
MAX_DEALS_PER_MESSAGE = 10

# Уже отправленный билет присылаем повторно, только если он подешевел хотя бы на столько процентов —
# чтобы не беспокоить из-за колебаний цены на несколько рублей
RENOTIFY_DROP_PERCENT = 10

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


@dataclass(frozen=True)
class _Route:
    """Маршрут и период, для которых считается обычная цена."""

    origin: str
    destination: str
    start: date
    end: date
    period: str  # ключ в кэше: '' — ближайшие дни, иначе свой период пользователя


# Обычные цены, известные в этой проверке. Маршрута нет в словаре — цену пока не знаем.
Baselines = dict[_Route, int | None]


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
        tickets = self._candidates(user, await self._fetch_tickets([user]))
        baselines = await self._baselines([(user, tickets)], BASELINE_FETCHES_PER_MANUAL_CHECK)
        return self._evaluate(user, tickets, baselines)

    async def cheapest_on_routes(self, user: UserSettings) -> list[Deal]:
        """Самый дешёвый билет по каждому маршруту пользователя, даже если он не горящий."""
        best: dict[tuple[str, str], Ticket] = {}
        for t in self._candidates(user, await self._fetch_tickets([user]), price_cap=False):
            route = (t.origin, t.destination)
            if route not in best or t.price < best[route].price:
                best[route] = t
        tickets = list(best.values())
        baselines = await self._baselines([(user, tickets)], BASELINE_FETCHES_PER_MANUAL_CHECK)
        return [Deal(t, baselines.get(_route(user, t))) for t in tickets]

    async def check_and_notify(self) -> None:
        users = [await self._db.settings(chat_id) for chat_id in await self._db.subscribers()]
        if not users:
            return

        background = asyncio.Semaphore(BACKGROUND_CONCURRENCY)
        tickets = await self._fetch_tickets(users, background)
        by_origin: dict[str, list[Ticket]] = defaultdict(list)
        for t in tickets:
            by_origin[t.origin].append(t)
        candidates = [(u, self._candidates(u, [t for o in u.origins for t in by_origin[o]])) for u in users]
        baselines = await self._baselines(candidates, BASELINE_FETCHES_PER_CHECK, background)
        log.info("Получено билетов: %d, известно обычных цен: %d", len(tickets), len(baselines))

        for user, user_tickets in candidates:
            new = [
                d
                for d in self._evaluate(user, user_tickets, baselines)
                if await self._db.is_new_or_cheaper(user.chat_id, d.ticket.key, d.ticket.price, RENOTIFY_DROP_PERCENT)
            ][:MAX_DEALS_PER_MESSAGE]
            if not new:
                continue
            # Все новые билеты — одним сообщением, чтобы не упираться в лимиты Telegram
            if await self._send(user.chat_id, format_deals(new, self.tp)):
                for d in new:
                    await self._db.mark_sent(user.chat_id, d.ticket.key, d.ticket.price)
                log.info("Пользователю %s отправлено билетов: %d", user.chat_id, len(new))
            await asyncio.sleep(0.05)  # Telegram разрешает ~30 сообщений в секунду

    async def _fetch_tickets(self, users: list[UserSettings], limit: asyncio.Semaphore | None = None) -> list[Ticket]:
        """Билеты, нужные всем переданным пользователям, — без повторных запросов по одному маршруту."""
        if not self.config.tp_token:
            raise RuntimeError("TRAVELPAYOUTS_TOKEN не задан в .env")

        # (город вылета, направление или None — все направления, день или месяц)
        queries: set[tuple[str, str | None, str]] = set()
        for user in users:
            for origin in user.origins:
                for dest in [d for d in user.destinations if d != origin] or [None]:
                    queries.update((origin, dest, when) for when in _query_periods(user, dest))

        # Запросы идут параллельно, скорость ограничивает клиент API
        results = await asyncio.gather(
            *(_limited(limit, self.tp.cheapest(origin, when, dest)) for origin, dest, when in queries),
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, Exception)]
        if errors:
            log.warning("Не удалось выполнить %d из %d запросов билетов: %s", len(errors), len(results), errors[0])
            if len(errors) == len(results):
                raise errors[0]

        now = datetime.now().astimezone()
        tickets = {t.key: t for r in results if not isinstance(r, Exception) for t in r}
        return [t for t in tickets.values() if t.departure_at > now]

    @staticmethod
    def _candidates(user: UserSettings, tickets: list[Ticket], price_cap: bool = True) -> list[Ticket]:
        """Билеты, подходящие пользователю по городам, датам и (если price_cap) потолку цены."""
        first_day, last_day = _search_dates(user)
        origins, dests = set(user.origins), set(user.destinations)
        return [
            t
            for t in tickets
            if t.origin in origins
            and (not dests or t.destination in dests)
            and first_day <= t.departure_at.date() <= last_day
            and not (price_cap and user.max_price and t.price > user.max_price)
        ]

    @staticmethod
    def _evaluate(user: UserSettings, tickets: list[Ticket], baselines: Baselines) -> list[Deal]:
        """Оставляет горящие билеты из подходящих пользователю, самые выгодные — первыми."""
        deals = []
        for t in tickets:
            route = _route(user, t)
            if route not in baselines:
                continue  # обычную цену ещё не знаем — решим в следующей проверке
            baseline = baselines[route]
            if baseline:
                hot = t.price <= baseline * (1 - user.min_discount / 100)
            else:
                # Нет данных об обычной цене — ориентируемся только на потолок цены
                hot = bool(user.max_price)
            if hot:
                deals.append(Deal(t, baseline))
        return sorted(deals, key=lambda d: (-(d.discount or 0), d.ticket.price))

    async def _baselines(
        self,
        candidates: list[tuple[UserSettings, list[Ticket]]],
        limit: int,
        concurrency: asyncio.Semaphore | None = None,
    ) -> Baselines:
        """Обычные цены для маршрутов всех билетов: из кэша в базе, а недостающие — из API,
        не больше limit новых за раз (маршруты с самыми дешёвыми билетами — первыми)."""
        cheapest: dict[_Route, int] = {}
        for user, tickets in candidates:
            for t in tickets:
                route = _route(user, t)
                cheapest[route] = min(t.price, cheapest.get(route, t.price))

        known: Baselines = {}
        missing = []
        for route in sorted(cheapest, key=cheapest.get):
            found, price = await self._db.get_baseline(route.origin, route.destination, route.period, BASELINE_TTL)
            if found:
                known[route] = price
            else:
                missing.append(route)

        computed = await asyncio.gather(
            *(_limited(concurrency, self._compute_baseline(r)) for r in missing[:limit]), return_exceptions=True
        )
        for route, price in zip(missing, computed):
            if isinstance(price, Exception):
                log.warning("Не удалось получить обычную цену %s-%s: %s", route.origin, route.destination, price)
            else:
                known[route] = price
        if len(missing) > limit:
            log.info("Обычных цен отложено до следующей проверки: %d", len(missing) - limit)
        return known

    async def _compute_baseline(self, route: _Route) -> int | None:
        """Медиана минимальных цен по дням за период. None — данных по маршруту мало."""
        prices: dict[date, int] = {}
        for month in _months(route.start, route.end):
            prices |= await self.tp.daily_min_prices(route.origin, route.destination, month)
        window = [p for day, p in prices.items() if route.start <= day <= route.end]
        price = int(median(window)) if len(window) >= BASELINE_MIN_DAYS else None
        await self._db.save_baseline(route.origin, route.destination, route.period, price, BASELINE_TTL)
        log.info(
            "Обычная цена %s-%s (%s): %s ₽ (по %d дням)",
            route.origin, route.destination, route.period or "ближайшие дни", price, len(window),
        )
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


async def _limited(semaphore: asyncio.Semaphore | None, coro):
    if semaphore is None:
        return await coro
    async with semaphore:
        return await coro


def _route(user: UserSettings, t: Ticket) -> _Route:
    """Маршрут билета и период, по которому для этого пользователя считается обычная цена."""
    if user.has_range:
        start = max(user.date_from - RANGE_BASELINE_MARGIN, today())
        end = user.date_to + RANGE_BASELINE_MARGIN
        return _Route(t.origin, t.destination, start, end, f"{start}:{end}")
    return _Route(t.origin, t.destination, today(), today() + timedelta(days=BASELINE_DAYS), "")


def _search_dates(user: UserSettings) -> tuple[date, date]:
    """Первый и последний день вылета, которые интересны пользователю."""
    if user.has_range:
        return max(user.date_from, today()), user.date_to
    return today(), today() + timedelta(days=user.days_ahead)


def _months(start: date, end: date) -> list[str]:
    """Месяцы («2026-12») от start до end включительно."""
    months, day = [], start.replace(day=1)
    while day <= end:
        months.append(day.strftime("%Y-%m"))
        day = (day + timedelta(days=32)).replace(day=1)
    return months


def _query_periods(user: UserSettings, destination: str | None) -> set[str]:
    """Дни или месяцы, которые нужно запросить в API.

    Запрос за месяц по конкретному направлению возвращает самый дешёвый билет на каждый день —
    для своего периода это экономит запросы. По всем направлениям месяц даёт лишь один билет
    в каждый город за весь месяц, поэтому там спрашиваем по дням.
    """
    first_day, last_day = _search_dates(user)
    if user.has_range and destination:
        return set(_months(first_day, last_day))
    return {(first_day + timedelta(days=i)).isoformat() for i in range((last_day - first_day).days + 1)}


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
