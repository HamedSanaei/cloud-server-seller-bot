import asyncio

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message

from cloud_platform.core.config import get_settings
from cloud_platform.core.i18n import Translator

dp = Dispatcher()

# User-facing strings come from the message catalog (M02-007), never
# scattered literals. The platform default locale is Persian.
_t = Translator()


@dp.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(_t.t("greeting.start"))


async def main() -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required to run the bot")
    bot = Bot(token=settings.telegram_bot_token)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
