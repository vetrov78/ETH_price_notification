import os
import unittest
from unittest.mock import AsyncMock, patch

from eth_price_bot import CryptoBot, HYPERITHM_VAULT_URL


class FakeResponse:
    status = 200

    def __init__(self, data):
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def json(self, content_type=None):
        return self.data


class FakeSession:
    def __init__(self, data):
        self.data = data

    def get(self, url, timeout):
        return FakeResponse(self.data)


class HyperithmVaultTests(unittest.IsolatedAsyncioTestCase):
    async def test_vault_deposits_use_usdc_tvl_not_usd_valuation(self):
        data = {
            "loan": {"active": True},
            "loan_computed": {
                "can_deposit": True,
                "tvl": 74999597772193,
                "tvl_in_usd": 76277880.61267582,
            },
            "on_chain_loan": {
                "paused": False,
                "loan": {"loan": {"maxCapacity": 75000000000000}},
                "vault_asset": {"decimals": 6},
            },
        }
        with patch.dict(os.environ, {
            "BTC_CRITICAL_PRICE": "95000",
            "ETH_CRITICAL_PRICE": "3000",
            "AERO_CRITICAL_PRICE": "0.9",
        }):
            bot = CryptoBot(FakeSession(data), None, None)

        status, error = await bot.get_hyperithm_vault_status()

        self.assertIsNone(error)
        self.assertEqual(status["deposits_usdc"], 74999597.772193)
        self.assertEqual(status["capacity_usdc"], 75000000)
        self.assertAlmostEqual(status["available_usdc"], 402.227807)
        self.assertTrue(status["accepts_deposits"])

    async def test_vault_displays_amounts_as_whole_usdc_units(self):
        status = {
            "accepts_deposits": True,
            "available_usdc": 1402.227807,
            "deposits_usdc": 74998597.772193,
            "capacity_usdc": 75000000,
        }
        bot = CryptoBot(None, None, None)
        bot.get_hyperithm_vault_status = AsyncMock(return_value=(status, None))
        bot.send_message = AsyncMock()

        await bot.hyperithm_vault_check()

        alert = bot.send_message.await_args.args[0]
        self.assertIn("Свободная вместимость: 1,402 USDC", alert)
        self.assertIn("Депозиты / лимит: 74,998,598 / 75,000,000 USDC", alert)
        self.assertNotIn(HYPERITHM_VAULT_URL, alert)

        bot.get_prices = AsyncMock(return_value={})
        bot.get_usd_brl_rate = AsyncMock(return_value=(None, "unavailable"))
        bot.get_eth_gas_gwei = AsyncMock(return_value=(None, "unavailable"))
        update = type("Update", (), {
            "message": type("Message", (), {"reply_text": AsyncMock()})(),
        })()

        await bot.cmd_price(update, None)

        message = update.message.reply_text.await_args.args[0]
        self.assertIn("- HDNV(dep/lim): 74,998,598/75,000,000 USDC", message)

    async def test_vault_alert_requires_more_than_1000_usdc_available(self):
        bot = CryptoBot(None, None, None)
        bot.send_message = AsyncMock()
        bot.get_hyperithm_vault_status = AsyncMock()

        def vault_status(available_usdc):
            return {
                "accepts_deposits": True,
                "available_usdc": available_usdc,
                "deposits_usdc": 74999900,
                "capacity_usdc": 75000000,
            }, None

        bot.get_hyperithm_vault_status.side_effect = [
            vault_status(500),
            vault_status(1000),
            vault_status(1001),
            vault_status(1500),
            vault_status(900),
            vault_status(1200),
        ]

        for _ in range(6):
            await bot.hyperithm_vault_check()

        self.assertEqual(bot.send_message.await_count, 2)
        messages = [call.args[0] for call in bot.send_message.await_args_list]
        self.assertIn("Свободная вместимость: 1,001 USDC", messages[0])
        self.assertIn("Свободная вместимость: 1,200 USDC", messages[1])
        for message in messages:
            self.assertIn("превышает 1,000 USDC", message)
            self.assertNotIn(HYPERITHM_VAULT_URL, message)
