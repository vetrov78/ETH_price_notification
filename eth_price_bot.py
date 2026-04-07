import os
import asyncio
import aiohttp
import logging
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

}

THRESHOLDS = {
    "BTC": float(os.getenv("BTC_CRITICAL_PRICE", 99000)),   # ниже этой цены → тревога
    "ETH": float(os.getenv("ETH_CRITICAL_PRICE", 3300)),
    "AERO": float(os.getenv("AERO_CRITICAL_PRICE", 0.2))
}

SUSN_METRICS_URL = "https://back.noon.capital/api/v1/protocol-metrics"
MORPHO_API_URL = "https://api.morpho.org/graphql"
MORPHO_SUSN_USDC_MARKET_ID = "0x8924445a76b678c536df977ed9222fb0b23ee5311497dd0223fe6270bb20b4e6"

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

# --- Gigavault ---
GIGAVAULT_START_MAX_TVL_RAW = os.getenv("GIGAVAULT_START_MAX_TVL", "90000000")
try:
    GIGAVAULT_START_MAX_TVL = float(GIGAVAULT_START_MAX_TVL_RAW.replace(",", "."))
except ValueError:
    logger.error(
        f"Некорректное значение GIGAVAULT_START_MAX_TVL='{GIGAVAULT_START_MAX_TVL_RAW}', использую 90000000"
    )
    GIGAVAULT_START_MAX_TVL = 90000000.0

def update_env_value(env_path: str, key: str, value: str):
        lines = []
        found = False

        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith(f"{key}="):
                        lines.append(f"{key}={value}\n")
                        found = True
                    else:
                        lines.append(line)

        if not found:
            lines.append(f"{key}={value}\n")

        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(lines)

