"""Точка входа: `python main.py`. Хостинг bothost.ru находит этот файл автоматически."""

import asyncio

from bot.main import main

if __name__ == "__main__":
    asyncio.run(main())
