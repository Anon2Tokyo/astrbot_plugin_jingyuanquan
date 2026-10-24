import asyncio
import hashlib
import io
import json
import time

import qrcode
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Image, Plain
from astrbot.api.star import Context, Star, StarTools

from .billing import Price, display, units
from .chat_billing import final_usage
from .ledger import Ledger
from .payment_api import AuthenticationError, DeepSeekPayments, PaymentError, money
from .payment_service import Service
from .payment_store import CHANNELS, LABELS, Store


class JingYuanQuan(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.ledger = Ledger(
            StarTools.get_data_dir("astrbot_plugin_jingyuanquan") / "ledger.sqlite3",
            units(config.get("default_daily_limit", "3000")),
        )
        self.chat_waiters = {}
        self.active = set()
        self.stopping = False
        self.payment_account = "default"
        self.payment_store = Store(self.ledger.path, self.ledger.default_limit)
        self.payment_api = DeepSeekPayments(config.get("web_token", ""))
        self.payment_service = Service(
            self.payment_store, self.payment_api, self.payment_account
        )
        self.payment_worker = None

    async def initialize(self):
        self.ledger.recover()
        self.payment_store.recover()
        self.payment_worker = asyncio.create_task(self.payment_poll_loop())

    async def terminate(self):
        self.stopping = True
        tasks = list(self.active)
        if self.payment_worker:
            tasks.append(self.payment_worker)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for order, task in list(self.chat_waiters.items()):
            task.cancel()
            self.ledger.pending(order, "插件重载，等待核对 AstrBot 调用")
        if self.chat_waiters:
            await asyncio.gather(
                *list(self.chat_waiters.values()), return_exceptions=True
            )
        self.chat_waiters.clear()
        await self.payment_api.close()

    def guard(self, event, admin=False):
        event.stop_event()
        if self.stopping:
            raise ValueError("插件正在重载，请稍后再试")
        if event.get_platform_name() != "aiocqhttp":
            raise ValueError("本插件仅支持 NapCat / OneBot QQ")
        if admin and not event.is_admin():
            raise ValueError("仅 AstrBot 管理员可执行")
        if not admin:
            groups = {str(g) for g in self.config.get("allowed_groups", [])}
            if not event.get_group_id() or str(event.get_group_id()) not in groups:
                raise ValueError("当前群未开放鲸元券服务")
        return str(event.get_sender_id())

    def source(self, event, kind):
        message_id = getattr(event.message_obj, "message_id", None)
        if message_id is None or str(message_id) == "":
            raise ValueError("消息缺少编号，无法安全入账")
        raw = [
            kind,
            event.get_self_id(),
            event.unified_msg_origin,
            str(message_id),
            event.get_sender_id(),
        ]
        return hashlib.sha256(json.dumps(raw, ensure_ascii=False).encode()).hexdigest()

    @filter.command("鲸元券")
    async def wallet(self, event: AstrMessageEvent):
        """查询钱包及今日额度。"""
        try:
            qq = self.guard(event)
            w = self.ledger.wallet(qq)
            text = f"余额：{display(w['balance'])} 鲸元券\n冻结：{display(w['frozen'])} 鲸元券\n可用：{display(w['balance'] - w['frozen'])} 鲸元券\n今日额度（含预占）：{display(w['used'])} / {display(w['daily_limit'])} 鲸元券"
        except ValueError as exc:
            text = str(exc)
        yield event.plain_result(text)

    @filter.command("鲸元券充值")
    async def recharge(self, event: AstrMessageEvent, qq: str, rmb: str):
        """管理员充值：QQ号 人民币金额。"""
        try:
            actor = self.guard(event, admin=True)
            row, fresh = self.ledger.recharge(
                qq, rmb, actor, self.source(event, "recharge")
            )
            text = f"{'充值成功' if fresh else '本条消息已入账，未重复充值'}\n用户：{row['qq']}\n实收：{row['rmb']} 元\n入账：{display(row['delta'])} 鲸元券\n充值单号：{row['id']}"
        except ValueError as exc:
            text = str(exc)
        yield event.plain_result(text)

    @filter.command("鲸元券限额")
    async def limit(self, event: AstrMessageEvent, qq: str, amount: str):
        """管理员设置每日上限：QQ号 鲸元券数。"""
        try:
            actor = self.guard(event, admin=True)
            self.ledger.set_limit(qq, units(amount), actor, self.source(event, "limit"))
            text = "每日上限已设置；已预占的请求仍按原预算结算。"
        except ValueError as exc:
            text = str(exc)
        yield event.plain_result(text)

    @filter.command("鲸元券流水")
    async def history(self, event: AstrMessageEvent):
        """查看最近十条账户记录。"""
        try:
            qq = self.guard(event)
            labels = {
                "recharge": "人工充值",
                "supplier_topup": "扫码充值",
                "settled": "消费",
                "released": "解冻",
                "limit": "调整限额",
            }
            rows = self.ledger.history(qq)
            text = (
                "\n".join(
                    f"{r['created']} UTC {labels[r['kind']]} {display(r['delta'])} 鲸元券\n单号：{r['source'] if r['kind'] in ('settled', 'released') else r['id']}"
                    for r in rows
                )
                or "暂无流水"
            )
        except ValueError as exc:
            text = str(exc)
        yield event.plain_result(text)

    def billed_group(self, event):
        return (
            event.get_platform_name() == "aiocqhttp"
            and bool(event.get_group_id())
            and str(event.get_group_id())
            in {str(g) for g in self.config.get("allowed_groups", [])}
        )

    async def chat_timeout(self, order):
        try:
            await asyncio.sleep(600)
            self.ledger.pending(order, "十分钟未取得完整回复用量，需核对")
        finally:
            self.chat_waiters.pop(order, None)

    @filter.on_llm_request(priority=-10000)
    async def bill_chat_request(self, event: AstrMessageEvent, req):
        """Check the sender's wallet before AstrBot's existing chat call."""
        if not self.billed_group(event) or event.is_stopped():
            return
        row = None
        try:
            if self.stopping:
                raise ValueError("插件正在重载，请稍后再试")
            price = Price.from_config(self.config)
            selected = event.get_extra("selected_provider")
            if selected:
                provider = self.context.get_provider_by_id(selected)
            else:
                provider_id = await self.context.get_current_chat_provider_id(
                    event.unified_msg_origin
                )
                provider = self.context.get_provider_by_id(provider_id)
            model = getattr(req, "model", None) or provider.get_model()
            if model != price.model:
                raise ValueError("当前聊天模型尚未配置匹配的鲸元券单价，请联系管理员")
            budget = units(self.config.get("chat_reserve", "300"))
            if budget <= 0:
                raise ValueError("聊天预占金额必须大于零")
            row, fresh = self.ledger.reserve(
                str(event.get_sender_id()), self.source(event, "chat"), budget, price
            )
            if not fresh:
                raise ValueError("本条聊天已受理，不会重复调用或扣费")
            event.set_extra(
                "jingyuanquan_chat",
                {
                    "order": row["id"],
                    "source": row["source"],
                    "qq": row["qq"],
                    "price": row["price"],
                    "tools": bool(getattr(req, "func_tool", None)),
                },
            )
            self.chat_waiters[row["id"]] = asyncio.create_task(
                self.chat_timeout(row["id"])
            )
        except asyncio.CancelledError:
            event.stop_event()
            if row:
                self.ledger.pending(row["id"], "聊天计费入口中断，需核对")
            raise
        except Exception as exc:  # noqa: BLE001 -- hook errors must fail closed
            event.stop_event()
            if (
                row
                and row.get("state") == "reserved"
                and row["id"] not in self.chat_waiters
            ):
                self.ledger.pending(row["id"], "聊天计费入口异常，需核对")
            text = (
                str(exc)
                if isinstance(exc, ValueError)
                else "计费暂不可用，请联系管理员"
            )
            await event.send(event.plain_result(text))

    @filter.on_llm_response(priority=10000)
    async def bill_chat_response(self, event: AstrMessageEvent, response):
        """Settle using native usage; leave AstrBot's answer and context intact."""
        record = event.get_extra("jingyuanquan_chat")
        if not record or getattr(response, "is_chunk", False):
            return
        if record["qq"] != str(event.get_sender_id()) or record[
            "source"
        ] != self.source(event, "chat"):
            return
        order = record["order"]
        try:
            price = Price.restore(record["price"])
            usage = final_usage(event, response, record["tools"], price.model)
            self.ledger.finish(
                order,
                price.cost(usage),
                usage={
                    "cache_hit": usage.hit,
                    "cache_miss": usage.miss,
                    "output": usage.output,
                },
                provider_id=getattr(response, "id", None),
            )
        except Exception as exc:  # noqa: BLE001 -- never charge with incomplete usage
            self.ledger.pending(order, "AstrBot 用量或结算异常，需核对")
            logger.warning("鲸元券聊天待核对: %s (%s)", order, type(exc).__name__)
        finally:
            waiter = self.chat_waiters.pop(order, None)
            if waiter:
                waiter.cancel()

    @filter.command("鲸元券待核对")
    async def pending(self, event: AstrMessageEvent):
        """管理员查看待核对请求，最多二十条。"""
        try:
            self.guard(event, admin=True)
            text = (
                "\n".join(
                    f"{r['id']} QQ={r['qq']} 冻结={display(r['reserved'])} 鲸元券"
                    for r in self.ledger.pending_list()
                )
                or "暂无待核对请求"
            )
        except ValueError as exc:
            text = str(exc)
        yield event.plain_result(text)

    @filter.command("鲸元券核对")
    async def reconcile(
        self, event: AstrMessageEvent, order: str, cost_rmb: str, reason: str
    ):
        """管理员核对：请求单号 实际成本元 核对依据（不含空格）。"""
        try:
            actor = self.guard(event, admin=True)
            if len(reason) < 2:
                raise ValueError("请填写核对依据")
            row = self.ledger.finish(
                order, cost_rmb, actor=actor, note="人工核对：" + reason, manual=True
            )
            text = f"核对完成：{row['id']}\n最终扣费：{display(row['charged'])} 鲸元券"
        except ValueError as exc:
            text = str(exc)
        yield event.plain_result(text)

    @filter.command("鲸元券统计")
    async def stats(self, event: AstrMessageEvent):
        """管理员查看累计成本及用户消费统计。"""
        try:
            self.guard(event, admin=True)
            income, cost, consumed, pending, users, revenue = self.ledger.stats()
            text = (
                f"供应商代充值：{self.payment_store.total()} 元\n累计人工充值实收：{income} 元\n累计消费：{display(consumed)} 鲸元券\n模型成本（配置价核算）：{cost} 元\n消费对应收入：{revenue} 元\n估算毛利：{revenue - cost} 元\n未结算请求：{pending}（成本未计入）\n消费前十：\n"
                + "\n".join(f"{u['qq']}：{display(u['total'])} 鲸元券" for u in users)
            )
        except ValueError as exc:
            text = str(exc)
        yield event.plain_result(text)

    def payment_ready(self):
        return bool(self.config.get("web_token", "").strip())

    def payment_guard(self, event, recharge=False):
        event.stop_event()
        if self.stopping:
            raise ValueError("插件正在重载，请稍后再试")
        if event.get_platform_name() != "aiocqhttp":
            raise ValueError("仅支持 NapCat / OneBot QQ")
        group = str(event.get_group_id() or "")
        allowed = {str(g) for g in self.config.get("allowed_groups", [])}
        if (group and group not in allowed) or (recharge and not group):
            raise ValueError("请在已开放的 QQ 群内发起充值")
        qq = str(event.get_sender_id())
        if not qq.isascii() or not qq.isdigit():
            raise ValueError("无法识别 QQ 用户")
        return qq

    def payment_source(self, event):
        mid = getattr(event.message_obj, "message_id", None)
        if mid is None or str(mid) == "":
            raise ValueError("消息缺少唯一编号，无法安全创建订单")
        key = [
            self.payment_account,
            event.get_self_id(),
            event.unified_msg_origin,
            event.get_sender_id(),
            str(mid),
        ]
        return hashlib.sha256(json.dumps(key).encode()).hexdigest()

    @staticmethod
    def payment_summary(row):
        return (
            f"订单：{row['id']}\n金额：{row['amount']} 元\n鲸元券：{row['credit_amount']}\n选择方式：{CHANNELS[row['channel']]}\n状态：{LABELS[row['state']]}"
            + (
                f"\n实际渠道：{CHANNELS.get(row['actual_channel'], '其他')}"
                if row.get("actual_channel")
                else ""
            )
        )

    @filter.command("鲸元券扫码充值")
    async def payment_choose_payment(self, event: AstrMessageEvent):
        """显示微信与鲸元券支付宝充值入口，不创建订单。"""
        try:
            self.payment_guard(event, recharge=True)
            yield event.plain_result(
                "鲸元券扫码充值，请输入 1 元至 100000 元之间的整数金额。请选择支付方式：\n/鲸元券微信充值 20元\n/鲸元券支付宝充值 20元\n款项进入运营方的 DeepSeek 账户。"
            )
        except ValueError as exc:
            yield event.plain_result(str(exc))

    @filter.command("鲸元券微信充值")
    async def payment_wechat_recharge(self, event: AstrMessageEvent, amount: str = ""):
        """使用微信充值：/鲸元券微信充值 20元。"""
        async for result in self.payment_recharge(event, amount, "WECHAT"):
            yield result

    @filter.command("鲸元券支付宝充值")
    async def payment_alipay_recharge(self, event: AstrMessageEvent, amount: str = ""):
        """使用鲸元券支付宝充值：/鲸元券支付宝充值 20元。"""
        async for result in self.payment_recharge(event, amount, "ALIPAY"):
            yield result

    async def payment_recharge(
        self, event: AstrMessageEvent, amount: str, channel: str
    ):
        task = asyncio.current_task()
        self.active.add(task)
        try:
            qq = self.payment_guard(event, recharge=True)
            if not amount:
                command = (
                    "鲸元券微信充值" if channel == "WECHAT" else "鲸元券支付宝充值"
                )
                raise ValueError(
                    f"用法：/{command} 20元；款项充值到运营方的 DeepSeek 账户"
                )
            if not self.payment_ready():
                raise ValueError("管理员尚未配置支付服务")
            amount = money(amount.removesuffix("元"))
            row, fresh = await self.payment_service.create(
                qq,
                event.unified_msg_origin,
                self.payment_source(event),
                amount,
                channel,
            )
            if not fresh:
                yield event.plain_result(
                    "已有订单，不会重复创建或发码。\n"
                    + self.payment_summary(row)
                    + "\n可用 /鲸元券充值查询 订单号 查询。"
                )
                return
            buffer = io.BytesIO()
            qrcode.make(row["url"]).save(buffer, format="PNG")
            yield event.chain_result(
                [
                    At(qq=qq),
                    Plain(
                        f"\n充值 {row['amount']} 元，到账 {row['credit_amount']} 鲸元券\n订单：{row['id']}\n请在 15 分钟内完成支付。款项进入运营方的 DeepSeek 账户。\n请使用{CHANNELS[row['channel']]}扫码。"
                    ),
                    Image.fromBytes(buffer.getvalue()),
                ]
            )
        except (ValueError, PaymentError) as exc:
            yield event.plain_result(str(exc))
        except Exception as exc:  # noqa: BLE001 -- do not disclose upstream/session data
            logger.warning("鲸元券扫码充值 充值处理异常 (%s)", type(exc).__name__)
            yield event.plain_result(
                "处理异常，请使用 /鲸元券充值账单 查看是否已创建订单，勿立即重复充值。"
            )
        finally:
            self.active.discard(task)

    @filter.command("鲸元券充值账单")
    async def payment_bills(self, event: AstrMessageEvent, page: int = 1):
        """只查看本人在本插件创建的订单，每页五条。"""
        try:
            qq = self.payment_guard(event)
            rows = self.payment_store.history(self.payment_account, qq, page)
            text = (
                "\n\n".join(self.payment_summary(row) for row in rows)
                or "暂无本人充值记录"
            )
            yield event.plain_result(text)
        except ValueError as exc:
            yield event.plain_result(str(exc))

    @filter.command("鲸元券充值查询")
    async def payment_query(self, event: AstrMessageEvent, order_id: str = ""):
        """查询本人的订单；省略订单号查询最近一笔。"""
        task = asyncio.current_task()
        self.active.add(task)
        try:
            qq = self.payment_guard(event)
            if not order_id:
                rows = self.payment_store.history(self.payment_account, qq)
                if not rows:
                    raise ValueError("暂无本人充值记录")
                order_id = rows[0]["id"]
            if not self.payment_ready():
                row = self.payment_store.own(self.payment_account, qq, order_id)
            else:
                row = await self.payment_service.check(qq, order_id)
            yield event.plain_result(
                "最近确认的结果（查询有冷却间隔）：\n" + self.payment_summary(row)
            )
        except (ValueError, PaymentError) as exc:
            yield event.plain_result(str(exc))
        finally:
            self.active.discard(task)

    async def payment_notify_paid(self):
        for row in self.payment_store.notifications(self.payment_account, time.time()):
            self.payment_store.notification_attempt(row["id"], time.time())
            try:
                sent = await asyncio.wait_for(
                    self.context.send_message(
                        row["origin"],
                        MessageChain(
                            [
                                At(qq=row["qq"]),
                                Plain(
                                    f"\n充值成功，已入账 {row['credit_amount']} 鲸元券（{row['amount']} 元）。\n订单：{row['id']}"
                                ),
                            ]
                        ),
                    ),
                    timeout=20,
                )
                if sent is False:
                    raise RuntimeError("未找到原群平台")
                self.payment_store.notified(row["id"])
            except Exception as exc:  # noqa: BLE001 -- payment is final even if QQ send fails
                logger.warning(
                    "鲸元券扫码充值 到账通知发送失败 (%s)", type(exc).__name__
                )

    async def payment_poll_loop(self):
        while True:
            try:
                if self.payment_ready():
                    await self.payment_service.tick()
                await self.payment_notify_paid()
            except AuthenticationError:
                logger.warning(
                    "鲸元券扫码充值 登录认证失败，已暂停网络查询；请更新凭证并重载插件"
                )
            except Exception as exc:  # noqa: BLE001 -- keep worker alive; service handles backoff
                logger.warning("鲸元券扫码充值 后台处理失败 (%s)", type(exc).__name__)
            await asyncio.sleep(2)
