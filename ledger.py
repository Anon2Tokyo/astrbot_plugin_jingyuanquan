"""Every balance mutation and journal entry share a SQLite transaction."""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .billing import EXCHANGE_RATE, Price, decimal, units

TZ = timezone(timedelta(hours=8))


def today():
    return datetime.now(TZ).date().isoformat()


class Ledger:
    def __init__(self, path, default_limit):
        self.path = str(path)
        self.default_limit = default_limit
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("不支持的账本版本，请使用对应版本插件")
            if version == 0:
                tables = {
                    r[0]
                    for r in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                for table in ("wallets", "journal", "requests"):
                    if (
                        table in tables
                        and db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                    ):
                        raise ValueError(
                            "检测到旧版微券账本，请先备份并单独迁移；不能直接按整数鲸元券读取"
                        )
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS wallets (
                    qq TEXT PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0 CHECK(balance>=0),
                    daily_limit INTEGER NOT NULL CHECK(daily_limit>=0));
                CREATE TABLE IF NOT EXISTS journal (
                    id TEXT PRIMARY KEY, source TEXT UNIQUE NOT NULL, qq TEXT NOT NULL,
                    kind TEXT NOT NULL, delta INTEGER NOT NULL, rmb TEXT NOT NULL,
                    actor TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
                    exchange_rate INTEGER NOT NULL DEFAULT 1000,
                    created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, source TEXT UNIQUE NOT NULL, qq TEXT NOT NULL,
                    day TEXT NOT NULL, reserved INTEGER NOT NULL, state TEXT NOT NULL,
                    price TEXT NOT NULL, charged INTEGER NOT NULL DEFAULT 0,
                    cost TEXT, usage TEXT, provider_id TEXT, note TEXT NOT NULL DEFAULT '',
                    created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE INDEX IF NOT EXISTS requests_user_day ON requests(qq,day,state);
                CREATE INDEX IF NOT EXISTS journal_user ON journal(qq,created);
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(journal)")}
            if "exchange_rate" not in columns:
                db.execute(
                    "ALTER TABLE journal ADD COLUMN exchange_rate INTEGER NOT NULL DEFAULT 1000"
                )
            db.execute("PRAGMA user_version=1")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def account(self, db, qq):
        if not qq.isascii() or not qq.isdigit():
            raise ValueError("QQ 号必须是数字")
        db.execute(
            "INSERT OR IGNORE INTO wallets(qq,daily_limit) VALUES (?,?)",
            (qq, self.default_limit),
        )

    def _wallet(self, db, qq, day):
        self.account(db, qq)
        wallet = dict(db.execute("SELECT * FROM wallets WHERE qq=?", (qq,)).fetchone())
        wallet["frozen"] = db.execute(
            "SELECT COALESCE(SUM(reserved),0) FROM requests WHERE qq=? AND state IN ('reserved','pending')",
            (qq,),
        ).fetchone()[0]
        wallet["used"] = db.execute(
            "SELECT COALESCE(SUM(CASE WHEN state IN ('reserved','pending') THEN reserved ELSE charged END),0) FROM requests WHERE qq=? AND day=?",
            (qq, day),
        ).fetchone()[0]
        return wallet

    def wallet(self, qq):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._wallet(db, qq, today())

    def recharge(self, qq, rmb, actor, source):
        amount = decimal(rmb)
        if amount <= 0 or amount * 100 != (amount * 100).to_integral_value():
            raise ValueError("充值金额须大于零，最多两位小数")
        delta = units(amount * EXCHANGE_RATE)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM journal WHERE source=?", (source,)
            ).fetchone()
            if old:
                return dict(old), False
            self.account(db, qq)
            order = "JY" + uuid.uuid4().hex
            db.execute("UPDATE wallets SET balance=balance+? WHERE qq=?", (delta, qq))
            db.execute(
                "INSERT INTO journal(id,source,qq,kind,delta,rmb,actor,exchange_rate) VALUES (?,?,?,'recharge',?,?,?,?)",
                (order, source, qq, delta, str(amount), actor, EXCHANGE_RATE),
            )
            return dict(
                db.execute("SELECT * FROM journal WHERE id=?", (order,)).fetchone()
            ), True

    def set_limit(self, qq, limit, actor, source):
        limit = units(limit)
        if limit < 0:
            raise ValueError("限额不能小于零")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM journal WHERE source=?", (source,)).fetchone():
                return
            self.account(db, qq)
            db.execute("UPDATE wallets SET daily_limit=? WHERE qq=?", (limit, qq))
            db.execute(
                "INSERT INTO journal(id,source,qq,kind,delta,rmb,actor,note) VALUES (?,?,?,'limit',0,'0',?,?)",
                (uuid.uuid4().hex, source, qq, actor, str(limit)),
            )

    def reserve(self, qq, source, amount, price, day=None):
        amount = units(amount)
        if amount <= 0:
            raise ValueError("预占必须大于零")
        day = day or today()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM requests WHERE source=?", (source,)
            ).fetchone()
            if old:
                return dict(old), False
            wallet = self._wallet(db, qq, day)
            if wallet["balance"] - wallet["frozen"] < amount:
                raise ValueError("可用余额不足以预占本次请求，请先充值")
            if wallet["daily_limit"] - wallet["used"] < amount:
                raise ValueError("今日剩余额度不足以预占本次请求")
            order = "AI" + uuid.uuid4().hex
            db.execute(
                "INSERT INTO requests(id,source,qq,day,reserved,state,price) VALUES (?,?,?,?,?,'reserved',?)",
                (order, source, qq, day, amount, price.snapshot()),
            )
            return dict(
                db.execute("SELECT * FROM requests WHERE id=?", (order,)).fetchone()
            ), True

    def pending(self, order, reason):
        with self.connect() as db:
            db.execute(
                "UPDATE requests SET state='pending',note=? WHERE id=? AND state='reserved'",
                (reason, order),
            )

    def recover(self):
        with self.connect() as db:
            db.execute(
                "UPDATE requests SET state='pending',note='进程中断，需人工核对' WHERE state='reserved'"
            )

    def finish(
        self,
        order,
        cost,
        actor="system",
        usage=None,
        provider_id=None,
        note="",
        release=False,
        manual=False,
    ):
        cost = decimal(cost)
        if release and cost != 0:
            raise ValueError("释放请求的成本必须为零")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM requests WHERE id=?", (order,)).fetchone()
            if row is None:
                raise ValueError("请求单号不存在")
            if row["state"] in ("settled", "released"):
                return dict(row)
            if manual and row["state"] != "pending":
                raise ValueError("只能核对待核对请求，不能结算运行中的请求")
            expected = Price.restore(row["price"]).charge(cost)
            charged = min(expected, row["reserved"])
            if expected > charged:
                note += f"; 超出预占，运营方承担差额={expected - charged} 鲸元券"
            state = "released" if release else "settled"
            db.execute(
                "UPDATE wallets SET balance=balance-? WHERE qq=?", (charged, row["qq"])
            )
            db.execute(
                "UPDATE requests SET state=?,charged=?,cost=?,usage=?,provider_id=?,note=? WHERE id=?",
                (
                    state,
                    charged,
                    str(cost),
                    json.dumps(usage) if usage is not None else None,
                    provider_id,
                    note,
                    order,
                ),
            )
            db.execute(
                "INSERT INTO journal(id,source,qq,kind,delta,rmb,actor,note,exchange_rate) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    uuid.uuid4().hex,
                    order,
                    row["qq"],
                    state,
                    -charged,
                    str(cost),
                    actor,
                    note,
                    Price.restore(row["price"]).exchange_rate,
                ),
            )
            return dict(
                db.execute("SELECT * FROM requests WHERE id=?", (order,)).fetchone()
            )

    def history(self, qq):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM journal WHERE qq=? ORDER BY rowid DESC LIMIT 10",
                    (qq,),
                )
            ]

    def pending_list(self):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM requests WHERE state='pending' ORDER BY created LIMIT 20"
                )
            ]

    def stats(self):
        with self.connect() as db:
            income = sum(
                (
                    Decimal(r[0])
                    for r in db.execute("SELECT rmb FROM journal WHERE kind='recharge'")
                ),
                Decimal(0),
            )
            cost = sum(
                (
                    Decimal(r[0])
                    for r in db.execute(
                        "SELECT cost FROM requests WHERE cost IS NOT NULL"
                    )
                ),
                Decimal(0),
            )
            consumed = db.execute(
                "SELECT COALESCE(SUM(charged),0) FROM requests"
            ).fetchone()[0]
            pending = db.execute(
                "SELECT COUNT(*) FROM requests WHERE state IN ('pending','reserved')"
            ).fetchone()[0]
            users = [
                dict(r)
                for r in db.execute(
                    "SELECT qq,SUM(charged) AS total FROM requests GROUP BY qq ORDER BY total DESC LIMIT 10"
                )
            ]
            revenue = sum(
                (
                    Decimal(r[0]) / Price.restore(r[1]).exchange_rate
                    for r in db.execute(
                        "SELECT charged,price FROM requests WHERE state='settled'"
                    )
                ),
                Decimal(0),
            )
            return income, cost, consumed, pending, users, revenue
