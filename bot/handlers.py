"""Команды бота и меню настроек."""

import logging
import re
from datetime import date, timedelta
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .checker import MAX_DEALS_PER_MESSAGE, Checker, format_deals, rub
from .config import today
from .db import CityKind, Database, UserSettings

router = Router()

BOT_COMMANDS = [
    BotCommand(command="check", description="Найти горящие билеты сейчас"),
    BotCommand(command="settings", description="Настройки"),
    BotCommand(command="from", description="Откуда лететь: /from Москва"),
    BotCommand(command="to", description="Куда лететь: /to Волгоград"),
    BotCommand(command="start", description="Подписаться на уведомления"),
    BotCommand(command="stop", description="Отписаться от уведомлений"),
]

# Профиль бота: короткое описание (до 120 символов) видно в профиле и при пересылке,
# полное (до 512) — на пустом экране чата до нажатия «Старт»
BOT_SHORT_DESCRIPTION = "🔥 Горящие авиабилеты на сегодня и завтра — сильно дешевле обычной цены. Автор: @luvv_life"
BOT_DESCRIPTION = (
    "🔥 Ищу горящие авиабилеты: сильно дешевле обычной цены, с вылетом сегодня и завтра.\n\n"
    "✈️ Выберите города вылета и направления\n"
    "💸 Задайте скидку и потолок цены\n"
    "📆 Или следите за своими датами — например, домой на новогодние каникулы\n"
    "🔔 Получайте уведомление, как только появится выгодный билет\n\n"
    "Нажмите «Старт», чтобы подписаться.\n\n"
    "Автор: @luvv_life"
)

DISCOUNT_OPTIONS = [20, 30, 40, 50, 60, 70]
PRICE_OPTIONS = [2000, 3000, 3500, 5000, 7000, 10000]
DAYS_OPTIONS = {0: "Только сегодня", 1: "Сегодня и завтра", 2: "3 дня", 6: "Неделя"}
MAX_RANGE_DAYS = 62  # свой период — не длиннее двух месяцев

CITY_TEXT = {
    "from": ("🛫 Города вылета", "Напишите город вылета, например: <code>Москва</code>"),
    "to": ("🛬 Направления", "Напишите город назначения, например: <code>Волгоград</code>"),
}

# Проверка билетов — главная функция, поэтому кнопка есть на каждом экране
CHECK_BUTTON = ("🔎 Проверить билеты", "check")
BACK_ROW = [("« Назад", "menu:main"), CHECK_BUTTON]
SETTINGS_BACK_ROW = [("« Настройки", "menu:settings"), CHECK_BUTTON]


class Input(StatesGroup):
    city_from = State()
    city_to = State()
    max_price = State()
    date_range = State()


# --- команды ---


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, db: Database, checker: Checker) -> None:
    await state.clear()
    await db.add_subscriber(message.chat.id)
    user = await db.settings(message.chat.id)
    await message.answer(
        "✈️ Вы подписаны на горящие билеты!\n\n" + main_text(user, checker) + "\n\n/stop — отписаться",
        reply_markup=main_keyboard(user),
    )


@router.message(Command("stop"))
async def cmd_stop(message: Message, state: FSMContext, db: Database) -> None:
    await state.clear()
    await db.remove_subscriber(message.chat.id)
    await message.answer("Вы отписались. Настройки сохранены — вернуться можно командой /start")


@router.message(Command("settings"))
async def cmd_settings(message: Message, state: FSMContext, db: Database) -> None:
    await state.clear()
    user = await db.settings(message.chat.id)
    await message.answer(settings_text(user), reply_markup=settings_keyboard(user))


@router.message(Command("from", "to"))
async def cmd_city(message: Message, command: CommandObject, state: FSMContext, db: Database, checker: Checker) -> None:
    kind: CityKind = command.command
    if command.args:
        await add_cities(message, kind, command.args, db, checker)
    await show_cities(message, kind, state, db, checker)


@router.message(Command("check"))
async def cmd_check(message: Message, state: FSMContext, db: Database, checker: Checker) -> None:
    await state.clear()
    await run_check(message, db, checker)


@router.callback_query(F.data == "check")
async def cb_check(query: CallbackQuery, state: FSMContext, db: Database, checker: Checker) -> None:
    await state.clear()
    await query.answer()
    await run_check(query.message, db, checker)


