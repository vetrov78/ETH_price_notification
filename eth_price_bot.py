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
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "AERO": "aerodrome-finance",
    "CRV": "curve-dao-token"
}

THRESHOLDS = {
    "BTC": float(os.getenv("BTC_CRITICAL_PRICE", 55000)),   # ниже этой цены → тревога
    "ETH": float(os.getenv("ETH_CRITICAL_PRICE", 3000)),
    "CRV": float(os.getenv("CRV_CRITICAL_PRICE", 1.0)),
    "AERO": float(os.getenv("AERO_CRITICAL_PRICE", 1.35))
}

# --- Настройки бота ---
VAULT_API_URL = "https://api.prod.paradex.trade/v1/vaults"

# Несколько публичных RPC для фолбэка
ETH_RPC_URLS = os.getenv(
    "ETH_RPC_URLS",
    "https://ethereum.publicnode.com,https://cloudflare-eth.com,https://rpc.ankr.com/eth"
).split(",")


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
            if symbol == "BTC" and price < THRESHOLDS["BTC"]:
                await self.send_alert(symbol, price, f"упал ниже ${THRESHOLDS['BTC']}")
            elif symbol == "ETH" and price < THRESHOLDS["ETH"]:
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

    # --- Получение информации о газе
    async def get_eth_gas_gwei(self):
        """Возвращает (gwei, None) или (None, error). Пробует несколько RPC по очереди."""
        payload = {"jsonrpc": "2.0", "method": "eth_gasPrice", "params": [], "id": 1}
        headers = {"Content-Type": "application/json"}
        errors = []

        for raw_url in ETH_RPC_URLS:
            url = raw_url.strip()
            if not url:
                continue
            try:
                async with self.session.post(url, json=payload, headers=headers, timeout=30) as resp:
                    if resp.status != 200:
                        errors.append(f"{url} status {resp.status}")
                        continue
                    j = await resp.json(content_type=None)  # на случай неверного content-type
                    wei_hex = j.get("result")
                    if not wei_hex or not isinstance(wei_hex, str) or not wei_hex.startswith("0x"):
                        errors.append(f"{url} no valid result: {j!r}")
                        continue
                    wei = int(wei_hex, 16)
                    gwei = wei / 1e9
                    return gwei, None
            except Exception as e:
                errors.append(f"{url} exception: {e}")

        # если сюда дошли — ни один RPC не сработал
        return None, " ; ".join(errors) or "No result from any RPC"

    async def gas_check(self):
        """Проверяет цену газа и присылает уведомление при низком уровне."""
        gas_gwei, gerr = await self.get_eth_gas_gwei()
        if gas_gwei is None:
            logger.error(f"Ошибка получения газа: {gerr}")
            return

        critical = float(os.getenv("GAS_CRITICAL_GWEI", 0.2))
        if gas_gwei < critical:
            msg = (
                f"⛽️ Газ в сети Ethereum опустился ниже порога!\n"
                f"Текущая цена: {gas_gwei:.2f} gwei\n"
                f"Пороговое значение: {critical:.2f} gwei"
            )
            await self.send_message(msg)

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
            msg_lines = ["💰 Текущие цены:"]
            # выводим в фиксированном порядке
            for symbol in ["BTC", "ETH", "CRV", "AERO"]:
                if symbol in prices:
                    msg_lines.append(f"- {symbol}: ${prices[symbol]:,.2f}")

            # цена газа
            # внутри cmd_price, после вывода монет
            gas_gwei, gerr = await self.get_eth_gas_gwei()
            if gas_gwei is not None:
                msg_lines.append(f"- GAS: {gas_gwei:.2f} gwei")
            else:
                msg_lines.append(f"- GAS: ошибка ({gerr})")

            if gas_gwei is not None:
                msg_lines.append(f"- GAS: {gas_gwei:.2f} gwei")
            else:
                msg_lines.append(f"- GAS: ошибка ({gerr})")

            await update.message.reply_text("\n".join(msg_lines))
        else:
            await update.message.reply_text("Не удалось получить цены")

    async def run_checks(self):
        while True:
            try:
                await self.price_check()
                await self.check_gigavault()
                await self.gas_check()
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
