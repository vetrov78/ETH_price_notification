import os
import asyncio
import logging

import requests
from telegram import Bot
from telegram.error import TelegramError
from dotenv import load_dotenv

# Настройка логов
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Загрузка конфига
load_dotenv("config.env")

class CryptoBot:
    def __init__(self):
        self.bot = Bot(token=os.getenv("TELEGRAM_TOKEN"))
        self.chat_id = os.getenv("CHAT_ID")
        self.coingecko_url = "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd"

    async def get_eth_price(self) -> float:
        """Получение текущей цены ETH через CoinGecko API"""
        try:
            response = requests.get(self.coingecko_url, timeout=10)
            data = response.json()
            return float(data["ethereum"]["usd"])
        except Exception as e:
            logger.error(f"Ошибка при получении цены ETH: {str(e)}")
            return None

    async def send_status(self, status: str):
        """Отправка статуса бота с текущей ценой ETH"""
        try:
            price = await self.get_eth_price()
            message = (
                f"{status}\n\n"
                f"• Текущая цена ETH: ${price:,.2f}" if price else "• Не удалось получить цену ETH"
            )
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=message
            )
            logger.info(f"Отправлено: {status}")
        except TelegramError as e:
            logger.error(f"Ошибка Telegram: {str(e)}")

async def main():
    bot = CryptoBot()

    # Уведомление о старте
    await bot.send_status("🚀 <b>Бот запущен</b>")

    try:
        # Основной асинхронный цикл бота
        while True:
            try:
                # Основная логика
                # Получаем цену ETH(синхронный запрос)
                price = requests.get(
                        "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd"
                    ).json()["ethereum"]["usd"]

                # print(f"Текущая цена ETH: ${price}")

                if price < float(os.getenv("ETH_CRITICAL_PRICE")):
                    message = f"🚨 ETH Price Alert! 🚨\nЦена ETH упала ниже ${os.getenv('ETH_CRITICAL_PRICE')}!\nТекущая цена: ${price}"
                    await bot.send_status(message)

                logger.info("Бот работает...")
                await asyncio.sleep(180)

            except Exception as e:
                logger.error(f"Ошибка в основном цикле: {str(e)}")
                await asyncio.sleep(5)

    except asyncio.CancelledError:
        await bot.send_status("🛑 <b>Бот остановлен</b>")
    finally:
        await bot.send_status("🛑 <b>Бот остановлен</b>")

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
