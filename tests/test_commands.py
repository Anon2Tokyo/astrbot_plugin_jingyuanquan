"""Command integration against an AstrBot API double (not a live framework test)."""

import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from test_core import ROOT


class Star:
    def __init__(self, context):
        self.context = context


class Event:
    def __init__(
        self, admin=False, group="100", message="你好 世界", mid="1", qq="12345"
    ):
        self.qq = qq
        self.extra = {}
        self.sent = []
        self.admin = admin
        self.group = group
        self.message_str = message
        self.message_obj = types.SimpleNamespace(message_id=mid)
        self.unified_msg_origin = "onebot:GroupMessage:" + group
        self.stopped = False

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

    def is_admin(self):
        return self.admin

    def is_stopped(self):
        return self.stopped

    def get_extra(self, key):
        return self.extra.get(key)

    def set_extra(self, key, value):
        self.extra[key] = value

    async def send(self, result):
        self.sent.append(result)

    def plain_result(self, text):
        return text


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        modules = {
            name: types.ModuleType(name)
            for name in (
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
        modules["astrbot.api.event"].MessageChain = list
        components = modules["astrbot.api.message_components"]
        components.At = lambda qq: types.SimpleNamespace(qq=qq)
        components.Plain = lambda text: types.SimpleNamespace(text=text)
        components.Image = types.SimpleNamespace(fromBytes=lambda data: data)
        modules["astrbot.api.event"].AstrMessageEvent = Event
        modules["astrbot.api.event"].filter = types.SimpleNamespace(
            command=lambda name: lambda func: func,
            on_llm_request=lambda **kw: lambda func: func,
            on_llm_response=lambda **kw: lambda func: func,
        )
        modules["astrbot.api.star"].Star = Star
        modules["astrbot.api.star"].Context = object
        modules["astrbot.api.star"].StarTools = types.SimpleNamespace(
            get_data_dir=lambda name: Path(self.temp.name)
        )
        from unittest.mock import patch

        self.patch = patch.dict(sys.modules, modules)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        sys.modules.pop("jq_test_plugin.main", None)
        main = importlib.import_module("jq_test_plugin.main")
        config = {
            k: v["default"]
            for k, v in json.loads(
                (ROOT / "_conf_schema.json").read_text(encoding="utf-8")
            ).items()
        }
        config.update(
            allowed_groups=["100"],
            cache_hit_price="1",
            cache_miss_price="2",
            output_price="3",
        )
        self.provider = types.SimpleNamespace(get_model=lambda: config["model"])
        context = types.SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="configured-provider"),
            get_provider_by_id=lambda _: self.provider,
        )
        self.plugin = main.JingYuanQuan(context, config)
        await self.plugin.initialize()
        self.addAsyncCleanup(self.plugin.terminate)

    async def collect(self, generator):
        return "\n".join([text async for text in generator])

    def fund(self):
        self.plugin.ledger.recharge("12345", "1", "admin", "seed")

    async def test_non_admin_recharge_denied(self):
        event = Event()
        result = await self.collect(self.plugin.recharge(event, "12345", "20"))
        self.assertIn("仅 AstrBot 管理员", result)
        self.assertEqual(self.plugin.ledger.wallet("12345")["balance"], 0)
        self.assertTrue(event.stopped)

    def request(self, **kwargs):
        return types.SimpleNamespace(
            model=None,
            prompt="你好",
            contexts=[{"role": "user", "content": "历史"}],
            system_prompt="人设",
            func_tool=None,
            **kwargs,
        )

    def response(self, **kwargs):
        values = dict(
            role="assistant",
            is_chunk=False,
            id="response-1",
            completion_text="正常回答",
            usage=types.SimpleNamespace(input_cached=10, input_other=20, output=30),
        )
        values.update(kwargs)
        return types.SimpleNamespace(**values)

    async def test_other_groups_and_private_are_not_intercepted(self):
        for group in ("200", ""):
            event = Event(group=group)
            await self.plugin.bill_chat_request(event, self.request())
            self.assertFalse(event.stopped)
            self.assertFalse(event.extra)
            self.assertFalse(event.sent)

    async def test_native_chat_keeps_prompt_context_and_settles_once(self):
        self.fund()
        event, request, response = Event(), self.request(), self.response()
        before = vars(request).copy()
        await self.plugin.bill_chat_request(event, request)
        self.assertFalse(event.stopped)
        self.assertEqual(vars(request), before)
        self.assertEqual(self.plugin.ledger.wallet("12345")["frozen"], 300)
        await self.plugin.bill_chat_response(event, response)
        await self.plugin.bill_chat_response(event, response)
        self.assertEqual(response.completion_text, "正常回答")
        self.assertEqual(self.plugin.ledger.wallet("12345")["balance"], 999)
        self.assertEqual(self.plugin.ledger.wallet("12345")["frozen"], 0)
        self.assertFalse(event.sent)
        duplicate = Event()
        await self.plugin.bill_chat_request(duplicate, request)
        self.assertTrue(duplicate.stopped)
        self.assertNotIn("jingyuanquan_chat", duplicate.extra)
        self.assertFalse(hasattr(self.plugin, "ask"))

    async def test_qq_wallets_are_independent_in_same_group(self):
        self.fund()
        a, b = Event(qq="12345"), Event(qq="67890", mid="2")
        await self.plugin.bill_chat_request(a, self.request())
        await self.plugin.bill_chat_request(b, self.request())
        self.assertFalse(a.stopped)
        self.assertTrue(b.stopped)
        await self.plugin.bill_chat_response(a, self.response())
        self.assertEqual(self.plugin.ledger.wallet("12345")["balance"], 999)
        self.assertEqual(self.plugin.ledger.wallet("67890")["balance"], 0)

    async def test_same_qq_shares_wallet_between_groups(self):
        self.fund()
        self.plugin.config["allowed_groups"].append("200")
        for group in ("100", "200"):
            event = Event(group=group)
            await self.plugin.bill_chat_request(event, self.request())
            await self.plugin.bill_chat_response(event, self.response())
        self.assertEqual(self.plugin.ledger.wallet("12345")["balance"], 998)

    async def test_missing_usage_retains_reservation(self):
        self.fund()
        event = Event()
        await self.plugin.bill_chat_request(event, self.request())
        await self.plugin.bill_chat_response(event, self.response(usage=None))
        self.assertEqual(len(self.plugin.ledger.pending_list()), 1)
        self.assertEqual(self.plugin.ledger.wallet("12345")["frozen"], 300)

    async def test_stream_chunks_are_not_billed(self):
        self.fund()
        event = Event()
        await self.plugin.bill_chat_request(event, self.request())
        await self.plugin.bill_chat_response(event, self.response(is_chunk=True))
        self.assertEqual(self.plugin.ledger.wallet("12345")["balance"], 1000)
        await self.plugin.bill_chat_response(event, self.response())
        self.assertEqual(self.plugin.ledger.wallet("12345")["balance"], 999)

    async def test_unavailable_tool_totals_go_to_review(self):
        self.fund()
        event, req = Event(), self.request()
        req.func_tool = object()
        await self.plugin.bill_chat_request(event, req)
        await self.plugin.bill_chat_response(event, self.response())
        self.assertEqual(len(self.plugin.ledger.pending_list()), 1)
        self.assertEqual(self.plugin.ledger.wallet("12345")["balance"], 1000)

    async def test_zero_prices_and_limit_stop_native_call(self):
        self.fund()
        self.plugin.config["output_price"] = "0"
        event = Event()
        await self.plugin.bill_chat_request(event, self.request())
        self.assertTrue(event.stopped)
        self.plugin.config["output_price"] = "3"
        self.plugin.ledger.set_limit("12345", 0, "admin", "limit")
        event = Event()
        await self.plugin.bill_chat_request(event, self.request())
        self.assertTrue(event.stopped)

    async def test_model_mismatch_stops_before_reservation(self):
        self.fund()
        req, event = self.request(), Event()
        req.model = "unpriced-model"
        await self.plugin.bill_chat_request(event, req)
        self.assertTrue(event.stopped)
        self.assertEqual(self.plugin.ledger.wallet("12345")["frozen"], 0)

    async def test_internal_error_stops_even_when_notification_fails(self):
        self.plugin.context.get_current_chat_provider_id.side_effect = RuntimeError(
            "internal"
        )
        event = Event()
        event.send = AsyncMock(side_effect=RuntimeError("send failed"))
        with self.assertRaises(RuntimeError):
            await self.plugin.bill_chat_request(event, self.request())
        self.assertTrue(event.stopped)

    async def test_reload_retains_unfinished_chat(self):
        self.fund()
        await self.plugin.bill_chat_request(Event(), self.request())
        await self.plugin.terminate()
        self.assertEqual(len(self.plugin.ledger.pending_list()), 1)

    async def test_recharge_still_idempotent(self):
        for _ in range(2):
            await self.collect(self.plugin.recharge(Event(admin=True), "12345", "1"))
        self.assertEqual(self.plugin.ledger.wallet("12345")["balance"], 1000)

    async def test_timeout_then_late_response_settles_once(self):
        from unittest.mock import patch

        self.fund()
        event = Event()
        await self.plugin.bill_chat_request(event, self.request())
        order = event.extra["jingyuanquan_chat"]["order"]
        task = self.plugin.chat_waiters.pop(order)
        task.cancel()
        import asyncio

        await asyncio.gather(task, return_exceptions=True)
        with patch("asyncio.sleep", new=AsyncMock()):
            await self.plugin.chat_timeout(order)
        self.assertEqual(len(self.plugin.ledger.pending_list()), 1)
        await self.plugin.bill_chat_response(event, self.response())
        self.assertEqual(self.plugin.ledger.wallet("12345")["balance"], 999)
        self.assertEqual(len(self.plugin.ledger.pending_list()), 0)

    async def test_cancelled_preflight_stops_event(self):
        import asyncio

        self.plugin.context.get_current_chat_provider_id.side_effect = (
            asyncio.CancelledError()
        )
        event = Event()
        with self.assertRaises(asyncio.CancelledError):
            await self.plugin.bill_chat_request(event, self.request())
        self.assertTrue(event.stopped)
