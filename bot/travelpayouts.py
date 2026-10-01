"""Клиент Travelpayouts (Aviasales) Data API.

Документация: https://support.travelpayouts.com/hc/ru/articles/203956163
Цены берутся из кэша поисков пользователей Aviasales за последние ~48 часов,
поэтому они могут немного отставать от реальных.
"""

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlencode

import aiohttp

API_URL = "https://api.travelpayouts.com"
AVIASALES_URL = "https://www.aviasales.ru"

# Разговорные названия, которых нет в справочнике
CITY_ALIASES = {
    "питер": "LED",
    "спб": "LED",
    "петербург": "LED",
    "мск": "MOW",
}

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Ticket:
    origin: str
    destination: str
    price: int
    airline: str
    flight_number: str
    departure_at: datetime
    transfers: int
    duration_minutes: int
    link: str

    @property
    def key(self) -> str:
        """Идентификатор рейса без цены — чтобы не присылать один и тот же билет дважды."""
        return f"{self.origin}-{self.destination}-{self.departure_at.isoformat()}-{self.airline}{self.flight_number}"


class TravelpayoutsClient:
    def __init__(self, token: str, marker: str, cache_dir: Path):
        self._token = token
        self._marker = marker
        self._cache_dir = cache_dir
        self._session: aiohttp.ClientSession | None = None
        self._cities: dict[str, str] = {}
        self._city_codes: dict[str, str] = {}
        self._airlines: dict[str, str] = {}

    async def __aenter__(self) -> "TravelpayoutsClient":
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        await self._load_directories()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session:
            await self._session.close()

    def city_name(self, code: str) -> str:
        return self._cities.get(code, code)

    def airline_name(self, code: str) -> str:
        return self._airlines.get(code, code)

    def find_city(self, query: str) -> str | None:
        """IATA-код города по названию («Волгоград», «питер») или по самому коду («VOG»)."""
        q = _normalize(query)
        if q in CITY_ALIASES:
            return CITY_ALIASES[q]
        if q in self._city_codes:
            return self._city_codes[q]
        code = query.strip().upper()
        return code if code in self._cities else None

    async def cheapest(self, origin: str, day: date, destination: str | None = None) -> list[Ticket]:
        """Самые дешёвые билеты в одну сторону на дату day: в destination или во все направления."""
        params = {
            "origin": origin,
            "departure_at": day.isoformat(),
            "one_way": "true",
            "sorting": "price",
            "limit": 1000,
        }
        if destination:
            params["destination"] = destination
        data = await self._get("/aviasales/v3/prices_for_dates", params)
        return [self._parse_ticket(item) for item in data]

    async def daily_min_prices(self, origin: str, destination: str, month: str) -> dict[date, int]:
        """Минимальная цена на каждый день месяца month (формат «2026-10»)."""
        params = {
            "origin": origin,
            "destination": destination,
            "group_by": "departure_at",
            "departure_at": month,
        }
        data = await self._get("/aviasales/v3/grouped_prices", params)
        return {date.fromisoformat(day): int(item["price"]) for day, item in (data or {}).items()}

    async def _get(self, path: str, params: dict):
        params = {**params, "currency": "rub", "token": self._token}
        async with self._session.get(API_URL + path, params=params) as resp:
            if resp.status == 401:
                raise RuntimeError("Travelpayouts: неверный TRAVELPAYOUTS_TOKEN")
            if resp.status != 200:
                raise RuntimeError(f"Travelpayouts вернул {resp.status}: {(await resp.text())[:200]}")
            body = await resp.json(content_type=None)
        if not body.get("success"):
            raise RuntimeError(f"Travelpayouts вернул ошибку: {body}")
        return body.get("data") or []

    def _parse_ticket(self, item: dict) -> Ticket:
        return Ticket(
            origin=item["origin"],
            destination=item["destination"],
            price=int(item["price"]),
            airline=item.get("airline", ""),
            flight_number=str(item.get("flight_number", "")),
            departure_at=datetime.fromisoformat(item["departure_at"]),
            transfers=int(item.get("transfers", 0)),
            duration_minutes=int(item.get("duration_to") or item.get("duration") or 0),
            link=self._build_link(item.get("link", "")),
        )

    def _build_link(self, path: str) -> str:
        if not path:
            return AVIASALES_URL
        url = AVIASALES_URL + path
        if self._marker:
            url += ("&" if "?" in url else "?") + urlencode({"marker": self._marker})
        return url

    async def _load_directories(self) -> None:
        """Справочники городов и авиакомпаний для красивых названий в уведомлениях."""
        cities = await self._fetch_cached("cities.json")
        self._cities = {c["code"]: c.get("name") or c["code"] for c in cities}
        # Если названия совпадают, приоритет у городов с действующим аэропортом
        for c in sorted(cities, key=lambda c: bool(c.get("has_flightable_airport"))):
            if c.get("name"):
                self._city_codes[_normalize(c["name"])] = c["code"]

        airlines = await self._fetch_cached("airlines.json")
        self._airlines = {a["code"]: a.get("name") or a["code"] for a in airlines if a.get("code")}

    async def _fetch_cached(self, name: str) -> list[dict]:
        path = self._cache_dir / name
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))

        try:
            async with self._session.get(f"{API_URL}/data/ru/{name}") as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as e:
            log.warning("Не удалось загрузить справочник %s: %s", name, e)
            return []

        self._cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data


def _normalize(name: str) -> str:
    return name.strip().lower().replace("ё", "е")
