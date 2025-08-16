import os
import asyncio
import requests
from telegram import Bot
from dotenv import load_dotenv

# Загружаем конфиг
load_dotenv("config.env")

async def send_alert(bot, current_price):
    """Асинхронная отправка сообщения"""
    message = f"🚨 ETH Price Alert! 🚨\nЦена ETH упала ниже ${os.getenv('ETH_CRITICAL_PRICE')}!\nТекущая цена: ${current_price}"
    await bot.send_message(
        chat_id=os.getenv("CHAT_ID"),
        text=message
    )

async def main():
    bot = Bot(token=os.getenv("TELEGRAM_TOKEN"))
    print("Бот запущен. Мониторинг цены ETH...")
    
    while True:
        try:
            # Получаем цену (синхронный запрос)
            price = requests.get(
                "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd"
            ).json()["ethereum"]["usd"]
            
            print(f"Текущая цена ETH: ${price}")

            if price < float(os.getenv("ETH_CRITICAL_PRICE")):
                await send_alert(bot, price)
                
            await asyncio.sleep(60)  # Асинхронная задержка
            
        except Exception as e:
            print(f"Ошибка: {e}")
            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())