# ETH Price Alert Bot

Telegram-бот для мониторинга цены Ethereum.

## Установка
1. Установи зависимости:
```bash
pip install -r requirements.txt
```

## Курс бразильского реала

Курс берётся из публичного API Bybit Spot: последняя цена сделки (`lastPrice`)
пары `USDTBRL`, то есть количество BRL за 1 USDT. API-ключ не нужен.
Это биржевой курс USDT/BRL, а не официальный валютный курс USD/BRL.

Для совместимости настройка порога остаётся `USD_BRL_CRITICAL_RATE`,
а команда изменения — `/set USD_BRL 5.20`.
