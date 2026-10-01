import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from .checker import Checker
from .config import DATA_DIR, TZ, load_config
from .db import Database, Defaults
from .handlers import BOT_COMMANDS, BOT_DESCRIPTION, BOT_SHORT_DESCRIPTION, router
from .travelpayouts import TravelpayoutsClient


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config()
    logging.info("База данных: %s, часовой пояс: %s", config.db_path.resolve(), TZ)
    if not config.tp_token:
        logging.warning("TRAVELPAYOUTS_TOKEN не задан — бот запустится, но искать билеты не сможет")

    defaults = Defaults(
        origin=config.origin,
        min_discount=config.min_discount_percent,
        max_price=config.max_price,
        days_ahead=config.days_ahead,
    )
    db = Database(config.db_path, defaults)
    await db.connect()
    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.set_my_commands(BOT_COMMANDS)
    await bot.set_my_description(BOT_DESCRIPTION)
    await bot.set_my_short_description(BOT_SHORT_DESCRIPTION)

    async with TravelpayoutsClient(config.tp_token, config.tp_marker, DATA_DIR) as tp:
        checker = Checker(config, db, tp, bot)
        dp = Dispatcher(db=db, checker=checker)
        dp.include_router(router)

        check_task = asyncio.create_task(checker.run_forever())
        try:
            await dp.start_polling(bot)
        finally:
            check_task.cancel()
            await db.close()


if __name__ == "__main__":
    asyncio.run(main())
