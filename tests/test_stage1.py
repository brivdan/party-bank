"""Stage 1 checks: login for all roles, access control, persistence, ledger integrity.

Run:  python -m unittest discover -s tests -v
"""

import unittest

from helpers import BankTestCase, csrf

from partybank.banking import BankError, ledger_problems, parse_amount


class Stage1Test(BankTestCase):
    # --- logins ---

    def test_user_login(self):
        r = self.card_login(self.client, "demo-user-0001", "1111")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.location.endswith("/me/"))
        page = self.client.get("/me/").get_data(as_text=True)
        self.assertIn("Alice Johnson", page)
        self.assertIn("A$95", page)  # 100 opening - 5 latte

    def test_merchant_login(self):
        r = self.card_login(self.client, "DEMOSHOP0001", "9001")
        self.assertTrue(r.location.endswith("/me/"))  # merged participant dashboard
        page = self.client.get("/me/").get_data(as_text=True)
        self.assertIn("Latte", page)
        self.assertIn("A$5", page)

    def test_admin_login(self):
        r = self.admin_login(self.client)
        self.assertTrue(r.location.endswith("/admin/"))
        self.assertIn("Alice Johnson", self.client.get("/admin/").get_data(as_text=True))

    def test_wrong_pin_and_lockout(self):
        for _ in range(5):
            r = self.card_login(self.client, "DEMOUSER0002", "0000")
            self.assertEqual(r.status_code, 401)
        r = self.card_login(self.client, "DEMOUSER0002", "2222")  # correct PIN, but locked
        self.assertEqual(r.status_code, 401)
        self.assertIn("Too many failed attempts", r.get_data(as_text=True))

    def test_logout(self):
        self.card_login(self.client, "DEMOUSER0001", "1111")
        token = csrf(self.client, "/me/")
        self.client.post("/logout", data={"csrf_token": token})
        self.assertEqual(self.client.get("/me/").status_code, 302)

    def test_post_without_csrf_rejected(self):
        r = self.client.post("/login", data={"card": "DEMOUSER0001", "pin": "1111"})
        self.assertEqual(r.status_code, 400)

    # --- access control ---

    def test_role_restrictions(self):
        self.assertEqual(self.client.get("/admin/").status_code, 302)  # anonymous -> login
        self.card_login(self.client, "DEMOUSER0001", "1111")
        self.assertEqual(self.client.get("/admin/").status_code, 403)
        # Merged roles: every participant can use the terminal and products pages.
        self.assertEqual(self.client.get("/merchant/terminal").status_code, 200)

        m = self.app.test_client()
        self.card_login(m, "DEMOSHOP0001", "9001")
        self.assertEqual(m.get("/me/").status_code, 200)
        self.assertEqual(m.get("/admin/cards").status_code, 403)

    def test_card_url_reveals_nothing(self):
        page = self.client.get("/card/DEMOUSER0001").get_data(as_text=True)
        self.assertIn("DEMO-USER-0001", page)
        self.assertNotIn("Alice", page)
        self.assertNotIn("A$95", page)
        self.assertEqual(self.client.get("/me/").status_code, 302)  # not logged in

    def test_blocked_card_ends_session(self):
        u = self.app.test_client()
        self.card_login(u, "DEMOUSER0003", "3333")
        self.assertEqual(u.get("/me/").status_code, 200)
        self.admin_login(self.client)
        card_id = self.query("SELECT id FROM cards WHERE token = 'DEMOUSER0003'")[0]["id"]
        self.client.post(f"/admin/cards/{card_id}/status", data={"csrf_token": csrf(self.client, "/admin/cards")})
        self.assertEqual(u.get("/me/").status_code, 302)

    # --- admin account creation and persistence ---

    def test_create_account_and_persist_across_restart(self):
        self.admin_login(self.client)
        token = csrf(self.client, "/admin/accounts/new")
        r = self.client.post("/admin/accounts/new", data={
            "csrf_token": token, "name": "Dave", "role": "user", "pin": "4321",
            "initial_balance": "20.4", "card_mode": "existing", "card_token": "NEWTAG123",
        })
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True))

        # "Restart": brand-new app instance on the same database file.
        self.app = self.make_app()
        c = self.app.test_client()
        self.card_login(c, "NEWTAG123", "4321")
        self.assertIn("A$20", c.get("/me/").get_data(as_text=True))

    def test_ledger_balances_match(self):
        with self.db() as conn:
            self.assertEqual(ledger_problems(conn), [])
            self.assertEqual(conn.execute("SELECT SUM(amount_cents) FROM ledger_entries").fetchone()[0], 0)

    def test_parse_amount(self):
        # Whole A$ only: nearest integer (half up), anything between 0 and 1 becomes 1.
        self.assertEqual(parse_amount("12"), 1200)
        self.assertEqual(parse_amount("12.4"), 1200)
        self.assertEqual(parse_amount("12.5"), 1300)
        self.assertEqual(parse_amount("3,6"), 400)
        self.assertEqual(parse_amount("0.3"), 100)
        self.assertEqual(parse_amount("0.01"), 100)
        self.assertEqual(parse_amount("0", allow_zero=True), 0)
        for bad in ("-1", "abc", "0", "nan", "1e99"):
            with self.assertRaises(BankError):
                parse_amount(bad)


if __name__ == "__main__":
    unittest.main()
