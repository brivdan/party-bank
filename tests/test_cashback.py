"""Cashback: managed like the merchant fee (Admin -> Settings, percentage, 0 = off).

Demo (whole A$): Alice 95, Bob 75, Coffee Corner 5 selling Espresso 3 / Latte 5 / Croissant 4.
Runs in no-PIN mode: the customer's card tap pays the seller's open order.
"""

import unittest

from helpers import BankTestCase, csrf

from partybank.banking import (UnauthorizedError, bank_overview, ledger_problems,
                               post_transaction)
from partybank.db import init_db, reserve_account_id


class CashbackTest(BankTestCase):
    REQUIRE_PIN = False

    def setUp(self):
        super().setUp()
        self.admin = self.app.test_client()
        self.admin_login(self.admin)
        self.seller = self.app.test_client()
        self.seller.get("/card/DEMOSHOP0001")

    def settings(self, fee="0", cashback="0"):
        values = {r["key"]: r["value"] for r in self.query("SELECT key, value FROM settings")}
        return self.admin.post("/admin/settings", data={
            "csrf_token": csrf(self.admin, "/admin/settings"), "base_url": values["base_url"],
            "party_name": values["party_name"], "fee_percent": fee, "cashback_percent": cashback})

    def sell(self, product, qty, card="DEMOUSER0001"):
        pid = self.query("SELECT id FROM products WHERE name = ?", (product,))[0]["id"]
        r = self.seller.post("/merchant/orders", data={"csrf_token": csrf(self.seller, "/merchant/terminal"),
                                                       f"qty_{pid}": str(qty)})
        order_id = int(r.location.rsplit("/", 1)[1])
        page = self.seller.get(f"/card/{card}", follow_redirects=True).get_data(as_text=True)
        return order_id, page

    def reserve(self):
        with self.db() as conn:
            return self.query("SELECT balance_cents FROM accounts WHERE id = ?",
                              (reserve_account_id(conn),))[0]["balance_cents"]

    def test_setting_saved_validated_and_audited(self):
        self.settings(cashback="10")
        self.assertEqual(self.query("SELECT value FROM settings WHERE key = 'cashback_bps'")[0]["value"], "1000")
        self.assertIn('name="cashback_percent" value="10"', self.admin.get("/admin/settings").get_data(as_text=True))
        self.assertIn("cashback=10%", self.query("SELECT details FROM audit_logs WHERE action = 'settings.update'")[0]["details"])
        for bad in ("21", "-1", "abc"):
            self.settings(cashback=bad)
            self.assertEqual(self.query("SELECT value FROM settings WHERE key = 'cashback_bps'")[0]["value"], "1000")

    def test_off_by_default(self):
        self.sell("Latte", 2)
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM transactions WHERE kind = 'cashback'")[0]["n"], 0)
        self.assertEqual(self.balance("Alice Johnson"), 85_00)

    def test_cashback_paid_to_customer_from_reserve(self):
        self.settings(cashback="10")
        reserve_before = self.reserve()
        order_id, page = self.sell("Latte", 4)  # 20 -> 10% = 2
        self.assertIn("Payment approved", page)
        self.assertIn("A$2 cashback", page)
        self.assertEqual(self.balance("Alice Johnson"), 95_00 - 20_00 + 2_00)
        self.assertEqual(self.balance("Coffee Corner"), 5_00 + 20_00)  # seller is not charged
        self.assertEqual(self.reserve(), reserve_before - 2_00)
        tx = self.query("SELECT * FROM transactions WHERE kind = 'cashback'")[0]
        self.assertEqual((tx["to_account_id"], tx["amount_cents"]), (self.account("Alice Johnson")["id"], 2_00))
        self.assertEqual(self.query("SELECT cashback_cents FROM orders WHERE id = ?", (order_id,))[0]["cashback_cents"], 2_00)

        alice = self.app.test_client()
        alice.get("/card/DEMOUSER0001")
        home = alice.get("/me/").get_data(as_text=True)
        self.assertRegex(home, r'<span class="pos">A\$2</span>Cashback received')
        self.assertIn("Cashback for order", home)

        with self.db() as conn:
            self.assertEqual(bank_overview(conn)["cashback_cents"], 2_00)
        self.assertIn("Cashback paid", self.admin.get("/admin/").get_data(as_text=True))
        with self.db() as conn:
            self.assertEqual(ledger_problems(conn), [])

    def test_rounding_like_fees(self):
        self.settings(cashback="5")
        self.sell("Espresso", 1)          # 3 -> 0.15 -> minimum A$1
        self.sell("Latte", 5)             # 25 -> 1.25 -> A$1
        self.sell("Latte", 6)             # 30 -> 1.5 -> A$2 (half up)
        amounts = [r["amount_cents"] for r in self.query("SELECT amount_cents FROM transactions "
                                                         "WHERE kind = 'cashback' ORDER BY id")]
        self.assertEqual(amounts, [1_00, 1_00, 2_00])

    def test_fee_and_cashback_together(self):
        self.settings(fee="10", cashback="10")
        self.sell("Latte", 2)  # 10: seller pays 1 fee, customer gets 1 back
        self.assertEqual(self.balance("Alice Johnson"), 95_00 - 10_00 + 1_00)
        self.assertEqual(self.balance("Coffee Corner"), 5_00 + 10_00 - 1_00)
        with self.db() as conn:
            self.assertEqual(bank_overview(conn)["net_earnings_cents"], 0)

    def test_no_double_cashback(self):
        self.settings(cashback="10")
        order_id, _ = self.sell("Latte", 2)
        self.seller.get("/card/DEMOUSER0001", follow_redirects=True)  # second tap
        token = csrf(self.seller, "/merchant/terminal")
        self.seller.post(f"/merchant/orders/{order_id}/pay", data={"csrf_token": token, "card": "DEMOUSER0001"})
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM transactions WHERE kind = 'cashback'")[0]["n"], 1)
        self.assertEqual(self.balance("Alice Johnson"), 86_00)

    def test_declined_payment_gets_no_cashback(self):
        self.settings(cashback="10")
        self.sell("Latte", 30)  # 150 > Alice's 95
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM transactions WHERE kind = 'cashback'")[0]["n"], 0)
        self.assertEqual(self.balance("Alice Johnson"), 95_00)

    def test_transfers_get_no_cashback(self):
        self.settings(cashback="10")
        alice = self.app.test_client()
        alice.get("/card/DEMOUSER0001")
        bob_id = self.account("Bob Smith")["id"]
        alice.post("/me/send", data={"csrf_token": csrf(alice, "/me/send"), "to": str(bob_id), "amount": "20"})
        alice.get("/card/DEMOUSER0001", follow_redirects=True)  # confirm by tapping own card
        self.assertEqual(self.balance("Alice Johnson"), 75_00)
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM transactions WHERE kind = 'cashback'")[0]["n"], 0)

    def test_engine_blocks_cashback_outside_a_purchase(self):
        self.settings(cashback="10")
        shop, alice = self.account("Coffee Corner"), self.account("Alice Johnson")
        with self.db() as conn:
            reserve = reserve_account_id(conn)
            for key in (None, "x" * 24 + "-cashback"):
                with self.assertRaises(UnauthorizedError):
                    post_transaction(conn, kind="cashback", from_id=reserve, to_id=alice["id"],
                                     amount_cents=1_00, actor=shop, idempotency_key=key)
            with self.assertRaises(UnauthorizedError):  # customers can't pay themselves cashback
                post_transaction(conn, kind="cashback", from_id=reserve, to_id=alice["id"],
                                 amount_cents=1_00, actor=alice)

    def test_existing_database_upgrades(self):
        """Re-running init on this database is a no-op, and an orders table
        without the cashback column gets it added."""
        init_db(self.db_path)
        with self.db() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(orders)")}
            self.assertIn("cashback_cents", cols)
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'transactions'").fetchone()[0]
            self.assertIn("'cashback'", sql)


if __name__ == "__main__":
    unittest.main()
