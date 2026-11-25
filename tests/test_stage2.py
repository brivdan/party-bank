"""Stage 2 checks: transfers, balances, insufficient funds, duplicates,
authorization, atomicity and ledger immutability.

Demo starting balances: Alice 95.50 (after a 4.50 latte), Bob 75.00, Carol 50.00.
"""

import re
import sqlite3
import threading
import unittest

from helpers import BankTestCase, csrf

from partybank.banking import (SYSTEM_ACTOR, BankError, UnauthorizedError, ledger_problems,
                               new_idempotency_key, post_transaction)
from partybank.db import connect, transaction


class Stage2Test(BankTestCase):
    def setUp(self):
        super().setUp()
        self.alice = self.account("Alice Johnson")
        self.bob = self.account("Bob Smith")
        self.card_login(self.client, "DEMOUSER0001", "1111")  # client = Alice

    def tx_count(self):
        return self.query("SELECT COUNT(*) AS n FROM transactions")[0]["n"]

    def start_send(self, client, to, amount, note=""):
        """Step 1. Returns (response, hidden fields of the confirm form)."""
        r = client.post("/me/send", data={"csrf_token": csrf(client, "/me/send"),
                                          "to": to, "amount": amount, "note": note})
        html = r.get_data(as_text=True)
        fields = dict(re.findall(r'type="hidden" name="(\w+)" value="([^"]*)"', html))
        return r, fields

    def confirm(self, client, fields, pin="1111"):
        return client.post("/me/send/confirm", data={**fields, "pin": pin})

    # --- completion criteria ---

    def test_two_users_transfer_and_balances_update(self):
        r, fields = self.start_send(self.client, f"#{self.bob['id']:04d}", "10.25", "Pizza")  # rounds to A$10
        self.assertEqual(r.status_code, 200)
        self.assertIn("Bob Smith", r.get_data(as_text=True))
        self.assertEqual(self.balance("Alice Johnson"), 95_00)  # nothing moves before confirm

        r = self.confirm(self.client, fields)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.balance("Alice Johnson"), 85_00)
        self.assertEqual(self.balance("Bob Smith"), 85_00)

        page = self.client.get("/me/").get_data(as_text=True)  # refreshed balance + flash
        self.assertIn("Sent A$10 to Bob Smith", page)
        self.assertIn("A$85", page)

        # Recorded: id, timestamp, sender, recipient, amount, type; balanced ledger.
        tx = self.query("SELECT * FROM transactions ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual((tx["kind"], tx["from_account_id"], tx["to_account_id"], tx["amount_cents"],
                          tx["created_by"], tx["description"]),
                         ("transfer", self.alice["id"], self.bob["id"], 10_00, self.alice["id"], "Pizza"))
        self.assertTrue(tx["created_at"])
        entries = self.query("SELECT account_id, amount_cents FROM ledger_entries WHERE transaction_id = ?",
                             (tx["id"],))
        self.assertEqual(sorted((e["account_id"], e["amount_cents"]) for e in entries),
                         sorted([(self.alice["id"], -10_00), (self.bob["id"], 10_00)]))
        with self.db() as conn:
            self.assertEqual(ledger_problems(conn), [])

    def test_incoming_and_outgoing_history(self):
        _, fields = self.start_send(self.client, str(self.bob["id"]), "5", "Thanks")
        self.confirm(self.client, fields)

        out_page = self.client.get("/me/history?dir=out").get_data(as_text=True)
        self.assertIn("Bob Smith", out_page)
        self.assertIn("-A$5", out_page)

        bob = self.app.test_client()
        self.card_login(bob, "DEMOUSER0002", "2222")
        in_page = bob.get("/me/history?dir=in").get_data(as_text=True)
        self.assertIn("Alice Johnson", in_page)
        self.assertIn("+A$5", in_page)
        self.assertIn("Thanks", in_page)
        self.assertNotIn("Alice Johnson", bob.get("/me/history?dir=out").get_data(as_text=True))
        self.assertEqual(bob.get("/me/balance.json").get_json()["balance_cents"], 80_00)

    def test_insufficient_funds_rejected(self):
        before = self.tx_count()
        r, _ = self.start_send(self.client, str(self.bob["id"]), "95.51")  # rounds to 96 > 95
        self.assertEqual(r.status_code, 400)
        self.assertIn("Insufficient funds", r.get_data(as_text=True))

        # Engine rejects too (e.g. balance changed between form and confirm).
        with self.db() as conn, self.assertRaises(BankError):
            post_transaction(conn, kind="transfer", from_id=self.alice["id"], to_id=self.bob["id"],
                             amount_cents=96_00, actor=self.alice)
        self.assertEqual(self.tx_count(), before)
        self.assertEqual(self.balance("Alice Johnson"), 95_00)

    def test_insufficient_funds_at_confirm(self):
        _, fields = self.start_send(self.client, str(self.bob["id"]), "90")
        with self.db() as conn:  # balance drops before Alice confirms
            post_transaction(conn, kind="transfer", from_id=self.alice["id"], to_id=self.bob["id"],
                             amount_cents=10_00, actor=self.alice)
        r = self.confirm(self.client, fields)
        self.assertEqual(r.status_code, 400)
        self.assertIn("Insufficient funds", r.get_data(as_text=True))
        self.assertEqual(self.balance("Alice Johnson"), 85_00)
        self.assertTrue(self.query("SELECT 1 FROM audit_logs WHERE action = 'transfer.rejected'"))

    # --- validation ---

    def test_invalid_amounts_rejected(self):
        for bad in ("-5", "0", "abc", ""):
            r, _ = self.start_send(self.client, str(self.bob["id"]), bad)
            self.assertEqual(r.status_code, 400, bad)
        for bad in (-100, 0, 1.5, True, "100", None, 150):  # 150 cents = A$1.50
            with self.db() as conn, self.assertRaises(BankError, msg=repr(bad)):
                post_transaction(conn, kind="transfer", from_id=self.alice["id"], to_id=self.bob["id"],
                                 amount_cents=bad, actor=self.alice)

    def test_tampered_negative_amount_at_confirm(self):
        _, fields = self.start_send(self.client, str(self.bob["id"]), "1")
        before = self.tx_count()
        fields["amount_cents"] = "-5000"
        r = self.confirm(self.client, fields)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.tx_count(), before)
        self.assertEqual(self.balance("Alice Johnson"), 95_00)

    def test_bad_recipients(self):
        for to, msg in ((str(self.alice["id"]), "your own account"), ("9999", "No active account"),
                        ("1", "No active account")):  # 1 = reserve/admin, not payable
            r, _ = self.start_send(self.client, to, "1")
            self.assertEqual(r.status_code, 400)
            self.assertIn(msg, r.get_data(as_text=True))

    # --- duplicates ---

    def test_duplicate_submit_pays_once(self):
        _, fields = self.start_send(self.client, str(self.bob["id"]), "7")
        self.confirm(self.client, fields)
        r = self.confirm(self.client, fields)  # double click / back + resubmit
        self.assertEqual(r.status_code, 302)
        self.assertIn("already sent", self.client.get("/me/").get_data(as_text=True))
        self.assertEqual(self.balance("Alice Johnson"), 88_00)
        self.assertEqual(self.balance("Bob Smith"), 82_00)

    def test_reused_key_with_different_amount_rejected(self):
        key = new_idempotency_key()
        with self.db() as conn:
            post_transaction(conn, kind="transfer", from_id=self.alice["id"], to_id=self.bob["id"],
                             amount_cents=100, actor=self.alice, idempotency_key=key)
            with self.assertRaises(BankError):
                post_transaction(conn, kind="transfer", from_id=self.alice["id"], to_id=self.bob["id"],
                                 amount_cents=200, actor=self.alice, idempotency_key=key)
        self.assertEqual(self.balance("Alice Johnson"), 94_00)

    def test_concurrent_double_spend(self):
        """Two simultaneous payments of the full balance: exactly one succeeds."""
        results = []

        def pay():
            conn = connect(self.db_path)
            try:
                post_transaction(conn, kind="transfer", from_id=self.alice["id"], to_id=self.bob["id"],
                                 amount_cents=95_00, actor=self.alice)
                results.append("ok")
            except BankError:
                results.append("rejected")
            finally:
                conn.close()

        threads = [threading.Thread(target=pay) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), ["ok", "rejected", "rejected", "rejected"])
        self.assertEqual(self.balance("Alice Johnson"), 0)

    # --- authorization ---

    def test_wrong_pin_at_confirm(self):
        _, fields = self.start_send(self.client, str(self.bob["id"]), "3")
        before = self.tx_count()
        r = self.confirm(self.client, fields, pin="0000")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.tx_count(), before)

    def test_engine_blocks_unauthorized(self):
        with self.db() as conn:
            with self.assertRaises(UnauthorizedError):  # Bob spending Alice's money
                post_transaction(conn, kind="transfer", from_id=self.alice["id"], to_id=self.bob["id"],
                                 amount_cents=100, actor=self.bob)
            with self.assertRaises(UnauthorizedError):  # users cannot mint money
                post_transaction(conn, kind="issue", from_id=1, to_id=self.alice["id"],
                                 amount_cents=100, actor=self.alice)
            with self.assertRaises(RuntimeError):  # actor is mandatory
                post_transaction(conn, kind="transfer", from_id=self.alice["id"], to_id=self.bob["id"],
                                 amount_cents=100, actor=None)

    def test_frozen_accounts_cannot_transact(self):
        with self.db() as conn:
            conn.execute("UPDATE accounts SET status = 'frozen' WHERE id = ?", (self.bob["id"],))
            with self.assertRaises(BankError):
                post_transaction(conn, kind="transfer", from_id=self.alice["id"], to_id=self.bob["id"],
                                 amount_cents=100, actor=self.alice)

    def test_anonymous_cannot_use_send(self):
        m = self.app.test_client()
        self.card_login(m, "DEMOSHOP0001", "9001")
        self.assertEqual(m.get("/me/send").status_code, 200)  # merged roles: merchants can send too
        anon = self.app.test_client()
        self.assertEqual(anon.get("/me/send").status_code, 302)

    # --- atomicity and immutability ---

    def test_failed_batch_rolls_back_completely(self):
        before = self.tx_count()
        with self.db() as conn:
            with self.assertRaises(ZeroDivisionError):
                with transaction(conn):
                    post_transaction(conn, kind="transfer", from_id=self.alice["id"], to_id=self.bob["id"],
                                     amount_cents=100, actor=self.alice)
                    1 / 0
        self.assertEqual(self.tx_count(), before)
        self.assertEqual(self.balance("Alice Johnson"), 95_00)
        self.assertEqual(self.balance("Bob Smith"), 75_00)

    def test_ledger_and_transactions_are_immutable(self):
        with self.db() as conn:
            for sql in ("UPDATE ledger_entries SET amount_cents = 0",
                        "DELETE FROM ledger_entries",
                        "UPDATE transactions SET amount_cents = 1",
                        "DELETE FROM transactions"):
                with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                    conn.execute(sql)

    def test_admin_sees_all_transactions(self):
        _, fields = self.start_send(self.client, str(self.bob["id"]), "2", "Taxi")
        self.confirm(self.client, fields)
        admin = self.app.test_client()
        self.admin_login(admin)
        page = admin.get("/admin/transactions").get_data(as_text=True)
        self.assertIn("Taxi", page)
        self.assertIn("Ledger check: every transaction is balanced", page)


if __name__ == "__main__":
    unittest.main()
