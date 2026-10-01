"""Периодическая проверка цен и рассылка горящих билетов."""

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, replace
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
    trip: bool = False  # найден по поездке пользователя, а не по ближайшим дням

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
        """Горящие билеты по настройкам пользователя: на ближайшие дни и на поездку."""
        views = _views(user)
        tickets = await self._fetch_tickets(views)
        candidates = [(v, self._candidates(v, tickets)) for v in views]
        baselines = await self._baselines(candidates, BASELINE_FETCHES_PER_MANUAL_CHECK)
        return _merge(self._evaluate(v, ts, baselines) for v, ts in candidates)

    async def cheapest_on_routes(self, user: UserSettings) -> list[Deal]:
        """Самый дешёвый билет по каждому маршруту пользователя, даже если он не горящий."""
        views = _views(user)
        tickets = await self._fetch_tickets(views)
        candidates = []
        for view in views:
            best: dict[tuple[str, str], Ticket] = {}
            for t in self._candidates(view, tickets, price_cap=False):
                route = (t.origin, t.destination)
                if route not in best or t.price < best[route].price:
                    best[route] = t
            candidates.append((view, list(best.values())))
        baselines = await self._baselines(candidates, BASELINE_FETCHES_PER_MANUAL_CHECK)
        return _merge(
            [Deal(t, baselines.get(_route(v, t)), trip=v.has_range) for t in ts] for v, ts in candidates
        )

    async def check_and_notify(self) -> None:
        users = [await self._db.settings(chat_id) for chat_id in await self._db.subscribers()]
        if not users:
            return

        views = {u.chat_id: _views(u) for u in users}
        all_views = [v for vs in views.values() for v in vs]
        background = asyncio.Semaphore(BACKGROUND_CONCURRENCY)
        tickets = await self._fetch_tickets(all_views, background)
        by_origin: dict[str, list[Ticket]] = defaultdict(list)
        for t in tickets:
            by_origin[t.origin].append(t)
        candidates = {
            id(v): (v, self._candidates(v, [t for o in v.origins for t in by_origin[o]])) for v in all_views
        }
        baselines = await self._baselines(list(candidates.values()), BASELINE_FETCHES_PER_CHECK, background)
        log.info("Получено билетов: %d, известно обычных цен: %d", len(tickets), len(baselines))

        for user in users:
            deals = _merge(self._evaluate(*candidates[id(v)], baselines) for v in views[user.chat_id])
            new = [
                d
                for d in deals
                if await self._db.is_new_or_cheaper(user.chat_id, d.ticket.key, d.ticket.price, RENOTIFY_DROP_PERCENT)
            ][:MAX_DEALS_PER_MESSAGE]
            if not new:
                continue
            # Все новые билеты — одним сообщением, чтобы не упираться в лимиты Telegram
            if await self._send(user.chat_id, format_deals(new, self.tp, trip_label=trip_label(user))):
                for d in new:
                    await self._db.mark_sent(user.chat_id, d.ticket.key, d.ticket.price)
                log.info("Пользователю %s отправлено билетов: %d", user.chat_id, len(new))
            await asyncio.sleep(0.05)  # Telegram разрешает ~30 сообщений в секунду

    async def _fetch_tickets(self, users: list[UserSettings], limit: asyncio.Semaphore | None = None) -> list[Ticket]:
        """Билеты, нужные всем переданным пользователям (их отслеживаниям, см. _views), —
        без повторных запросов по одному маршруту."""
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
                deals.append(Deal(t, baseline, trip=user.has_range))
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


def _views(user: UserSettings) -> list[UserSettings]:
    """Отслеживания пользователя, которые работают одновременно: поездка (свои даты) и ближайшие дни.

    Каждое — копия настроек: у поездки задан date_from/date_to, у ближайших дней — нет.
    Поездка идёт первой: если билет подходит под оба, он считается найденным по поездке.
    """
    views = [user] if user.has_range else []
    if user.days_ahead >= 0:
        views.append(replace(user, date_from=None, date_to=None))
    return views


def _merge(deal_lists) -> list[Deal]:
    """Объединяет билеты разных отслеживаний без повторов, самые выгодные — первыми."""
    seen, deals = set(), []
    for deal in (d for ds in deal_lists for d in ds):
        if deal.ticket.key not in seen:
            seen.add(deal.ticket.key)
            deals.append(deal)
    return sorted(deals, key=lambda d: (-(d.discount or 0), d.ticket.price))


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


def format_deals(deals: list[Deal], tp: TravelpayoutsClient, hot: bool = True, trip_label: str = "") -> str:
    """Билеты одним сообщением. Если есть билеты на поездку, они идут отдельным разделом с заголовком."""
    nearby = [d for d in deals if not d.trip]
    trip = [d for d in deals if d.trip]
    if not trip:
        return "\n\n".join(format_deal(d, tp, hot) for d in nearby)
    sections = []
    if nearby:
        sections.append("⏱ <b>Ближайшие дни</b>\n\n" + "\n\n".join(format_deal(d, tp, hot) for d in nearby))
    title = f"📆 <b>Ваша поездка {trip_label}</b>" if trip_label else "📆 <b>Ваша поездка</b>"
    sections.append(title + "\n\n" + "\n\n".join(format_deal(d, tp, hot) for d in trip))
    return "\n\n\n".join(sections)


def trip_label(user: UserSettings) -> str:
    """«28.12–08.01» или «30.12» для поездки пользователя."""
    if not user.has_range:
        return ""
    if user.date_from == user.date_to:
        return f"{user.date_from:%d.%m}"
    return f"{user.date_from:%d.%m}–{user.date_to:%d.%m}"