# --- Логика бота ---
class CryptoBot:
    def __init__(self, session, app, chat_id):
        self.session = session
        self.app = app
        self.chat_id = chat_id
        self.scheduler = AsyncIOScheduler()
        self.prev_max_tvl = {'Gigavault': GIGAVAULT_START_MAX_TVL}
        self.gas_below_threshold = None

        # локальные пороги, которые можно менять во время работы
        self.thresholds = {
            "BTC": THRESHOLDS["BTC"],
            "ETH": THRESHOLDS["ETH"],
            "AERO": THRESHOLDS["AERO"],
        }

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
            if symbol == "BTC" and price < self.thresholds["BTC"]:
                await self.send_alert(symbol, price, f"упал ниже ${self.thresholds['BTC']}")
            elif symbol == "ETH" and price < self.thresholds["ETH"]:
                await self.send_alert(symbol, price, f"упала ниже ${self.thresholds['ETH']}")
            elif symbol == "AERO" and price > self.thresholds["AERO"]:
                await self.send_alert(symbol, price, f"выросла выше ${self.thresholds['AERO']}")

    async def send_daily_prices(self):
        prices = await self.get_prices()
        msg = "🌅 Утренний отчёт по ценам:\n"

        if prices:
            for symbol in ["BTC", "ETH", "AERO"]:
                if symbol in prices:
                    msg += f"- {symbol}: ${prices[symbol]:,.2f}\n"
        else:
            msg += "— Не удалось получить цены монет\n"

        # --- GAS ---
        gas_gwei, gerr = await self.get_eth_gas_gwei()
        if gas_gwei is not None:
            msg += f"- GAS: {gas_gwei:.2f} gwei\n"
        else:
            msg += f"- GAS: ошибка ({gerr})\n"

        # --- sUSN ---
        susn_metrics, serr = await self.get_susn_metrics()
        if susn_metrics and susn_metrics.get("apy_7d") is not None:
            msg += f"- sUSN 7d APY: {susn_metrics['apy_7d']:.2f}%\n"
        else:
            msg += f"- sUSN 7d APY: error ({serr})\n"

        # --- Morpho sUSN/USDC borrow rate
        borrow_apy, merr = await self.get_morpho_susn_usdc_borrow_apy()
        if borrow_apy is not None:
            msg += f"- Morpho sUSN/USDC borrow APY: {borrow_apy * 100:.2f}%\n"
        else:
            msg += f"- Morpho sUSN/USDC borrow APY: error ({merr})\n"

        await self.send_message(msg)

    async def get_susn_metrics(self):
        try:
            async with self.session.get(
                SUSN_METRICS_URL,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0"
                },
                timeout=30
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    return None, f"Noon metrics status {resp.status}: {body[:200]}"

                data = await resp.json(content_type=None)

                apy_7d = None

                # 1) сначала пробуем текущее поле apy
                if isinstance(data.get("apy"), (str, int, float)):
                    try:
                        apy_7d = float(data["apy"])
                    except (TypeError, ValueError):
                        pass

                # 2) fallback: берём последнее значение из apyTimeSeries
                if apy_7d is None and isinstance(data.get("apyTimeSeries"), dict):
                    ts = data["apyTimeSeries"]
                    if ts:
                        last_date = sorted(ts.keys())[-1]
                        try:
                            apy_7d = float(ts[last_date])
                        except (TypeError, ValueError):
                            pass

                if apy_7d is None:
                    return None, f"Unexpected Noon payload: {data!r}"

                return {
                    "apy_7d": apy_7d
                }, None

        except Exception as e:
            return None, f"Noon metrics exception: {e}"
    
    async def get_morpho_susn_usdc_borrow_apy(self):
        query = """
        query MarketRate($uniqueKey: String!, $chainId: Int!) {
          marketByUniqueKey(uniqueKey: $uniqueKey, chainId: $chainId) {
            state {
              borrowApy
            }
          }
        }
        """

        payload = {
            "query": query,
            "variables": {
                "uniqueKey": MORPHO_SUSN_USDC_MARKET_ID,
                "chainId": 1
            }
        }

        try:
            async with self.session.post(
                MORPHO_API_URL,
                json=payload,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0"
                },
                timeout=30
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    return None, f"Morpho API status {resp.status}: {body[:200]}"

                data = await resp.json(content_type=None)
                market = data.get("data", {}).get("marketByUniqueKey")

                if not market:
                    return None, f"Unexpected Morpho payload: {data!r}"

                borrow_apy = market.get("state", {}).get("borrowApy")
                if borrow_apy is None:
                    return None, f"borrowApy not found: {data!r}"

                return float(borrow_apy), None

        except Exception as e:
            return None, f"Morpho API exception: {e}"
    
    # --- Получение данных Gigavault ---
    async def get_gigavault_data(self):
        try:
            async with self.session.get(
                VAULT_API_URL,
                headers={"Accept": "application/json"},
                timeout=30
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"Gigavault API status={resp.status}, body={body[:200]}")
                    return None

                return await resp.json(content_type=None)

        except Exception as e:
            logger.error(f"Ошибка при получении данных Gigavault: {e}")
            return None

    async def check_gigavault(self):
        vaults = await self.get_gigavault_data()

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
        """Уведомления по газу:
        - при первом запуске: если газ < порога — сразу шлём предупреждение;
        - дальше: алерт только при пересечении порога вниз/вверх.
        """
        gas_gwei, gerr = await self.get_eth_gas_gwei()
        if gas_gwei is None:
            logger.error(f"Ошибка получения газа: {gerr}")
            return

        raw_critical = os.getenv("GAS_CRITICAL_GWEI", "7.0")
        try:
            critical = float(raw_critical.replace(",", "."))
        except ValueError:
            logger.error(f"Некорректное значение GAS_CRITICAL_GWEI='{raw_critical}', использую 7.0")
            critical = 7.0

        logger.info(f"gas_check: gas={gas_gwei:.4f} gwei, critical={critical:.4f}, "
                    f"state={self.gas_below_threshold}")

        # 1) Первичная инициализация
        if self.gas_below_threshold is None:
            self.gas_below_threshold = gas_gwei < critical

            # Если уже ниже порога — сразу предупреждаем один раз
            if self.gas_below_threshold:
                await self.send_message(
                    "⛽️ Газ в сети Ethereum уже ниже порога!\n"
                    f"Текущая цена: {gas_gwei:.2f} gwei\n"
                    f"Пороговое значение: {critical:.2f} gwei"
                )
            return

        # 2) Пересечение вниз (было >=, стало <)
        if gas_gwei < critical and self.gas_below_threshold is False:
            self.gas_below_threshold = True
            await self.send_message(
                "⛽️ Газ в сети Ethereum опустился ниже порога!\n"
                f"Текущая цена: {gas_gwei:.2f} gwei\n"
                f"Пороговое значение: {critical:.2f} gwei"
            )
            return

        # 3) Пересечение вверх (было <, стало >=)
        if gas_gwei >= critical and self.gas_below_threshold is True:
            self.gas_below_threshold = False
            await self.send_message(
                "✅ Газ в сети Ethereum поднялся выше порога.\n"
                f"Текущая цена: {gas_gwei:.2f} gwei\n"
                f"Пороговое значение: {critical:.2f} gwei"
            )
            return

        # 4) Состояние не изменилось — молчим

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
        text = (
            "🤖 Бот для мониторинга криптовалют\n"
            f"Авто-проверка каждые {CHECK_INTERVAL} секунд.\n"
            "Команда /price для текущих цен."
        )

        if update.message:
            await update.message.reply_text(text)
        elif update.effective_chat:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=text)

    async def cmd_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        prices = await self.get_prices()
        if prices:
            msg_lines = ["💰 Текущие цены:"]

            for symbol in ["BTC", "ETH", "AERO"]:
                if symbol in prices:
                    msg_lines.append(f"- {symbol}: ${prices[symbol]:,.2f}")

            gas_gwei, gerr = await self.get_eth_gas_gwei()
            if gas_gwei is not None:
                msg_lines.append(f"- GAS: {gas_gwei:.2f} gwei")
            else:
                msg_lines.append(f"- GAS: ошибка ({gerr})")
            
            # get sUSN 7d APR
            susn_metrics, serr = await self.get_susn_metrics()
            if susn_metrics and susn_metrics.get("apy_7d") is not None:
                msg_lines.append(f"- sUSN 7d APY: {susn_metrics['apy_7d']:.2f}%")
            else:
                msg_lines.append(f"- sUSN 7d APY: error ({serr})")

            # get Morpho sUSN/USDC borrow rate
            borrow_apy, merr = await self.get_morpho_susn_usdc_borrow_apy()
            if borrow_apy is not None:
                msg_lines.append(f"- Morpho sUSN/USDC borrow APY: {borrow_apy * 100:.2f}%")
            else:
                msg_lines.append(f"- Morpho sUSN/USDC borrow APY: error ({merr})")

            await update.message.reply_text("\n".join(msg_lines))
        else:
            await update.message.reply_text("Не удалось получить цены")

    async def cmd_set(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда: /set <COIN> <VALUE>
        Примеры:
        /set BTC 95000
        /set CRV 0.9
        """
        if len(context.args) != 2:
            await update.message.reply_text(
                "Использование: /set <COIN> <VALUE>\n"
                "Например: /set BTC 95000"
            )
            return

        symbol = context.args[0].upper()
        value_str = context.args[1].replace(",", ".")  # на всякий случай, если введёшь с запятой

        if symbol not in self.thresholds:
            await update.message.reply_text(
                f"Неизвестная монета: {symbol}\n"
                f"Доступные: {', '.join(self.thresholds.keys())}"
            )
            return

        try:
            new_value = float(value_str)
        except ValueError:
            await update.message.reply_text("Не получилось прочитать число. Пример: /set BTC 95000")
            return

        old_value = self.thresholds[symbol]
        self.thresholds[symbol] = new_value
        env_key = f"{symbol}_CRITICAL_PRICE"
        update_env_value("config.env", env_key, value_str)
        os.environ[env_key] = value_str  # опционально, чтобы getenv тоже видел новое значение в этом процессе

        await update.message.reply_text(
            f"✅ Порог для {symbol} обновлён:\n"
            f"было: {old_value}\n"
            f"стало: {new_value}"
        )

    async def cmd_thresholds(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        lines = ["Текущие пороги:"]
        for symbol, value in self.thresholds.items():
            lines.append(f"- {symbol}: {value}")
        await update.message.reply_text("\n".join(lines))

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
        app.add_handler(CommandHandler("set", bot.cmd_set))
        app.add_handler(CommandHandler("thresholds", bot.cmd_thresholds))


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
