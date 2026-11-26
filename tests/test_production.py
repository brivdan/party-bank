"""Production features: clean start, admin password change, site on/off switch."""

import os
import tempfile
import unittest
from contextlib import closing

from helpers import BankTestCase, csrf

from partybank import create_app
from partybank.db import connect
from partybank.seed import ensure_admin


class ProductionStartTest(unittest.TestCase):
    def test_fresh_database_has_no_demo_data_and_strong_admin(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = os.path.join(tmp, "prod.db")
            create_app({"DATABASE": path, "SECRET_KEY": "x", "TESTING": True})
            username, password = ensure_admin(path)
            self.assertEqual(username, "admin")
            self.assertGreaterEqual(len(password), 24)
            with closing(connect(path)) as conn:
                roles = [r["role"] for r in conn.execute("SELECT role FROM accounts ORDER BY id")]
                self.assertEqual(sorted(roles), ["admin", "system", "system"])  # reserve, cash vault, admin
                self.assertEqual(conn.execute("SELECT COALESCE(SUM(ABS(balance_cents)), 0) FROM accounts").fetchone()[0], 0)
                for table in ("cards", "products", "transactions", "orders"):
                    self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
            self.assertIsNone(ensure_admin(path))  # second run does not create another admin


class ProductionFeaturesTest(BankTestCase):
    REQUIRE_PIN = False

    def setUp(self):
        super().setUp()
        self.admin = self.app.test_client()
        self.admin_login(self.admin)

    def change(self, current, new, repeat=None):
        return self.admin.post("/admin/password", data={"csrf_token": csrf(self.admin, "/admin/password"),
                                                        "current": current, "new": new,
                                                        "repeat": new if repeat is None else repeat})

    def test_admin_changes_password(self):
        for current, new, repeat in (("wrong-pass", "brand-new-pass-1", None), ("admin123", "short", None),
                                     ("admin123", "brand-new-pass-1", "different-pass-1")):
            self.assertEqual(self.change(current, new, repeat).status_code, 400)
        self.assertEqual(self.change("admin123", "brand-new-pass-1").status_code, 302)
        fresh = self.app.test_client()
        self.assertEqual(self.admin_login(fresh, password="admin123").status_code, 401)
        self.assertEqual(self.admin_login(fresh, password="brand-new-pass-1").status_code, 302)
        actions = [r["action"] for r in self.query("SELECT action FROM audit_logs")]
        self.assertIn("admin.password_change", actions)
        self.assertIn("admin.password_change.rejected", actions)

    def test_only_admins_reach_password_and_switch(self):
        alice = self.app.test_client()
        alice.get("/card/DEMOUSER0001")
        self.assertEqual(alice.get("/admin/password").status_code, 403)
        self.assertEqual(alice.post("/admin/site", data={"csrf_token": csrf(alice, "/me/")}).status_code, 403)

    def toggle(self):
        return self.admin.post("/admin/site", data={"csrf_token": csrf(self.admin, "/admin/")})

    def test_site_switch(self):
        alice = self.app.test_client()
        alice.get("/card/DEMOUSER0001")  # logged in before closing
        self.toggle()
        self.assertEqual(self.query("SELECT value FROM settings WHERE key = 'site_open'")[0]["value"], "0")

        for path in ("/me/", "/me/send", "/merchant/terminal"):
            r = alice.get(path)
            self.assertEqual(r.status_code, 503, path)
            self.assertIn("The bank is closed", r.get_data(as_text=True))
        anon = self.app.test_client()
        self.assertEqual(anon.get("/card/DEMOUSER0002").status_code, 503)  # no tap logins
        self.assertEqual(anon.get("/login").status_code, 503)
        self.assertEqual(anon.get("/admin/login").status_code, 200)       # staff can still log in
        self.assertEqual(self.admin.get("/admin/").status_code, 200)
        self.assertIn("The site is CLOSED", self.admin.get("/admin/").get_data(as_text=True))

        self.toggle()
        self.assertEqual(alice.get("/me/").status_code, 200)
        actions = [r["action"] for r in self.query("SELECT action FROM audit_logs WHERE action LIKE 'site.%'")]
        self.assertEqual(actions, ["site.close", "site.open"])

    def test_closed_site_blocks_payments(self):
        seller = self.app.test_client()
        seller.get("/card/DEMOSHOP0001")
        pid = self.query("SELECT id FROM products WHERE name = 'Latte'")[0]["id"]
        seller.post("/merchant/orders", data={"csrf_token": csrf(seller, "/merchant/terminal"), f"qty_{pid}": "1"})
        self.toggle()
        seller.get("/card/DEMOUSER0001", follow_redirects=True)
        self.assertEqual(self.balance("Alice Johnson"), 95_00)


if __name__ == "__main__":
    unittest.main()
