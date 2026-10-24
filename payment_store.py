"""Durable local QQ-to-order ownership and bounded polling schedule."""

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from enum import StrEnum

from .billing import EXCHANGE_RATE, decimal
from .ledger import Ledger
from .payment_api import money


class State(StrEnum):
    CREATING = "CREATING"
    PENDING = "PENDING"
    PAID = "PAID"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


LABELS = {
    "CREATING": "创建中",
    "PENDING": "未支付",
    "PAID": "已到账鲸元券",
    "FAILED": "支付失败",
    "EXPIRED": "已过有效期（未确认支付）",
    "REVIEW_REQUIRED": "需要核对",
}

CHANNELS = {"ALIPAY": "支付宝", "WECHAT": "微信"}


class Store:
    def __init__(self, path, default_limit=3000):
        self.path = str(path)
        self.ledger = Ledger(path, default_limit)
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL UNIQUE,
                    account TEXT NOT NULL, qq TEXT NOT NULL, origin TEXT NOT NULL,
                    amount TEXT NOT NULL, exchange_rate INTEGER NOT NULL, credit_amount INTEGER NOT NULL, request_id TEXT NOT NULL UNIQUE,
                    upstream_id TEXT, url TEXT, state TEXT NOT NULL,
                    upstream_status TEXT, paid_at TEXT, note TEXT NOT NULL DEFAULT '',
                    created REAL NOT NULL, next_check REAL NOT NULL,
                    polling_done INTEGER NOT NULL DEFAULT 0,
                    notified INTEGER NOT NULL DEFAULT 0,
                    notify_attempts INTEGER NOT NULL DEFAULT 0,
                    notify_after REAL NOT NULL DEFAULT 0,
                    UNIQUE(account, upstream_id));
                CREATE INDEX IF NOT EXISTS owner_idx ON orders(account,qq,created);
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(orders)")}
            if "channel" not in columns:
                db.execute(
                    "ALTER TABLE orders ADD COLUMN channel TEXT NOT NULL DEFAULT 'ALIPAY'"
                )
            if "actual_channel" not in columns:
                db.execute("ALTER TABLE orders ADD COLUMN actual_channel TEXT")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def recover(self):
        with self.db() as db:
            db.execute(
                "UPDATE orders SET state='REVIEW_REQUIRED',polling_done=1,note='创建中断，不能自动重试' WHERE state='CREATING'"
            )

    def create(self, account, qq, origin, source, amount, now, channel="ALIPAY"):
        if channel not in CHANNELS:
            raise ValueError("不支持的支付渠道")
        amount = money(amount)
        with self.db() as db:
            old = db.execute(
                "SELECT * FROM orders WHERE source=?", (source,)
            ).fetchone()
            if old:
                if old["qq"] != qq or old["account"] != account:
                    raise ValueError("消息来源与订单不一致")
                return dict(old), False
            # One current QR per user; manual repetition does not generate more payable orders.
            active = db.execute(
                "SELECT * FROM orders WHERE account=? AND qq=? AND state IN ('CREATING','PENDING') AND created>? ORDER BY created DESC LIMIT 1",
                (account, qq, now - 900),
            ).fetchone()
            if active:
                return dict(active), False
            latest = db.execute(
                "SELECT MAX(created) FROM orders WHERE account=? AND qq=?",
                (account, qq),
            ).fetchone()[0]
            if latest is not None and now - latest < 60:
                raise ValueError("创建充值订单过于频繁，请稍后再试")
            count = db.execute(
                "SELECT COUNT(*) FROM orders WHERE account=? AND polling_done=0",
                (account,),
            ).fetchone()[0]
            if count >= 100:
                raise ValueError("待处理订单较多，请稍后再试")
            oid = "DS" + uuid.uuid4().hex
            db.execute(
                "INSERT INTO orders(id,source,account,qq,origin,amount,exchange_rate,credit_amount,request_id,state,created,next_check) VALUES (?,?,?,?,?,?,?,?,?,'CREATING',?,?)",
                (
                    oid,
                    source,
                    account,
                    qq,
                    origin,
                    amount,
                    EXCHANGE_RATE,
                    int(decimal(amount) * EXCHANGE_RATE),
                    str(uuid.uuid4()),
                    now,
                    now + 10,
                ),
            )
            db.execute("UPDATE orders SET channel=? WHERE id=?", (channel, oid))
            return dict(
                db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            ), True

    def bind(self, oid, upstream_id, url):
        with self.db() as db:
            db.execute(
                "UPDATE orders SET upstream_id=?,url=?,state='PENDING' WHERE id=? AND state='CREATING'",
                (upstream_id, url, oid),
            )

    def review(self, oid, note):
        with self.db() as db:
            db.execute(
                "UPDATE orders SET state='REVIEW_REQUIRED',polling_done=1,note=? WHERE id=? AND state!='PAID'",
                (note, oid),
            )

    def own(self, account, qq, oid):
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM orders WHERE account=? AND qq=? AND id=?",
                (account, qq, oid),
            ).fetchone()
            if not row:
                raise ValueError("订单不存在或不属于你")
            return dict(row)

    def history(self, account, qq, page=1):
        if not 1 <= page <= 10000:
            raise ValueError("页码无效")
        with self.db() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM orders WHERE account=? AND qq=? ORDER BY created DESC,id DESC LIMIT 5 OFFSET ?",
                    (account, qq, (page - 1) * 5),
                )
            ]

    def due(self, account, now):
        with self.db() as db:
            db.execute(
                "UPDATE orders SET state='EXPIRED' WHERE account=? AND state='PENDING' AND created<=?",
                (account, now - 900),
            )
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM orders WHERE account=? AND polling_done=0 AND upstream_id IS NOT NULL AND next_check<=?",
                    (account, now),
                )
            ]

    def reschedule(self, oid, now):
        with self.db() as db:
            r = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            if r["polling_done"]:
                return
            age = now - r["created"]
            if age < 900:
                next_time = min(now + 10, r["created"] + 900)
            else:
                next_time = next(
                    (r["created"] + s for s in (960, 1200, 1800) if s > age), None
                )
            db.execute(
                "UPDATE orders SET next_check=?,polling_done=? WHERE id=?",
                (next_time or now, int(next_time is None), oid),
            )

    def apply(self, oid, upstream, now):
        with self.db() as db:
            row = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            if row["state"] == "PAID":
                return False  # Never regress a confirmed payment or notify again.
            if upstream is None:
                return False
            status = upstream.get("payment_order_status")
            actual_channel = upstream.get("payment_channel")
            if isinstance(actual_channel, str):
                db.execute(
                    "UPDATE orders SET actual_channel=? WHERE id=?",
                    (actual_channel, oid),
                )
            state, note = row["state"], ""
            try:
                if (
                    upstream.get("payment_order_id") != row["upstream_id"]
                    or upstream.get("currency") != "CNY"
                    or money(upstream.get("amount")) != row["amount"]
                ):
                    raise ValueError("金额、币种或订单号不符")
                if status == "SUCCESS":
                    paid_at = upstream.get("paid_at")
                    if (
                        not isinstance(paid_at, str)
                        or datetime.fromisoformat(paid_at.replace("Z", "+00:00")).tzinfo
                        is None
                    ):
                        raise ValueError("支付时间异常")
                    state = "PAID"
                elif status == "FAILED":
                    state = "FAILED"
                elif status == "CREATED":
                    state = "PENDING" if now - row["created"] < 900 else "EXPIRED"
                else:
                    raise ValueError("未知支付状态")
            except (ValueError, TypeError):
                state, note = "REVIEW_REQUIRED", "上游字段异常，禁止自动确认"
            db.execute(
                "UPDATE orders SET state=?,upstream_status=?,paid_at=?,note=?,polling_done=? WHERE id=?",
                (
                    state,
                    status if isinstance(status, str) else None,
                    upstream.get("paid_at") if state == "PAID" else None,
                    note,
                    int(state in ("PAID", "REVIEW_REQUIRED")),
                    oid,
                ),
            )
            if state == "PAID":
                # Same connection/transaction as the payment state transition.
                self.ledger.account(db, row["qq"])
                db.execute(
                    "UPDATE wallets SET balance=balance+? WHERE qq=?",
                    (row["credit_amount"], row["qq"]),
                )
                db.execute(
                    "INSERT INTO journal(id,source,qq,kind,delta,rmb,actor,note,exchange_rate) VALUES (?,?,?,'supplier_topup',?,?,'payment',?,?)",
                    (
                        oid,
                        "payment:" + oid,
                        row["qq"],
                        row["credit_amount"],
                        row["amount"],
                        "DeepSeek 供应商代充值",
                        row["exchange_rate"],
                    ),
                )
            return state == "PAID"

    def notifications(self, account, now):
        with self.db() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM orders WHERE account=? AND state='PAID' AND notified=0 AND notify_attempts<3 AND notify_after<=?",
                    (account, now),
                )
            ]

    def notification_attempt(self, oid, now):
        with self.db() as db:
            db.execute(
                "UPDATE orders SET notify_attempts=notify_attempts+1,notify_after=? WHERE id=?",
                (now + 60, oid),
            )

    def notified(self, oid):
        with self.db() as db:
            db.execute("UPDATE orders SET notified=1 WHERE id=?", (oid,))

    def total(self):
        with self.db() as db:
            return sum(
                (
                    decimal(r[0])
                    for r in db.execute(
                        "SELECT rmb FROM journal WHERE kind='supplier_topup'"
                    )
                ),
                decimal(0),
            )
