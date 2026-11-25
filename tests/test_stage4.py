"""Stage 4 checks: bank desk actions (cash, transfers, charges, credits/debits),
dashboard figures, traceability and the ban on direct balance overwrites.

Demo data: opening balances are cash deposits (Alice 100, Bob 75, Carol 50),
so the vault holds A$225. Alice then bought a A$5 latte -> Alice 95.
"""

import sqlite3
import unittest

from helpers import BankTestCase, csrf

from partybank.banking import (UnauthorizedError, bank_overview, ledger_problems,
                               post_transaction)
from partybank.db import cash_account_id, reserve_account_id


class Stage4Test(BankTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.app.test_client()
        self.admin_login(self.admin)
        self.alice = self.account("Alice Johnson")
        self.bob = self.account("Bob Smith")
        with self.db() as conn:
            self.reserve_id = reserve_account_id(conn)
            self.cash_id = cash_account_id(conn)

    def overview(self):
        with self.db() as conn:
            return bank_overview(conn)

    def desk(self, action, account, amount, reason="", **extra):
        page = self.admin.get(f"/admin/money?account={account['id'] if isinstance(account, sqlite3.Row) else account}")
        html = page.get_data(as_text=True)
        token = csrf(self.admin, "/admin/money")
        import re
        key = re.search(rf'/admin/money/{action if action != "debit" else "credit"}".*?name="key" value="([^"]+)"',
                        html, re.S).group(1)
        account_id = account["id"] if isinstance(account, sqlite3.Row) else account
        data = {"csrf_token": token, "key": key, "account_id": account_id, "amount": amount, "reason": reason, **extra}
        if action in ("credit", "debit"):
            data["direction"] = action
            action = "credit"
        return self.admin.post(f"/admin/money/{action}", data=data), key, data, action

    def last_tx(self):
        return self.query("SELECT * FROM transactions ORDER BY id DESC LIMIT 1")[0]

    def audit_rows(self, action):
        return self.query("SELECT * FROM audit_logs WHERE action = ?", (action,))

    # --- setup ---

    def test_bank_accounts_exist_and_are_viewable(self):
        self.assertEqual(self.overview()["cash_cents"], 225_00)
        for acc_id in (self.reserve_id, self.cash_id):
            self.assertEqual(self.admin.get(f"/admin/accounts/{acc_id}").status_code, 200)
        page = self.admin.get("/admin/").get_data(as_text=True)
        self.assertIn("Party Bank Reserve", page)
        self.assertIn("Physical cash reserves", page)

    # --- deposits and withdrawals ---

    def test_deposit_updates_balance_and_cash_not_earnings(self):
        before = self.overview()
        r, *_ = self.desk("deposit", self.bob, "20")
        self.assertEqual(r.status_code, 302)
        after = self.overview()
        self.assertEqual(self.balance("Bob Smith"), 95_00)
        self.assertEqual(after["cash_cents"], before["cash_cents"] + 20_00)
        self.assertEqual(after["digital_cents"], before["digital_cents"] + 20_00)
        self.assertEqual(after["earnings_cents"], before["earnings_cents"])   # not profit
        self.assertEqual(after["reserve_cents"], before["reserve_cents"])     # not bank money
        tx = self.last_tx()
        self.assertEqual((tx["kind"], tx["from_account_id"], tx["to_account_id"], tx["amount_cents"]),
                         ("deposit", self.cash_id, self.bob["id"], 20_00))
        self.assertEqual(tx["created_by"], self.account("Administrator")["id"])
        self.assertIn(f"ref #{tx['id']}", self.audit_rows("money.deposit")[0]["details"])

    def test_withdrawal_updates_balance_and_cash(self):
        r, *_ = self.desk("withdrawal", self.bob, "30", "Going home")
        self.assertEqual(self.balance("Bob Smith"), 45_00)
        self.assertEqual(self.overview()["cash_cents"], 195_00)
        self.assertEqual(self.last_tx()["kind"], "withdrawal")
        self.assertTrue(self.audit_rows("money.withdrawal"))

    def test_withdrawal_limits(self):
        self.desk("withdrawal", self.bob, "76")  # more than Bob has
        self.assertEqual(self.balance("Bob Smith"), 75_00)
        # Bob gets a big credit, but the vault only holds 225.00 of physical cash.
        self.desk("credit", self.bob, "500", "Prize money")
        r, *_ = self.desk("withdrawal", self.bob, "300")
        self.assertIn("Not enough physical cash", self.admin.get(r.location).get_data(as_text=True))
        self.assertEqual(self.balance("Bob Smith"), 575_00)
        self.assertEqual(self.overview()["cash_cents"], 225_00)
        self.assertTrue(self.audit_rows("money.withdrawal.rejected"))

    def test_bank_float_deposit_to_reserve(self):
        before = self.overview()
        self.desk("deposit", self.reserve_id, "50", "Opening float")
        after = self.overview()
        self.assertEqual(after["cash_cents"], before["cash_cents"] + 50_00)
        self.assertEqual(after["reserve_cents"], before["reserve_cents"] + 50_00)
        self.assertEqual(after["earnings_cents"], before["earnings_cents"])

    # --- transfers, charges, adjustments ---

    def test_admin_transfer_requires_reason(self):
        self.desk("transfer", self.alice, "5", "", to_account_id=self.bob["id"])
        self.assertEqual(self.balance("Alice Johnson"), 95_00)
        self.desk("transfer", self.alice, "5", "Paid for Bob's ticket", to_account_id=self.bob["id"])
        self.assertEqual((self.balance("Alice Johnson"), self.balance("Bob Smith")), (90_00, 80_00))
        self.assertEqual(self.last_tx()["description"], "Paid for Bob's ticket")

    def test_charge_needs_pin_and_counts_as_earnings(self):
        before = self.overview()
        self.desk("charge", self.alice, "2", "Card replacement", pin="0000")
        self.assertEqual(self.balance("Alice Johnson"), 95_00)
        self.desk("charge", self.alice, "2", "Card replacement", pin="1111")
        after = self.overview()
        self.assertEqual(self.balance("Alice Johnson"), 93_00)
        self.assertEqual(after["earnings_cents"], before["earnings_cents"] + 2_00)
        self.assertEqual(after["reserve_cents"], before["reserve_cents"] + 2_00)
        tx = self.last_tx()
        self.assertEqual((tx["kind"], tx["to_account_id"]), ("charge", self.reserve_id))

    def test_credit_and_debit_need_reason_and_are_traceable(self):
        before = self.overview()
        self.desk("credit", self.alice, "10", "")           # rejected: no reason
        self.assertEqual(self.balance("Alice Johnson"), 95_00)
        self.desk("credit", self.alice, "10", "Lost-and-found refund")
        self.desk("debit", self.alice, "4", "Duplicate credit")
        self.assertEqual(self.balance("Alice Johnson"), 101_00)
        after = self.overview()
        self.assertEqual(after["reserve_cents"], before["reserve_cents"] - 6_00)
        self.assertEqual(after["earnings_cents"], before["earnings_cents"])  # corrections aren't profit

        txs = self.query("SELECT * FROM transactions WHERE kind = 'adjustment' ORDER BY id")
        self.assertEqual([t["description"] for t in txs],
                         ["Credit: Lost-and-found refund", "Debit: Duplicate credit"])
        self.assertEqual([(t["from_account_id"], t["to_account_id"]) for t in txs],
                         [(self.reserve_id, self.alice["id"]), (self.alice["id"], self.reserve_id)])
        self.assertIn("Lost-and-found refund", self.audit_rows("money.credit")[0]["details"])
        self.assertIn("Duplicate credit", self.audit_rows("money.debit")[0]["details"])
        self.assertIn("Lost-and-found refund", self.admin.get("/admin/transactions").get_data(as_text=True))

    def test_duplicate_submit_posts_once(self):
        r, key, data, action = self.desk("deposit", self.bob, "10")
        self.admin.post(f"/admin/money/{action}", data=data)
        self.assertEqual(self.balance("Bob Smith"), 85_00)

    def test_frozen_account_rejected(self):
        with self.db() as conn:
            conn.execute("UPDATE accounts SET status = 'frozen' WHERE id = ?", (self.bob["id"],))
        self.desk("deposit", self.bob, "10")
        self.assertEqual(self.balance("Bob Smith"), 75_00)

    # --- access control and integrity ---

    def test_only_admins_use_the_desk(self):
        u = self.app.test_client()
        self.card_login(u, "DEMOUSER0001", "1111")
        r = u.post("/admin/money/deposit", data={"csrf_token": csrf(u, "/me/"), "account_id": self.alice["id"],
                                                 "amount": "1000"})
        self.assertEqual(r.status_code, 403)
        with self.db() as conn:
            for kind, src, dst in (("deposit", self.cash_id, self.alice["id"]),
                                   ("adjustment", self.reserve_id, self.alice["id"]),
                                   ("charge", self.bob["id"], self.reserve_id)):
                with self.assertRaises(UnauthorizedError):
                    post_transaction(conn, kind=kind, from_id=src, to_id=dst, amount_cents=100,
                                     actor=self.alice, description="sneaky")

    def test_no_direct_balance_overwrites(self):
        with self.db() as conn:
            for sql, params in (
                ("UPDATE accounts SET balance_cents = 999999 WHERE id = ?", (self.alice["id"],)),
                ("INSERT INTO accounts (name, role, balance_cents) VALUES ('Rich', 'user', 1000)", ()),
                ("INSERT INTO ledger_entries (transaction_id, account_id, amount_cents, balance_after_cents) "
                 "VALUES (1, ?, 100, 1)", (self.alice["id"],)),
            ):
                with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                    conn.execute(sql, params)
        self.assertEqual(self.balance("Alice Johnson"), 95_00)

    def test_dashboard_figures(self):
        self.set_fee_and_sell()
        self.desk("deposit", self.bob, "20")
        stats = self.overview()
        self.assertEqual(stats["accounts"], 5)
        self.assertEqual(stats["digital_cents"],
                         sum(self.balance(n) for n in ("Alice Johnson", "Bob Smith", "Carol White",
                                                       "Coffee Corner", "Snack Stand")))
        self.assertEqual(stats["earnings_cents"], 1_00)               # 2.5% of 10 = 0.25 -> minimum A$1
        self.assertEqual(stats["volume_cents"], 5_00 + 10_00)          # purchases + transfers
        self.assertEqual(stats["cash_cents"], stats["digital_cents"] + stats["reserve_cents"])
        page = self.admin.get("/admin/").get_data(as_text=True)
        self.assertIn("Ranking by balance", page)
        ranking = re.findall(r'<td class="muted">(\d+)\.</td>\s*<td><a [^>]+>([^<]+)</a>', page)
        self.assertEqual(ranking[0], ("1", "Bob Smith"))  # 95.00 after the deposit
        with self.db() as conn:
            self.assertEqual(ledger_problems(conn), [])

    def set_fee_and_sell(self):
        values = {r["key"]: r["value"] for r in self.query("SELECT key, value FROM settings")}
        self.admin.post("/admin/settings", data={"csrf_token": csrf(self.admin, "/admin/settings"),
                                                 "base_url": values["base_url"],
                                                 "party_name": values["party_name"], "fee_percent": "2.5"})
        m = self.app.test_client()
        self.card_login(m, "DEMOSHOP0001", "9001")
        latte = self.query("SELECT id FROM products WHERE name = 'Latte'")[0]["id"]
        r = m.post("/merchant/orders", data={"csrf_token": csrf(m, "/merchant/terminal"), f"qty_{latte}": "2"})
        order_id = r.location.rsplit("/", 1)[1]
        m.post(f"/merchant/orders/{order_id}/pay",
               data={"csrf_token": csrf(m, "/merchant/terminal"), "card": "DEMOUSER0001", "pin": "1111"})


class MigrationTest(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = os.path.join(self.tmp.name, "old.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_upgrade_from_stage3_database(self):
        """A Stage 3 database (quoted table name after its earlier rebuild, no
        Stage 4 kinds, no system_key, no Cash Vault, reserve-issued money)
        upgrades in place without losing rows."""
        from contextlib import closing

        from partybank.banking import SYSTEM_ACTOR, create_account
        from partybank.db import connect, init_db, transaction

        init_db(self.db_path)
        with closing(connect(self.db_path)) as conn:
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'transactions'").fetchone()[0]
            old = (sql.replace(", 'deposit', 'withdrawal', 'charge'", "")
                      .replace("CREATE TABLE transactions", "CREATE TABLE transactions_old", 1))
            conn.execute("PRAGMA foreign_keys = OFF")
            with transaction(conn):
                for t in ("trg_tx_immutable", "trg_tx_no_delete"):
                    conn.execute(f"DROP TRIGGER {t}")
                conn.execute(old)
                conn.execute("DROP TABLE transactions")
                conn.execute("ALTER TABLE transactions_old RENAME TO transactions")
                conn.execute("DELETE FROM accounts WHERE system_key = 'cash'")
                conn.execute("UPDATE accounts SET system_key = NULL")
            conn.execute("PRAGMA foreign_keys = ON")
            self.assertTrue(conn.execute("SELECT sql FROM sqlite_master WHERE name = 'transactions'")
                            .fetchone()[0].startswith('CREATE TABLE "transactions"'))
            # Stage 1-3 style money: issued by the reserve.
            reserve = conn.execute("SELECT id FROM accounts WHERE role = 'system'").fetchone()[0]
            with transaction(conn):
                user = create_account(conn, name="Old User", role="user", pin="1234", actor=SYSTEM_ACTOR)
                post_transaction(conn, kind="issue", from_id=reserve, to_id=user, amount_cents=1000,
                                 actor=SYSTEM_ACTOR)
            tx_before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

        init_db(self.db_path)
        with closing(connect(self.db_path)) as conn:
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'transactions'").fetchone()[0]
            self.assertIn("'charge'", sql)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], tx_before)
            keys = {r[0] for r in conn.execute("SELECT system_key FROM accounts WHERE role = 'system'")}
            self.assertEqual(keys, {"reserve", "cash"})
            self.assertEqual(ledger_problems(conn), [])
        conn = connect(self.db_path)
        try:  # triggers were re-created
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("DELETE FROM transactions")
        finally:
            conn.close()


import re  # noqa: E402  (used by the ranking assertion)

if __name__ == "__main__":
    unittest.main()
