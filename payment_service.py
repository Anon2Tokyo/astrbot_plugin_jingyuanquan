import asyncio
import time

from .payment_api import AuthenticationError


class Service:
    def __init__(self, store, api, account, clock=time.time):
        self.store, self.api, self.account, self.clock = store, api, account, clock
        self.lock = asyncio.Lock()
        self.next_fetch = 0
        self.errors = 0
        self.auth_failed = False
        self.user_checks = {}

    async def create(self, qq, origin, source, amount, channel="ALIPAY"):
        async with self.lock:
            if self.auth_failed:
                raise ValueError("支付服务认证已暂停，请联系管理员")
            row, fresh = self.store.create(
                self.account, qq, origin, source, amount, self.clock(), channel
            )
            if not fresh:
                if row["channel"] != channel:
                    raise ValueError(
                        f"已有另一支付方式的未结束订单，请先处理该订单：{row['id']}"
                    )
                return row, False
            try:
                upstream_id, url = await self.api.create(
                    row["amount"], row["request_id"], row["channel"]
                )
                self.store.bind(row["id"], upstream_id, url)
            except asyncio.CancelledError:
                self.store.review(row["id"], "创建中断，需人工核对，不自动重试")
                raise
            except Exception as exc:  # noqa: BLE001 -- a POST may have succeeded before any failure
                self.store.review(
                    row["id"], "创建结果未确认，请管理员核对；不要立即重复充值"
                )
                if isinstance(exc, AuthenticationError):
                    self.auth_failed = True
                raise ValueError(
                    f"创建结果未确认，未发送二维码。请联系管理员核对。订单：{row['id']}"
                ) from None
            return self.store.own(self.account, qq, row["id"]), True

    async def _refresh(self, rows):
        now = self.clock()
        if not rows or self.auth_failed or now < self.next_fetch:
            return
        self.next_fetch = now + 10
        try:
            orders = await self.api.orders()
        except AuthenticationError:
            self.auth_failed = True
            raise
        except Exception:  # noqa: BLE001 -- all query failures must preserve payment states
            self.errors += 1
            self.next_fetch = now + min(300, 10 * 2 ** min(self.errors, 5))
            # Even an outage cannot leave an order auto-polling forever.
            for row in rows:
                if now - row["created"] >= 1800:
                    self.store.reschedule(row["id"], now)
            raise
        self.errors = 0
        for row in rows:
            self.store.apply(row["id"], orders.get(row["upstream_id"]), now)
            self.store.reschedule(row["id"], now)

    async def tick(self):
        async with self.lock:
            rows = self.store.due(self.account, self.clock())
            await self._refresh(rows)

    async def check(self, qq, oid):
        async with self.lock:
            now = self.clock()
            row = self.store.own(
                self.account, qq, oid
            )  # authorize BEFORE network access
            if now < self.user_checks.get(qq, 0):
                raise ValueError("主动查单每人 30 秒一次，请稍后再试")
            self.user_checks = {
                key: until for key, until in self.user_checks.items() if until > now
            }
            self.user_checks[qq] = now + 30
            if row["upstream_id"] and row["state"] != "PAID":
                if self.auth_failed:
                    raise ValueError("支付服务认证已暂停，订单保留，请联系管理员")
                await self._refresh([row])
            return self.store.own(self.account, qq, oid)
