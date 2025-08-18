import os
import asyncio
import aiohttp
import logging
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv

# Настройка логов
logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),  # Логи в файл
        logging.StreamHandler()          # Логи в консоль
    ]
)
logger = logging.getLogger(__name__)

# Загрузка конфига
load_dotenv("config.env")

class CryptoBot:
    def __init__(self):
        self.token = os.getenv("TELEGRAM_TOKEN")
        self.chat_id = os.getenv("CHAT_ID")
        self.critical_price = float(os.getenv("ETH_CRITICAL_PRICE", 3000))
        self.check_interval = 300  # 5 минут
        self.app = Application.builder().token(self.token).build()
        self.session = aiohttp.ClientSession()

    async def get_eth_price(self) -> float:
        """Асинхронное получение цены ETH через CoinGecko API"""
        url = "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd"
        try:
            async with self.session.get(url, timeout=60) as response:
                data = await response.json()
                return float(data["ethereum"]["usd"])
        except Exception as e:
            logger.error(f"Ошибка при получении цены ETH: {str(e)}")
            return None

    async def send_alert(self, price: float):
        """Отправка предупреждения о падении цены"""
        message = (
            f"🚨 ETH Price Alert! 🚨\n"
            f"Цена ETH упала ниже ${self.critical_price}!\n"
            f"Текущая цена: ${price:,.2f}"
        )
        await self.send_message(message)

    async def send_message(self, text: str):
        """Универсальная отправка сообщений"""
        try:
            await self.app.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения: {str(e)}")

    async def price_check(self):
        """Основная функция проверки цены"""
        price = await self.get_eth_price()
        if price:
            logger.info(f"Текущая цена ETH: ${price:,.2f}")
            if price < self.critical_price:
                await self.send_alert(price)
        return price

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start"""
        await update.message.reply_text(
            "🤖 Бот для мониторинга цены ETH\n\n"
            "Автоматически проверяет цену каждые 3 минуты\n"
            f"Тревожный порог: ${self.critical_price}"
        )

    async def price_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /price"""
        price = await self.get_eth_price()
        if price:
            await update.message.reply_text(f"💰 Текущая цена ETH: ${price:,.2f}")
        else:
            await update.message.reply_text("Не удалось получить цену")

    async def run_checks(self):
        """Цикл проверки цены"""
        while True:
            try:
                await self.price_check()
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Ошибка в цикле проверки: {str(e)}")
                await asyncio.sleep(60)

    async def run(self):
        """Основной запуск бота"""
        # Регистрация команд
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("price", self.price_command))

        # Запуск бота
        await self.send_message("🚀 Бот запущен")
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()

        # Запуск фоновой задачи
        self.check_task = asyncio.create_task(self.run_checks())

        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            await self.shutdown()

    async def shutdown(self):
        """Корректное завершение работы"""
        self.check_task.cancel()
        await self.send_message("🛑 Бот остановлен")
        await self.session.close()
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()

async def main():
    bot = CryptoBot()
    try:
        await bot.run()
    except Exception as e:
        logger.error(f"Фатальная ошибка: {str(e)}")
        await bot.shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass