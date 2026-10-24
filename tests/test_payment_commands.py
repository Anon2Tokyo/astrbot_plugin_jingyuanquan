import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_payments import ROOT, remote


class Event:
    message_str = "鲸元券支付宝充值 20元"
    unified_msg_origin = "bot:GroupMessage:100"
    message_obj = types.SimpleNamespace(message_id="1")

    def __init__(self, qq="123", group="100"):
        self.qq, self.group, self.stopped = qq, group, False

    def stop_event(self):
        self.stopped = True

    def get_platform_name(self):
        return "aiocqhttp"

    def get_group_id(self):
        return self.group

    def get_self_id(self):
        return "999"

    def get_sender_id(self):
        return self.qq

    def plain_result(self, text):
        return text

    def chain_result(self, chain):
        return chain


class At:
    def __init__(self, qq):
        self.qq = qq


class Plain:
    def __init__(self, text):
        self.text = text


class Image:
    @staticmethod
    def fromBytes(data):
        return data


class Star:
    def __init__(self, context):
        self.context = context


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_amount_ignores_legacy_tiers(self):
        self.plugin.config["amounts"] = ["1", "20"]
        result = await self.collect(self.plugin.payment_wechat_recharge(Event(), "6元"))
        self.assertIn("6.00 元", result[0][1].text)
        self.assertEqual(self.plugin.payment_api.create.call_args.args[0], "6.00")

    async def test_below_one_yuan_does_not_create_order(self):
        result = await self.collect(
            self.plugin.payment_alipay_recharge(Event(), "0.99元")
        )
        self.assertIn("1 元至 100000 元之间的整数金额", result[0])
        self.plugin.payment_api.create.assert_not_awaited()
        self.assertEqual(self.plugin.payment_store.history("default", "123"), [])

    async def test_wechat_entry_records_channel_and_mentions_wechat(self):
        result = await self.collect(
            self.plugin.payment_wechat_recharge(Event(), "20元")
        )
        self.assertIn("请使用微信扫码", result[0][1].text)
        self.assertEqual(result[0][0].qq, "123")
        row = self.plugin.payment_store.history("default", "123")[0]
        self.assertEqual(row["channel"], "WECHAT")
        self.assertEqual(self.plugin.payment_api.create.call_args.args[-1], "WECHAT")

    async def test_choose_payment_does_not_create(self):
        result = await self.collect(self.plugin.payment_choose_payment(Event()))
        self.assertIn("/鲸元券微信充值", result[0])
        self.assertIn("/鲸元券支付宝充值", result[0])
        self.plugin.payment_api.create.assert_not_awaited()

    async def test_changing_channel_does_not_relabel_old_qr(self):
        await self.collect(self.plugin.payment_alipay_recharge(Event(), "20"))
        result = await self.collect(self.plugin.payment_wechat_recharge(Event(), "20"))
        self.assertIn("另一支付方式", result[0])
        self.plugin.payment_api.create.assert_awaited_once()
        self.assertEqual(
            self.plugin.payment_store.history("default", "123")[0]["channel"], "ALIPAY"
        )

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        modules = {
            n: types.ModuleType(n)
            for n in (
                "astrbot",
                "astrbot.api",
                "astrbot.api.event",
                "astrbot.api.star",
                "astrbot.api.message_components",
            )
        }
        modules["astrbot.api"].AstrBotConfig = dict
        modules["astrbot.api"].logger = types.SimpleNamespace(
            warning=lambda *args: None
        )
        modules["astrbot.api.event"].AstrMessageEvent = Event
        modules["astrbot.api.event"].MessageChain = list
        modules["astrbot.api.event"].filter = types.SimpleNamespace(
            command=lambda *a, **kw: lambda f: f,
            on_llm_request=lambda **kw: lambda f: f,
            on_llm_response=lambda **kw: lambda f: f,
        )
        modules["astrbot.api.star"].Star = Star
        modules["astrbot.api.star"].Context = object
        modules["astrbot.api.star"].StarTools = types.SimpleNamespace(
            get_data_dir=lambda name: Path(self.temp.name)
        )
        for name, obj in (("At", At), ("Plain", Plain), ("Image", Image)):
            setattr(modules["astrbot.api.message_components"], name, obj)
        stub = patch.dict(sys.modules, modules)
        stub.start()
        self.addCleanup(stub.stop)
        sys.modules.pop("dsp_test.main", None)
        main = importlib.import_module("dsp_test.main")
        config = {
            k: v["default"]
            for k, v in json.loads(
                (ROOT / "_conf_schema.json").read_text(encoding="utf-8")
            ).items()
        }
        config.update(
            web_token="placeholder",
            allowed_groups=["100"],
        )
        self.context = types.SimpleNamespace(send_message=AsyncMock(return_value=True))
        self.plugin = main.JingYuanQuan(self.context, config)
        await self.plugin.payment_api.close()
        fake = types.SimpleNamespace(
            create=AsyncMock(return_value=("up-1", "https://qr.95516.com/path")),
            orders=AsyncMock(return_value={"up-1": remote()}),
            close=AsyncMock(),
        )
        self.plugin.payment_api = self.plugin.payment_service.api = fake
        self.addAsyncCleanup(self.plugin.terminate)

    async def collect(self, gen):
        return [value async for value in gen]

    async def test_qr_mentions_requester_and_has_png(self):
        event = Event()
        result = await self.collect(self.plugin.payment_alipay_recharge(event, "20元"))
        chain = result[0]
        self.assertEqual(chain[0].qq, "123")
        self.assertIn("15 分钟", chain[1].text)
        self.assertTrue(chain[2].startswith(b"\x89PNG"))
        self.assertTrue(event.stopped)
        again = await self.collect(self.plugin.payment_alipay_recharge(event, "20元"))
        self.assertIn("不会重复", again[0])
        self.plugin.payment_api.create.assert_awaited_once()

    async def test_blocked_group_does_not_create(self):
        await self.collect(
            self.plugin.payment_alipay_recharge(Event(group="200"), "20")
        )
        self.plugin.payment_api.create.assert_not_awaited()

    async def test_private_cannot_create_but_can_read_own(self):
        await self.collect(self.plugin.payment_alipay_recharge(Event(group=""), "20"))
        self.plugin.payment_api.create.assert_not_awaited()
        result = await self.collect(self.plugin.payment_bills(Event(group="")))
        self.assertIn("暂无本人", result[0])

    async def test_bills_and_query_cannot_leak_other_users(self):
        await self.collect(self.plugin.payment_alipay_recharge(Event(), "20"))
        row = self.plugin.payment_store.history("default", "123")[0]
        result = await self.collect(self.plugin.payment_bills(Event(qq="456")))
        self.assertNotIn(row["id"], result[0])
        result = await self.collect(
            self.plugin.payment_query(Event(qq="456"), row["id"])
        )
        self.assertIn("不属于你", result[0])
        self.plugin.payment_api.orders.assert_not_awaited()

    async def test_only_paid_notifies_and_notification_is_at_correct_qq(self):
        await self.collect(self.plugin.payment_alipay_recharge(Event(), "20"))
        await self.plugin.payment_notify_paid()
        self.context.send_message.assert_not_awaited()
        row = self.plugin.payment_store.history("default", "123")[0]
        self.plugin.payment_store.apply(row["id"], remote(), row["created"] + 10)
        await self.plugin.payment_notify_paid()
        await self.plugin.payment_notify_paid()
        self.context.send_message.assert_awaited_once()
        self.assertEqual(self.plugin.ledger.wallet("123")["balance"], 20000)
        self.assertEqual(self.plugin.ledger.history("123")[0]["kind"], "supplier_topup")
        origin, chain = self.context.send_message.call_args.args
        self.assertEqual(origin, Event.unified_msg_origin)
        self.assertEqual(chain[0].qq, "123")

    async def test_missing_platform_does_not_mark_notified(self):
        await self.collect(self.plugin.payment_alipay_recharge(Event(), "20"))
        row = self.plugin.payment_store.history("default", "123")[0]
        self.plugin.payment_store.apply(row["id"], remote(), row["created"] + 10)
        self.context.send_message.return_value = False
        await self.plugin.payment_notify_paid()
        row = self.plugin.payment_store.own("default", "123", row["id"])
        self.assertEqual(row["notified"], 0)
        self.assertEqual(row["notify_attempts"], 1)
        self.assertEqual(self.plugin.ledger.wallet("123")["balance"], 20000)


if __name__ == "__main__":
    unittest.main()
