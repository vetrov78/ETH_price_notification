import os
import unittest
from unittest.mock import AsyncMock, patch

from eth_price_bot import CryptoBot


class PriceThresholdTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        env = patch.dict(os.environ, {
            "BTC_CRITICAL_PRICE": "95000",
            "ETH_CRITICAL_PRICE": "3000.50",
            "AERO_CRITICAL_PRICE": "0,9",
            "USD_BRL_CRITICAL_RATE": "5,20",
        })
        env.start()
        self.addCleanup(env.stop)
        self.bot = CryptoBot(None, None, None)
        self.bot.get_prices = AsyncMock()
        self.bot.send_alert = AsyncMock()

    def test_thresholds_are_loaded_as_numbers(self):
        self.assertEqual(self.bot.thresholds, {
            "BTC": 95000.0,
            "ETH": 3000.5,
            "AERO": 0.9,
            "USD_BRL": 5.2,
        })

    async def test_prices_crossing_thresholds_trigger_alerts(self):
        self.bot.get_prices.return_value = {
            "BTC": 94000, "ETH": 2900.5, "AERO": 1,
        }

        await self.bot.price_check()

        self.assertEqual(self.bot.send_alert.await_count, 3)
        self.bot.send_alert.assert_any_await("BTC", 94000, "упал ниже $95000.0")
        self.bot.send_alert.assert_any_await("ETH", 2900.5, "упала ниже $3000.5")
        self.bot.send_alert.assert_any_await("AERO", 1, "выросла выше $0.9")

    async def test_prices_at_or_on_safe_side_of_thresholds_do_not_alert(self):
        for prices in (
            {"BTC": 95000, "ETH": 3000.5, "AERO": 0.9},
            {"BTC": 96000, "ETH": 3100, "AERO": 0.8},
        ):
            with self.subTest(prices=prices):
                self.bot.get_prices.return_value = prices
                await self.bot.price_check()
                self.bot.send_alert.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
