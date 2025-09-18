import os
import asyncio
import aiohttp
import logging
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

# --- Логи ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# --- Конфиг ---
load_dotenv("config.env")

# --- Настройки монет ---
COINS = {
    "ETH": "ethereum",
    "AERO": "aerodrome-finance",
    "CRV": "curve-dao-token"
}

THRESHOLDS = {
    "ETH": float(os.getenv("ETH_CRITICAL_PRICE", 3000)),
    "CRV": float(os.getenv("CRV_CRITICAL_PRICE", 1.0)),
    "AERO": float(os.getenv("AERO_CRITICAL_PRICE", 1.35))
}

# --- Настройки бота ---
VAULT_API_URL = "https://api.prod.paradex.trade/v1/vaults"
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 300))  # в секундах
DAILY_HOUR = int(os.getenv("DAILY_REPORT_HOUR", 9))
DAILY_MINUTE = int(os.getenv("DAILY_REPORT_MINUTE", 0))

# --- Логика бота ---
class CryptoBot:
    def __init__(self, session, app, chat_id):
        self.session = session
        self.app = app
        self.chat_id = chat_id
        self.scheduler = AsyncIOScheduler()
        self.prev_max_tvl = {'Gigavault': 60000000}  # словарь vault_name -> max_tvl

    # --- Крипто ---
    async def get_prices(self):
        ids = ",".join(COINS.values())
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd"
        try:
            async with self.session.get(url, timeout=30) as resp:
                data = await resp.json()
                return {symbol: data[cgid]["usd"] for symbol, cgid in COINS.items() if cgid in data}
        except Exception as e:
            logger.error(f"Ошибка при получении цен: {e}")
            return {}

    async def price_check(self):
        prices = await self.get_prices()
        for symbol, price in prices.items():
            if symbol == "ETH" and price < THRESHOLDS["ETH"]:
                await self.send_alert(symbol, price, f"упала ниже ${THRESHOLDS['ETH']}")
            elif symbol == "CRV" and price > THRESHOLDS["CRV"]:
                await self.send_alert(symbol, price, f"выросла выше ${THRESHOLDS['CRV']}")
            elif symbol == "AERO" and price > THRESHOLDS["AERO"]:
                await self.send_alert(symbol, price, f"выросла выше ${THRESHOLDS['AERO']}")

    async def send_daily_prices(self):
        prices = await self.get_prices()
        if prices:
            msg = "🌅 Утренний отчёт по ценам:\n"
            for symbol, price in prices.items():
                msg += f"- {symbol}: ${price:,.2f}\n"
            await self.send_message(msg)

    # --- Получение данных Gigavault ---
    async def get_gigavault_data(self):
        try:
            response = requests.get(VAULT_API_URL, headers={"Accept": "application/json"})
            response.raise_for_status()
            return response.json()

        except Exception as e:
            print("Ошибка при получении данных:", e)
            return None


    async def check_gigavault(self):
        vaults = await self.get_gigavault_data()
        # logger.info([x for x in vaults['results'] if x['name']=='Gigavault'])

        for vault in vaults['results']:
            # Проверяем название именно в объекте
            if vault.get('name') == "Gigavault":
                max_tvl = vault.get('max_tvl', 0)
                prev = self.prev_max_tvl.get("Gigavault", 0)

                if max_tvl > prev:
                    free_space = max_tvl - prev
                    msg = f"📢 Gigavault max TVL увеличен!\n" \
                        f"Было: {prev:,}\n" \
                        f"Стало: {max_tvl:,}\n" \
                        f"Доступное место появилось: {free_space:,}"
                    await self.send_message(msg)

                # Обновляем сохранённое значение
                self.prev_max_tvl["Gigavault"] = max_tvl

    # --- Telegram ---
    async def send_message(self, text: str):
        try:
            await self.app.bot.send_message(chat_id=self.chat_id, text=text)
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения: {e}")

    async def send_alert(self, symbol: str, price: float, condition: str):
        msg = f"🚨 {symbol} Price Alert! 🚨\nУсловие: {condition}\nТекущая цена: ${price:,.2f}"
        await self.send_message(msg)

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🤖 Бот для мониторинга криптовалют\n"
            f"Авто-проверка каждые {CHECK_INTERVAL} секунд.\n"
            "Команда /price для текущих цен."
        )

    async def cmd_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        prices = await self.get_prices()
        if prices:
            msg = "💰 Текущие цены:\n"
            for symbol, price in prices.items():
                msg += f"- {symbol}: ${price:,.2f}\n"
            await update.message.reply_text(msg)
        else:
            await update.message.reply_text("Не удалось получить цены")

    async def run_checks(self):
        while True:
            try:
                await self.price_check()
                await self.check_gigavault()
                await asyncio.sleep(CHECK_INTERVAL)
            except Exception as e:
                logger.error(f"Ошибка в цикле проверки: {e}")
                await asyncio.sleep(60)

    async def shutdown(self):
        self.scheduler.shutdown()
        if self.session:
            await self.session.close()
        await self.send_message("🛑 Бот остановлен")

# --- Async main ---
async def main():
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("CHAT_ID")

    async with aiohttp.ClientSession() as session:
        app = Application.builder().token(token).build()
        bot = CryptoBot(session, app, chat_id)

        # --- Регистрируем команды ---
        app.add_handler(CommandHandler("start", bot.cmd_start))
        app.add_handler(CommandHandler("price", bot.cmd_price))

        # --- Уведомление о запуске ---
        await bot.send_message("✅ Бот запущен")

        # --- Фоновая проверка цен и Gigavault ---
        asyncio.create_task(bot.run_checks())

        # --- Планировщик утреннего отчёта ---
        bot.scheduler.add_job(bot.send_daily_prices, "cron", hour=DAILY_HOUR, minute=DAILY_MINUTE)
        bot.scheduler.start()

        # --- Запуск polling ---
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        logger.info("Бот запущен")

        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            await bot.shutdown()
            await app.updater.stop()
            await app.stop()
            await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