async def run_check(message: Message, db: Database, checker: Checker) -> None:
    status = await message.answer("🔎 Ищу билеты…")
    user = await db.settings(message.chat.id)
    try:
        deals = await checker.find_deals(user)
        cheapest = await checker.cheapest_on_routes(user) if user.destinations and not deals else []
    except Exception as e:
        logging.exception("Ошибка ручной проверки")
        text = f"⚠️ Не удалось получить цены: {escape(str(e))}"
    else:
        if deals:
            text = format_deals(deals[:MAX_DEALS_PER_MESSAGE], checker.tp)
            if len(deals) > MAX_DEALS_PER_MESSAGE:
                text += f"\n\n…и ещё {len(deals) - MAX_DEALS_PER_MESSAGE}"
        elif cheapest:
            text = "Горящих билетов сейчас нет. Самые дешёвые по вашим направлениям:\n\n" + format_deals(
                cheapest[:MAX_DEALS_PER_MESSAGE], checker.tp, hot=False
            )
        else:
            text = "Сейчас горящих билетов нет 🤷\n\nМожно снизить скидку или поднять потолок цены в настройках."

    markup = _keyboard([("🔄 Проверить ещё раз", "check"), ("🏠 Меню", "menu:new")])
    await status.edit_text(text, reply_markup=markup, disable_web_page_preview=True)


# --- главный экран ---


def main_text(user: UserSettings, checker: Checker) -> str:
    origins = ", ".join(checker.tp.city_name(c) for c in user.origins)
    dests = ", ".join(checker.tp.city_name(c) for c in user.destinations) or "все направления"
    return (
        f"🛫 Откуда: <b>{escape(origins)}</b>\n"
        f"🛬 Куда: <b>{escape(dests)}</b>\n"
        f"📉 Скидка от обычной цены: <b>от {user.min_discount}%</b>\n"
        f"💰 Потолок цены: <b>{price_label(user.max_price)}</b>\n"
        f"📅 Даты: <b>{dates_label(user).lower()}</b>"
    )


def main_keyboard(user: UserSettings) -> InlineKeyboardMarkup:
    return _keyboard(
        [CHECK_BUTTON],
        [("🛫 Откуда", "cities:from"), ("🛬 Куда", "cities:to")],
        [(f"📅 {dates_label(user)}", "menu:days"), ("⚙️ Настройки", "menu:settings")],
    )


@router.callback_query(F.data == "menu:main")
async def cb_main(query: CallbackQuery, state: FSMContext, db: Database, checker: Checker) -> None:
    await state.clear()
    user = await db.settings(query.message.chat.id)
    await _edit(query, main_text(user, checker), main_keyboard(user))


@router.callback_query(F.data == "menu:new")
async def cb_main_new(query: CallbackQuery, state: FSMContext, db: Database, checker: Checker) -> None:
    """Главный экран новым сообщением, чтобы не затирать найденные билеты."""
    await state.clear()
    user = await db.settings(query.message.chat.id)
    await query.message.answer(main_text(user, checker), reply_markup=main_keyboard(user))
    await query.answer()


# --- настройки: скидка и потолок цены ---


def settings_text(user: UserSettings) -> str:
    return (
        "⚙️ <b>Настройки</b>\n\n"
        f"📉 Скидка от обычной цены: <b>от {user.min_discount}%</b>\n"
        f"💰 Потолок цены: <b>{price_label(user.max_price)}</b>"
    )


def settings_keyboard(user: UserSettings) -> InlineKeyboardMarkup:
    return _keyboard(
        [(f"📉 Скидка: {user.min_discount}%", "menu:discount"), (f"💰 {price_label(user.max_price)}", "menu:price")],
        BACK_ROW,
    )


@router.callback_query(F.data == "menu:settings")
async def cb_settings(query: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.clear()
    await _show_settings(query, db)


@router.callback_query(F.data == "menu:discount")
async def cb_discount_menu(query: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.clear()
    user = await db.settings(query.message.chat.id)
    buttons = [(_mark(f"{d}%", d == user.min_discount), f"set:min_discount:{d}") for d in DISCOUNT_OPTIONS]
    await _edit(
        query,
        "📉 На сколько процентов билет должен быть дешевле обычной цены маршрута?\n\n"
        "Чем больше процент, тем реже, но выгоднее уведомления.",
        _keyboard(buttons[:3], buttons[3:], SETTINGS_BACK_ROW),
    )


@router.callback_query(F.data == "menu:price")
async def cb_price_menu(query: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.clear()
    user = await db.settings(query.message.chat.id)
    buttons = [(_mark(rub(p), p == user.max_price), f"set:max_price:{p}") for p in PRICE_OPTIONS]
    await _edit(
        query,
        "💰 Дороже какой суммы билеты не присылать, даже если скидка большая?\n\n"
        "Без потолка бот пришлёт и дальние направления: например, Владивосток за 12 000 ₽ при обычных 30 000 ₽.",
        _keyboard(
            buttons[:3],
            buttons[3:],
            [(_mark("Без потолка", user.max_price == 0), "set:max_price:0"), ("✏️ Своя сумма", "input:max_price")],
            SETTINGS_BACK_ROW,
        ),
    )


@router.callback_query(F.data == "menu:days")
async def cb_days_menu(query: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.clear()
    user = await db.settings(query.message.chat.id)
    buttons = [
        (_mark(label, not user.has_range and d == user.days_ahead), f"set:days_ahead:{d}")
        for d, label in DAYS_OPTIONS.items()
    ]
    custom = _mark(f"📆 {dates_label(user)}" if user.has_range else "📆 Свои даты", user.has_range)
    await _edit(
        query,
        "📅 На какие даты искать билеты?\n\n"
        "«Свои даты» — если ждёте билет на конкретную поездку, например домой на новогодние каникулы. "
        "Бот будет следить за ценами на эти даты и пришлёт билет, когда он заметно подешевеет.",
        _keyboard(buttons[:2], buttons[2:], [(custom, "input:dates")], BACK_ROW),
    )


@router.callback_query(F.data == "input:dates")
async def cb_input_dates(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Input.date_range)
    await query.answer()
    await query.message.answer(
        "📆 Напишите даты вылета, например:\n"
        "<code>28.12-08.01</code> — с 28 декабря по 8 января\n"
        "<code>30.12</code> — только 30 декабря",
        reply_markup=_keyboard(BACK_ROW),
    )


@router.message(Input.date_range, F.text, ~F.text.startswith("/"))
async def input_dates(message: Message, state: FSMContext, db: Database, checker: Checker) -> None:
    try:
        date_from, date_to = parse_date_range(message.text)
    except ValueError as e:
        await message.answer(f"🤔 {e}\nНапример: <code>28.12-08.01</code>")
        return
    await state.clear()
    await db.set_date_range(message.chat.id, date_from, date_to)
    user = await db.settings(message.chat.id)
    await message.answer("Сохранено ✅\n\n" + main_text(user, checker), reply_markup=main_keyboard(user))


def parse_date_range(text: str) -> tuple[date, date]:
    """«28.12-08.01», «28.12.2026 - 8.1.2027», «30.12» → (с, по). Год можно не писать: берётся ближайший."""
    found = re.findall(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?", text)
    if not 1 <= len(found) <= 2:
        raise ValueError("Не понял даты.")
    start = today()
    dates = []
    for day, month, year in found:
        try:
            if year:
                d = date(int(year) + (2000 if len(year) == 2 else 0), int(month), int(day))
            else:
                prev = dates[-1] if dates else start
                d = date(prev.year, int(month), int(day))
                if d < prev:  # 28.12-08.01: январь — уже следующего года
                    d = d.replace(year=d.year + 1)
        except ValueError:
            raise ValueError("Такой даты нет.") from None
        dates.append(d)

    date_from, date_to = dates[0], dates[-1]
    if date_to < date_from:
        raise ValueError("Дата «по» раньше даты «с».")
    if date_to < start:
        raise ValueError("Эти даты уже прошли.")
    if (date_to - date_from).days + 1 > MAX_RANGE_DAYS:
        raise ValueError(f"Слишком длинный период: можно не больше {MAX_RANGE_DAYS} дней.")
    if date_from > start + timedelta(days=365):
        raise ValueError("Так далеко вперёд цен ещё нет.")
    return date_from, date_to


@router.callback_query(F.data.startswith("set:"))
async def cb_set(query: CallbackQuery, state: FSMContext, db: Database, checker: Checker) -> None:
    await state.clear()
    _, field, value = query.data.split(":")
    await db.update_setting(query.message.chat.id, field, int(value))
    await query.answer("Сохранено ✅")
    if field == "days_ahead":  # даты выбираются с главного экрана
        await db.set_date_range(query.message.chat.id, None, None)
        user = await db.settings(query.message.chat.id)
        await _edit(query, main_text(user, checker), main_keyboard(user))
    else:
        await _show_settings(query, db)


@router.callback_query(F.data == "input:max_price")
async def cb_input_price(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Input.max_price)
    await query.answer()
    await query.message.answer(
        "✏️ Напишите максимальную цену в рублях, например: <code>4500</code>",
        reply_markup=_keyboard(SETTINGS_BACK_ROW),
    )


@router.message(Input.max_price, F.text, ~F.text.startswith("/"))
async def input_price(message: Message, state: FSMContext, db: Database) -> None:
    digits = re.sub(r"\D", "", message.text)
    if not digits or not 0 < int(digits) <= 1_000_000:
        await message.answer("Не понял сумму 🤔 Напишите число, например: <code>4500</code>")
        return
    await state.clear()
    await db.update_setting(message.chat.id, "max_price", int(digits))
    user = await db.settings(message.chat.id)
    await message.answer("Сохранено ✅\n\n" + settings_text(user), reply_markup=settings_keyboard(user))


# --- города вылета и назначения ---


@router.callback_query(F.data.startswith("cities:"))
async def cb_cities(query: CallbackQuery, state: FSMContext, db: Database, checker: Checker) -> None:
    await show_cities(query.message, query.data.split(":")[1], state, db, checker, edit=True)
    await query.answer()


@router.message(Input.city_from, F.text, ~F.text.startswith("/"))
@router.message(Input.city_to, F.text, ~F.text.startswith("/"))
async def input_city(message: Message, state: FSMContext, db: Database, checker: Checker) -> None:
    kind: CityKind = "from" if await state.get_state() == Input.city_from.state else "to"
    if await add_cities(message, kind, message.text, db, checker):
        await show_cities(message, kind, state, db, checker)


@router.callback_query(F.data.startswith("rmcity:"))
async def cb_remove_city(query: CallbackQuery, state: FSMContext, db: Database, checker: Checker) -> None:
    _, kind, code = query.data.split(":")
    chat_id = query.message.chat.id
    if kind == "from" and len(await db.cities(chat_id, "from")) <= 1:
        await query.answer("Нужен хотя бы один город вылета. Сначала добавьте другой.", show_alert=True)
        return
    await db.remove_city(chat_id, kind, code)
    await query.answer("Убрал")
    await show_cities(query.message, kind, state, db, checker, edit=True)


@router.callback_query(F.data == "allcities:to")
async def cb_all_destinations(query: CallbackQuery, state: FSMContext, db: Database, checker: Checker) -> None:
    await db.clear_cities(query.message.chat.id, "to")
    await query.answer("Слежу за всеми направлениями")
    await show_cities(query.message, "to", state, db, checker, edit=True)


async def add_cities(message: Message, kind: CityKind, text: str, db: Database, checker: Checker) -> bool:
    """Добавляет города из текста через запятую. Возвращает True, если добавлен хотя бы один."""
    user = await db.settings(message.chat.id)
    added, unknown = [], []
    for name in filter(None, (n.strip() for n in text.split(","))):
        code = checker.tp.find_city(name)
        clash = user.destinations if kind == "from" else user.origins
        if code is None or code in clash:
            unknown.append(name)
            continue
        await db.add_city(message.chat.id, kind, code)
        added.append(checker.tp.city_name(code))

    if added:
        await message.answer(f"✅ Добавил: {escape(', '.join(added))}")
    if unknown:
        await message.answer(
            f"🤔 Не получилось добавить: {escape(', '.join(unknown))}.\n"
            "Проверьте название. Город вылета и назначения не могут совпадать."
        )
    return bool(added)


async def show_cities(
    message: Message, kind: CityKind, state: FSMContext, db: Database, checker: Checker, edit: bool = False
) -> None:
    """Список городов. Сразу ждёт ввода нового города, без отдельной кнопки «Добавить»."""
    await state.set_state(Input.city_from if kind == "from" else Input.city_to)
    codes = await db.cities(message.chat.id, kind)
    title, prompt = CITY_TEXT[kind]
    rows = [[(f"❌ {checker.tp.city_name(c)}", f"rmcity:{kind}:{c}")] for c in codes]

    if kind == "to" and not codes:
        text = f"{title}: 🌍 <b>все</b>.\n\nЧтобы следить только за нужными городами, напишите их."
    else:
        text = f"{title} (нажмите на город, чтобы убрать)."
        if kind == "to":
            rows.append([("🌍 Все направления", "allcities:to")])
    text += f"\n\n✏️ {prompt}\nМожно несколько через запятую."
    rows.append(BACK_ROW)

    markup = _keyboard(*rows)
    if edit:
        await _edit_message(message, text, markup)
    else:
        await message.answer(text, reply_markup=markup)


# --- вспомогательное ---


async def _show_settings(query: CallbackQuery, db: Database) -> None:
    user = await db.settings(query.message.chat.id)
    await _edit(query, settings_text(user), settings_keyboard(user))


async def _edit(query: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    await _edit_message(query.message, text, markup)
    try:
        await query.answer()
    except TelegramBadRequest:
        pass  # на этот callback уже ответили


async def _edit_message(message: Message, text: str, markup: InlineKeyboardMarkup) -> None:
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


def _keyboard(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


def _mark(label: str, selected: bool) -> str:
    return f"✅ {label}" if selected else label


def price_label(max_price: int) -> str:
    return rub(max_price) if max_price else "без потолка"


def dates_label(user: UserSettings) -> str:
    if user.has_range:
        if user.date_from == user.date_to:
            return f"{user.date_from:%d.%m}"
        return f"{user.date_from:%d.%m}–{user.date_to:%d.%m}"
    return DAYS_OPTIONS.get(user.days_ahead, f"{user.days_ahead + 1} дн.")
