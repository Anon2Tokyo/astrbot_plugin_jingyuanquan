import asyncio
import importlib
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import httpx

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("dsp_test")
package.__path__ = [str(ROOT)]
sys.modules["dsp_test"] = package
api = importlib.import_module("dsp_test.payment_api")
store_module = importlib.import_module("dsp_test.payment_store")
service_module = importlib.import_module("dsp_test.payment_service")


def remote(oid="up-1", status="SUCCESS", amount="20", currency="CNY"):
    return {
        "payment_order_id": oid,
        "amount": amount,
        "currency": currency,
        "payment_order_status": status,
        "paid_at": "2026-10-23T10:22:38+00:00" if status == "SUCCESS" else None,
    }


class StoreTests(unittest.TestCase):
    def test_credit_is_atomic_idempotent_and_separate_from_manual_income(self):
        row = self.order()
        self.assertTrue(self.book.apply(row["id"], remote(), 10))
        self.assertFalse(self.book.apply(row["id"], remote(), 20))
        self.assertEqual(self.book.ledger.wallet("123")["balance"], 20000)
        self.assertEqual(len(self.book.ledger.history("123")), 1)
        self.assertEqual(self.book.ledger.stats()[0], 0)
        self.assertEqual(self.book.total(), 20)
        self.book.ledger.recharge("123", "1", "admin", "manual")
        self.assertEqual(self.book.ledger.stats()[0], 1)
        self.assertEqual(self.book.total(), 20)

    def test_journal_failure_rolls_back_payment_and_wallet(self):
        row = self.order()
        with self.book.db() as db:
            db.execute(
                "CREATE TRIGGER fail_credit BEFORE INSERT ON journal BEGIN SELECT RAISE(ABORT, 'test failure'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.book.apply(row["id"], remote(), 10)
        self.assertEqual(self.book.own("account", "123", row["id"])["state"], "PENDING")
        self.assertEqual(self.book.ledger.wallet("123")["balance"], 0)
        with self.book.db() as db:
            db.execute("DROP TRIGGER fail_credit")
        reopened = store_module.Store(self.book.path)
        self.assertTrue(reopened.apply(row["id"], remote(), 20))
        self.assertEqual(reopened.ledger.wallet("123")["balance"], 20000)

    def test_concurrent_confirmation_only_credits_once(self):
        from concurrent.futures import ThreadPoolExecutor

        row = self.order()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(lambda _: self.book.apply(row["id"], remote(), 10), range(4))
            )
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.book.ledger.wallet("123")["balance"], 20000)

    def test_bad_payment_never_credits_wallet(self):
        row = self.order()
        self.assertFalse(self.book.apply(row["id"], remote(amount="21"), 10))
        self.assertEqual(self.book.ledger.wallet("123")["balance"], 0)
        self.assertEqual(self.book.total(), 0)

    def test_rate_is_locked_at_creation(self):
        from unittest.mock import patch

        row = self.order()
        with patch.object(store_module, "EXCHANGE_RATE", 2000):
            self.book.apply(row["id"], remote(), 10)
        self.assertEqual(self.book.ledger.wallet("123")["balance"], 20000)
        self.assertEqual(self.book.ledger.history("123")[0]["exchange_rate"], 1000)

    def test_existing_orders_migrate_without_changing_identity(self):
        row = self.order()
        with self.book.db() as db:
            db.execute("ALTER TABLE orders DROP COLUMN channel")
            db.execute("ALTER TABLE orders DROP COLUMN actual_channel")
        reopened = store_module.Store(self.book.path)
        restored = reopened.own("account", "123", row["id"])
        self.assertEqual(restored["upstream_id"], row["upstream_id"])
        self.assertEqual(restored["amount"], row["amount"])
        self.assertEqual(restored["channel"], "ALIPAY")
        self.assertIsNone(restored["actual_channel"])

    def test_chosen_and_actual_channel_are_separate(self):
        row, _ = self.book.create(
            "account", "123", "group", "channel-test", "20", 0, "WECHAT"
        )
        self.book.bind(row["id"], "up-1", "https://qr.95516.com/path")
        self.book.apply(row["id"], dict(remote(), payment_channel="ALIPAY"), 10)
        result = self.book.own("account", "123", row["id"])
        self.assertEqual(result["channel"], "WECHAT")
        self.assertEqual(result["actual_channel"], "ALIPAY")
        self.assertEqual(result["state"], "PAID")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.book = store_module.Store(Path(self.temp.name) / "orders.db")

    def order(self, qq="123", source="s", now=0, upstream="up-1"):
        row, _ = self.book.create("account", qq, "group", source, "20", now)
        self.book.bind(row["id"], upstream, "https://qr.95516.com/path")
        return self.book.own("account", qq, row["id"])

    def test_same_message_and_active_user_do_not_create_twice(self):
        row = self.order()
        for source in ("s", "new-message"):
            existing, fresh = self.book.create(
                "account", "123", "group", source, "20", 10
            )
            self.assertFalse(fresh)
            self.assertEqual(existing["id"], row["id"])

    def test_two_users_have_separate_orders_and_private_history(self):
        a = self.order()
        b = self.order("456", "b", upstream="up-2")
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(len(self.book.history("account", "123")), 1)
        for account, qq in (("account", "456"), ("other", "123")):
            with self.assertRaises(ValueError):
                self.book.own(account, qq, a["id"])

    def test_upstream_order_unique(self):
        self.order()
        with self.assertRaises(sqlite3.IntegrityError):
            self.order("456", "b", upstream="up-1")

    def test_success_idempotent_and_does_not_regress(self):
        row = self.order()
        self.assertTrue(self.book.apply(row["id"], remote(), 10))
        self.assertFalse(self.book.apply(row["id"], remote(), 20))
        self.book.apply(row["id"], remote(status="FAILED"), 30)
        self.assertEqual(self.book.own("account", "123", row["id"])["state"], "PAID")
        self.assertEqual(len(self.book.notifications("account", 30)), 1)
        self.book.notified(row["id"])
        self.assertEqual(self.book.notifications("account", 30), [])

    def test_created_after_expiry_is_not_paid(self):
        row = self.order()
        self.book.apply(row["id"], remote(status="CREATED"), 900)
        self.assertEqual(self.book.own("account", "123", row["id"])["state"], "EXPIRED")
        self.assertEqual(self.book.notifications("account", 900), [])

    def test_schedule_stops_after_final_check(self):
        row = self.order()
        for at, expected in (
            (10, 20),
            (899, 900),
            (900, 960),
            (960, 1200),
            (1200, 1800),
        ):
            self.book.reschedule(row["id"], at)
            self.assertEqual(
                self.book.own("account", "123", row["id"])["next_check"], expected
            )
        self.book.reschedule(row["id"], 1800)
        self.assertEqual(self.book.due("account", 99999), [])

    def test_late_success_can_be_manually_confirmed(self):
        row = self.order()
        self.book.due("account", 1800)
        self.book.reschedule(row["id"], 1800)
        self.assertTrue(self.book.apply(row["id"], remote(), 2000))

    def test_failed_no_notification_but_bounded_rechecks(self):
        row = self.order()
        self.book.apply(row["id"], remote(status="FAILED"), 10)
        self.assertEqual(self.book.own("account", "123", row["id"])["state"], "FAILED")
        self.assertEqual(self.book.notifications("account", 10), [])
        self.book.reschedule(row["id"], 1800)
        self.assertEqual(self.book.due("account", 1801), [])

    def test_invalid_success_not_accepted(self):
        variants = [
            remote(amount="19"),
            remote(currency="USD"),
            remote(oid="other"),
            remote(status="UNKNOWN"),
            dict(remote(), paid_at=None),
        ]
        for i, payload in enumerate(variants):
            row = self.order(str(i), str(i), upstream=f"up-{i}")
            if payload["payment_order_id"] == "up-1":
                payload["payment_order_id"] = f"up-{i}"
            self.book.apply(row["id"], payload, 10)
            self.assertEqual(
                self.book.own("account", str(i), row["id"])["state"], "REVIEW_REQUIRED"
            )

    def test_missing_order_never_implies_failure(self):
        row = self.order()
        self.book.apply(row["id"], None, 10)
        self.assertEqual(self.book.own("account", "123", row["id"])["state"], "PENDING")

    def test_recover_creation_does_not_repeat_post(self):
        row, _ = self.book.create("account", "123", "group", "s", "20", 0)
        self.book.recover()
        self.assertEqual(
            self.book.own("account", "123", row["id"])["state"], "REVIEW_REQUIRED"
        )

    def test_notification_retries_are_bounded(self):
        row = self.order()
        self.book.apply(row["id"], remote(), 0)
        for at in (0, 60, 120):
            self.assertEqual(len(self.book.notifications("account", at)), 1)
            self.book.notification_attempt(row["id"], at)
        self.assertEqual(self.book.notifications("account", 1000), [])


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.book = store_module.Store(Path(self.temp.name) / "orders.db")
        self.api = types.SimpleNamespace(
            create=AsyncMock(return_value=("up-1", "https://qr.95516.com/path")),
            orders=AsyncMock(return_value={"up-1": remote()}),
        )
        self.now = 0
        self.service = service_module.Service(
            self.book, self.api, "account", lambda: self.now
        )

    async def test_no_pending_no_network(self):
        await self.service.tick()
        self.api.orders.assert_not_awaited()

    async def test_duplicate_message_posts_once(self):
        await asyncio.gather(
            *(self.service.create("123", "g", "s", "20") for _ in range(4))
        )
        self.api.create.assert_awaited_once()

    async def test_timeout_create_keeps_review_and_never_retries(self):
        self.api.create.side_effect = api.PaymentError("timeout")
        with self.assertRaises(ValueError):
            await self.service.create("123", "g", "s", "20")
        row, fresh = await self.service.create("123", "g", "s", "20")
        self.assertEqual(row["state"], "REVIEW_REQUIRED")
        self.assertFalse(fresh)
        self.api.create.assert_awaited_once()

    async def test_many_orders_share_one_fetch_and_ignore_unowned(self):
        await self.service.create("123", "g", "s", "20")
        self.api.create.return_value = ("up-2", "https://qr.95516.com/path2")
        await self.service.create("456", "g", "s2", "20")
        self.api.orders.return_value["history-with-no-qq"] = remote(
            "history-with-no-qq"
        )
        self.now = 10
        await self.service.tick()
        self.api.orders.assert_awaited_once()
        self.assertEqual(len(self.book.notifications("account", 10)), 1)

    async def test_other_users_cannot_trigger_lookup(self):
        row, _ = await self.service.create("123", "g", "s", "20")
        with self.assertRaises(ValueError):
            await self.service.check("456", row["id"])
        self.api.orders.assert_not_awaited()

    async def test_auth_failure_pauses_further_requests(self):
        await self.service.create("123", "g", "s", "20")
        self.api.orders.side_effect = api.AuthenticationError("expired")
        self.now = 10
        with self.assertRaises(api.AuthenticationError):
            await self.service.tick()
        self.now = 100
        await self.service.tick()
        self.api.orders.assert_awaited_once()

    async def test_failure_backoff_and_deadline(self):
        row, _ = await self.service.create("123", "g", "s", "20")
        self.api.orders.side_effect = api.PaymentError("down")
        self.now = 10
        with self.assertRaises(api.PaymentError):
            await self.service.tick()
        self.now = 11
        await self.service.tick()
        self.api.orders.assert_awaited_once()
        self.now = 1800
        with self.assertRaises(api.PaymentError):
            await self.service.tick()
        self.assertEqual(self.book.own("account", "123", row["id"])["polling_done"], 1)


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_device_value_is_not_sent(self):
        def handler(request):
            body = json.loads(request.content)
            self.assertEqual(set(body), {"order_info"})
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "biz_code": 0,
                        "biz_data": {
                            "code": 0,
                            "payment_order_id": "minimal",
                            "amount": "1",
                            "url": "https://qr.95516.com/path",
                        },
                    },
                },
            )

        adapter = await self.adapter(handler)
        await adapter.create("1", "minimal-request")

    async def test_wechat_payload_uses_observed_fallback(self):
        def handler(request):
            info = json.loads(request.content)["order_info"]
            self.assertEqual(info["payment_method_type"], "CMB_UNIONPAY")
            self.assertEqual(info["fallback_method_type"], "WECHAT")
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "biz_code": 0,
                        "biz_data": {
                            "code": 0,
                            "payment_order_id": "wx-order",
                            "amount": "20",
                            "url": "https://qr.95516.com/path",
                        },
                    },
                },
            )

        adapter = await self.adapter(handler)
        self.assertEqual(
            (await adapter.create("20", "wx-request", "WECHAT"))[0], "wx-order"
        )

    async def adapter(self, handler):
        client = httpx.AsyncClient(
            base_url="https://platform.deepseek.com",
            transport=httpx.MockTransport(handler),
        )
        adapter = api.DeepSeekPayments("placeholder", client=client)
        self.addAsyncCleanup(adapter.close)
        return adapter

    async def test_create_payload(self):
        def handler(request):
            body = json.loads(request.content)
            self.assertEqual(body["order_info"]["request_id"], "request-id")
            self.assertEqual(body["order_info"]["amount"], "20.00")
            self.assertEqual(body["order_info"]["fallback_method_type"], "ALIPAY")
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "biz_code": 0,
                        "biz_data": {
                            "code": 0,
                            "payment_order_id": "up-1",
                            "amount": "20",
                            "url": "https://qr.95516.com/path",
                        },
                    },
                },
            )

        adapter = await self.adapter(handler)
        self.assertEqual(
            await adapter.create("20", "request-id"),
            ("up-1", "https://qr.95516.com/path"),
        )

    async def test_query_ignores_bonus_orders(self):
        adapter = await self.adapter(
            lambda request: httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "biz_code": 0,
                        "biz_data": {
                            "invoices": {
                                "payment_orders": [remote()],
                                "bonus_orders": [{"id": "gift"}],
                            }
                        },
                    },
                },
            )
        )
        self.assertEqual(list(await adapter.orders()), ["up-1"])

    async def test_business_error_is_not_success(self):
        adapter = await self.adapter(
            lambda request: httpx.Response(
                200, json={"code": 0, "data": {"biz_code": 1, "biz_msg": "secret"}}
            )
        )
        with self.assertRaises(api.PaymentError) as raised:
            await adapter.orders()
        self.assertNotIn("secret", str(raised.exception))

    async def test_http_auth_error(self):
        adapter = await self.adapter(lambda request: httpx.Response(401))
        with self.assertRaises(api.AuthenticationError):
            await adapter.orders()

    def test_amount_validation(self):
        for value in (
            "NaN",
            "Infinity",
            "-1",
            "0",
            "0.001",
            "0.99",
            "1.001",
            "1.5",
            "100000.01",
            "100001",
        ):
            with self.assertRaises(ValueError):
                api.money(value)
        self.assertEqual(api.money("20"), "20.00")
        self.assertEqual(api.money("1"), "1.00")
        self.assertEqual(api.money("6"), "6.00")
        self.assertEqual(api.money("100000"), "100000.00")
        self.assertEqual(api.money("10001"), "10001.00")


if __name__ == "__main__":
    unittest.main()
