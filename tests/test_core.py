import importlib
import sqlite3
import sys
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from decimal import Decimal
from pathlib import Path

# Load under a package name without requiring a running AstrBot installation.
ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("jq_test_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("jq_test_plugin", pkg)
billing = importlib.import_module("jq_test_plugin.billing")
ledger = importlib.import_module("jq_test_plugin.ledger")
PRICE = billing.Price("test-model", Decimal(1), Decimal(2), Decimal(3), Decimal("1.5"))
USAGE = {
    "prompt_cache_hit_tokens": 10,
    "prompt_cache_miss_tokens": 20,
    "completion_tokens": 30,
    "prompt_tokens": 30,
    "total_tokens": 60,
}


class LedgerTests(unittest.TestCase):
    def test_integer_rounding(self):
        for cost, expected in (
            ("0", 0),
            ("0.0005", 1),
            ("0.001", 2),
            ("0.01", 15),
            ("0.1", 150),
        ):
            self.assertEqual(PRICE.charge(cost), expected)
        for value in ("0.5", "1.2", "NaN", "-1"):
            with self.assertRaises(ValueError):
                billing.units(value)
        self.assertEqual(billing.display(20000), "20000")

    def test_cent_recharge_and_rate_snapshot(self):
        row, _ = self.book.recharge("12345", "0.01", "admin", "cent")
        self.assertEqual(row["delta"], 10)
        self.assertEqual(row["exchange_rate"], 1000)

    def test_snapshot_revenue_survives_rate_change(self):
        other = billing.Price(
            "test", Decimal(1), Decimal(2), Decimal(3), Decimal("1.5"), 2000
        )
        row, _ = self.book.reserve("12345", "other", 100, other)
        self.book.finish(row["id"], ".01")
        self.assertEqual(self.book.stats()[-1], Decimal(".015"))
        self.assertEqual(
            PriceRestored := billing.Price.restore(other.snapshot()), other
        )
        self.assertEqual(PriceRestored.charge(".01"), 30)

    def test_old_ledger_refused_without_changing_balance(self):
        path = Path(self.temp.name) / "old.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE wallets(qq TEXT, balance INTEGER)")
            db.execute("INSERT INTO wallets VALUES ('12345', 200000000)")
            db.commit()
        with self.assertRaisesRegex(ValueError, "旧版"):
            ledger.Ledger(path, 3000)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(
                db.execute("SELECT balance FROM wallets").fetchone()[0], 200000000
            )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.book = ledger.Ledger(
            Path(self.temp.name) / "test.sqlite3", billing.units("3000")
        )
        self.book.recharge("12345", "20", "admin", "topup")

    def reserve(self, source="ask", amount="100", day=None):
        return self.book.reserve("12345", source, billing.units(amount), PRICE, day)[0]

    def test_recharge_idempotency_and_generated_number(self):
        row, fresh = self.book.recharge("12345", "20", "admin", "topup")
        self.assertFalse(fresh)
        self.assertTrue(row["id"].startswith("JY"))
        self.assertEqual(self.book.wallet("12345")["balance"], billing.units("20000"))

    def test_concurrent_reservations_cannot_exceed_daily_limit(self):
        self.book.set_limit("12345", billing.units("100"), "admin", "limit")

        def attempt(i):
            try:
                self.reserve(str(i), "60")
                return True
            except ValueError:
                return False

        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(attempt, range(16))), 1)
        self.assertEqual(self.book.wallet("12345")["frozen"], billing.units("60"))

    def test_wallet_budget_independent_of_daily_limit(self):
        self.book.set_limit("12345", billing.units("100000"), "admin", "limit")
        self.reserve(amount="15000")
        with self.assertRaises(ValueError):
            self.reserve("second", "5100")

    def test_request_duplicate_returns_existing(self):
        row = self.reserve()
        duplicate, fresh = self.book.reserve(
            "12345", "ask", billing.units("100"), PRICE
        )
        self.assertFalse(fresh)
        self.assertEqual(duplicate["id"], row["id"])

    def test_settlement_idempotency_and_refund(self):
        row = self.reserve()
        for _ in range(2):
            self.book.finish(row["id"], ".01", usage=USAGE)
        wallet = self.book.wallet("12345")
        self.assertEqual(wallet["balance"], billing.units("19985"))
        self.assertEqual(wallet["frozen"], 0)
        self.assertEqual(wallet["used"], billing.units("15"))
        self.assertEqual(len(self.book.history("12345")), 2)

    def test_cross_day_settlement_stays_on_request_day(self):
        row = self.reserve(day="2020-01-01")
        self.assertEqual(self.book.wallet("12345")["used"], 0)
        self.assertEqual(self.book.wallet("12345")["frozen"], billing.units("100"))
        self.book.finish(row["id"], ".01")
        self.assertEqual(self.book.wallet("12345")["used"], 0)

    def test_recovery_and_manual_reconciliation(self):
        row = self.reserve()
        with self.assertRaises(ValueError):
            self.book.finish(row["id"], 0, manual=True)
        restarted = ledger.Ledger(self.book.path, billing.units("3000"))
        restarted.recover()
        self.assertEqual(restarted.pending_list()[0]["id"], row["id"])
        restarted.finish(row["id"], 0, actor="admin", note="verified", manual=True)
        self.assertEqual(restarted.wallet("12345")["frozen"], 0)

    def test_budget_overrun_is_not_charged_to_user(self):
        row = self.reserve()
        result = self.book.finish(row["id"], "1")
        self.assertEqual(result["charged"], billing.units("100"))
        self.assertIn("运营方", result["note"])
        self.assertEqual(self.book.stats()[1], Decimal(1))

    def test_explicit_rejection_releases_daily_and_wallet_budget(self):
        row = self.reserve()
        self.book.finish(row["id"], 0, release=True)
        wallet = self.book.wallet("12345")
        self.assertEqual(wallet["frozen"], 0)
        self.assertEqual(wallet["used"], 0)

    def test_invalid_money(self):
        for amount in ("NaN", "Infinity", "-1", "0", "0.001"):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                self.book.recharge("12345", amount, "admin", amount)

    def test_cached_input_not_double_counted(self):
        usage = billing.Usage.parse(USAGE)
        self.assertEqual(PRICE.cost(usage), Decimal(".00014"))
        self.assertEqual(PRICE.charge(PRICE.cost(usage)), 1)
        self.assertEqual(PRICE.charge(Decimal("0.000000001")), 1)

    def test_usage_validation(self):
        for invalid in (
            {},
            dict(USAGE, total_tokens=59),
            dict(USAGE, completion_tokens=True),
        ):
            with self.assertRaises(ValueError):
                billing.Usage.parse(invalid)
