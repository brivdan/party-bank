"""Simplified mode: merged user/merchant roles and REQUIRE_PIN = False.

Demo (whole A$): Alice 95 (DEMOUSER0001), Bob 75 (DEMOUSER0002), Carol 50,
Coffee Corner 5 (DEMOSHOP0001) selling Espresso 3 / Latte 5 / Croissant 4.
"""

import re
import unittest

from helpers import BankTestCase, csrf

from partybank import REQUIRE_PIN
from partybank.banking import ledger_problems


class SimplifiedTest(BankTestCase):
    REQUIRE_PIN = False

    def tap(self, client, token):
        """Simulate an NFC tap / QR scan: the phone opens the card URL. Follows redirects."""
        return client.get(f"/card/{token}", follow_redirects=True)

    def logged_in(self, token):
        c = self.app.test_client()
        self.tap(c, token)
        return c

    def tx_count(self, kind=None):
        sql = "SELECT COUNT(*) AS n FROM transactions" + (" WHERE kind = ?" if kind else "")
        return self.query(sql, (kind,) if kind else ())[0]["n"]

    def prepare_transfer(self, client, to_name, amount, note=""):
        to_id = self.account(to_name)["id"]
        return client.post("/me/send", data={"csrf_token": csrf(client, "/me/send"), "to": str(to_id),
                                             "amount": amount, "note": note})

    def open_order(self, seller, product="Latte", qty=1):
        pid = self.query("SELECT id FROM products WHERE name = ?", (product,))[0]["id"]
        r = seller.post("/merchant/orders", data={"csrf_token": csrf(seller, "/merchant/terminal"),
                                                  f"qty_{pid}": str(qty)})
        return int(r.location.rsplit("/", 1)[1])

    def test_default_flag_is_off(self):
        self.assertFalse(REQUIRE_PIN)

    # --- login ---

    def test_tap_logs_in_immediately(self):
        c = self.app.test_client()
        r = self.tap(c, "DEMOUSER0001")
        self.assertEqual(r.request.path, "/me/")
        page = r.get_data(as_text=True)
        self.assertIn("Alice Johnson", page)
        self.assertIn("A$95", page)

    def test_manual_card_login_without_pin(self):
        c = self.app.test_client()
        self.assertNotIn('name="pin"', c.get("/login").get_data(as_text=True))
        r = c.post("/login", data={"csrf_token": csrf(c, "/login"), "card": "demo-user-0002"})
        self.assertTrue(r.location.endswith("/me/"))

    def test_bad_cards_do_not_log_in(self):
        with self.db() as conn:
            conn.execute("UPDATE cards SET status = 'blocked' WHERE token = 'DEMOUSER0003'")
        for token in ("DEMOUSER0003", "UNKNOWN1234"):
            c = self.app.test_client()
            r = c.get(f"/card/{token}")
            self.assertEqual(r.status_code, 401, token)
            self.assertEqual(c.get("/me/").status_code, 302)

    def test_former_merchant_gets_same_dashboard_and_nav(self):
        m = self.logged_in("DEMOSHOP0001")
        page = m.get("/me/").get_data(as_text=True)
        for text in ("Coffee Corner", "Total earned", "Total spent", "Send money",
                     'href="/merchant/terminal"', 'href="/merchant/products"', 'href="/me/history"'):
            self.assertIn(text, page)
        self.assertEqual(m.get("/merchant/").location, "/me/")

    # --- transfers ---

    def test_recipient_dropdown(self):
        with self.db() as conn:
            conn.execute("UPDATE accounts SET status = 'frozen' WHERE name = 'Carol White'")
        alice = self.logged_in("DEMOUSER0001")
        options = re.findall(r'<option value="\d+"[^>]*>([^<]+)</option>', alice.get("/me/send").get_data(as_text=True))
        self.assertEqual(options, ["Bob Smith", "Coffee Corner", "Snack Stand"])  # no self/frozen/admin/bank

    def test_transfer_confirmed_by_scanning_own_card(self):
        alice = self.logged_in("DEMOUSER0001")
        page = self.prepare_transfer(alice, "Bob Smith", "12.4", "Drinks").get_data(as_text=True)  # rounds to A$12
        self.assertIn("Tap your own card", page)
        self.assertNotIn('name="pin"', page)
        self.assertEqual(self.balance("Alice Johnson"), 95_00)  # nothing moves before the scan

        r = self.tap(alice, "DEMOUSER0001")
        self.assertEqual(r.request.path, "/me/")
        self.assertIn("Sent A$12 to Bob Smith", r.get_data(as_text=True))
        self.assertEqual((self.balance("Alice Johnson"), self.balance("Bob Smith")), (83_00, 87_00))

        # Tapping again does not send a second time.
        r = self.tap(alice, "DEMOUSER0001")
        self.assertEqual(self.balance("Alice Johnson"), 83_00)
        self.assertEqual(self.tx_count("transfer"), 1)

    def test_transfer_rejects_someone_elses_card(self):
        alice = self.logged_in("DEMOUSER0001")
        self.prepare_transfer(alice, "Bob Smith", "5")
        r = self.tap(alice, "DEMOUSER0002")  # Bob's card on Alice's phone
        self.assertIn("does not belong to this account", r.get_data(as_text=True))
        self.assertEqual(self.balance("Alice Johnson"), 95_00)
        self.tap(alice, "DEMOUSER0001")  # still pending; own card confirms
        self.assertEqual(self.balance("Alice Johnson"), 90_00)

    def test_transfer_confirmed_by_typed_card_id(self):
        alice = self.logged_in("DEMOUSER0001")
        html = self.prepare_transfer(alice, "Bob Smith", "1").get_data(as_text=True)
        fields = dict(re.findall(r'type="hidden" name="(\w+)" value="([^"]*)"', html))
        r = alice.post("/me/send/confirm", data={**fields, "card": "DEMOUSER0002"})
        self.assertEqual(r.status_code, 401)
        alice.post("/me/send/confirm", data={**fields, "card": "DEMO-USER-0001"})
        alice.post("/me/send/confirm", data={**fields, "card": "DEMO-USER-0001"})  # double submit
        self.assertEqual(self.balance("Alice Johnson"), 94_00)
        self.assertEqual(self.balance("Bob Smith"), 76_00)

    def test_transfer_insufficient_funds(self):
        alice = self.logged_in("DEMOUSER0001")
        r = self.prepare_transfer(alice, "Bob Smith", "95.51")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Insufficient funds", r.get_data(as_text=True))
        self.assertEqual(self.tx_count("transfer"), 0)

    # --- payments ---

    def test_customer_tap_pays_and_seller_stays_logged_in(self):
        seller = self.logged_in("DEMOSHOP0001")
        order_id = self.open_order(seller, "Latte", 2)
        r = self.tap(seller, "DEMOUSER0001")  # Alice taps her card on the seller's phone
        page = r.get_data(as_text=True)
        self.assertEqual(r.request.path, f"/merchant/orders/{order_id}")
        self.assertIn("Payment approved", page)
        self.assertIn("Alice Johnson", page)
        self.assertEqual(self.balance("Alice Johnson"), 85_00)
        self.assertEqual(self.balance("Coffee Corner"), 15_00)
        self.assertIn("Coffee Corner", seller.get("/me/").get_data(as_text=True))  # seller session kept

        # Second tap: the order is already paid, nothing is charged again.
        self.tap(seller, "DEMOUSER0001")
        self.assertEqual(self.balance("Alice Johnson"), 85_00)
        self.assertEqual(self.tx_count("purchase"), 2)  # demo latte + this one

        # Both histories show it.
        alice = self.logged_in("DEMOUSER0001")
        self.assertIn("2× Latte", alice.get("/me/history?dir=out").get_data(as_text=True))
        self.assertIn("2× Latte", seller.get("/me/history?dir=in").get_data(as_text=True))

    def test_payment_failures(self):
        seller = self.logged_in("DEMOSHOP0001")
        order_id = self.open_order(seller, "Latte", 30)  # 150 > Alice's 95
        r = self.tap(seller, "DEMOUSER0001")
        self.assertIn("Insufficient funds", r.get_data(as_text=True))
        r = self.tap(seller, "DEMOSHOP0001")  # seller's own card
        self.assertIn("can&#39;t pay your own order", r.get_data(as_text=True))
        with self.db() as conn:
            conn.execute("UPDATE cards SET status = 'blocked' WHERE token = 'DEMOUSER0002'")
        r = self.tap(seller, "DEMOUSER0002")
        self.assertIn("blocked", r.get_data(as_text=True))
        self.assertEqual(self.query("SELECT status FROM orders WHERE id = ?", (order_id,))[0]["status"], "open")
        self.assertEqual(self.balance("Alice Johnson"), 95_00)

    def test_manual_card_entry_payment(self):
        seller = self.logged_in("DEMOSHOP0001")
        order_id = self.open_order(seller, "Espresso")
        page = seller.get(f"/merchant/orders/{order_id}/pay?card=DEMOUSER0002").get_data(as_text=True)
        self.assertNotIn('name="pin"', page)
        token = csrf(seller, "/merchant/terminal")
        seller.post(f"/merchant/orders/{order_id}/pay", data={"csrf_token": token, "card": "DEMOUSER0002"})
        seller.post(f"/merchant/orders/{order_id}/pay", data={"csrf_token": token, "card": "DEMOUSER0002"})
        self.assertEqual(self.balance("Bob Smith"), 72_00)

    def test_former_user_can_sell_products(self):
        alice = self.logged_in("DEMOUSER0001")
        token = csrf(alice, "/merchant/products")
        alice.post("/merchant/products", data={"csrf_token": token, "name": "Lemonade", "price": "2.00"})
        pid = self.query("SELECT id FROM products WHERE name = 'Lemonade'")[0]["id"]
        alice.post(f"/merchant/products/{pid}/edit",
                   data={"csrf_token": token, "name": "Lemonade", "price": "2.50", "active": "1"})
        order_id = self.open_order(alice, "Lemonade", 2)
        self.tap(alice, "DEMOUSER0002")  # Bob pays on Alice's phone
        self.assertEqual((self.balance("Alice Johnson"), self.balance("Bob Smith")), (101_00, 69_00))
        self.assertEqual(self.query("SELECT status FROM orders WHERE id = ?", (order_id,))[0]["status"], "paid")

    # --- admin ---

    def test_admin_login_still_needs_password(self):
        a = self.app.test_client()
        self.assertEqual(self.admin_login(a, password="wrong").status_code, 401)
        self.assertEqual(a.get("/admin/").status_code, 302)
        self.admin_login(a)
        self.assertEqual(a.get("/admin/").status_code, 200)
        alice = self.logged_in("DEMOUSER0001")
        self.assertEqual(alice.get("/admin/money").status_code, 403)

    def test_admin_charge_with_card_and_new_account_without_pin(self):
        a = self.app.test_client()
        self.admin_login(a)
        html = a.get("/admin/money").get_data(as_text=True)
        key = re.search(r'/admin/money/charge".*?name="key" value="([^"]+)"', html, re.S).group(1)
        data = {"csrf_token": csrf(a, "/admin/money"), "key": key, "account_id": self.account("Bob Smith")["id"],
                "amount": "1", "reason": "Lost card", "card": "DEMOUSER0001"}
        a.post("/admin/money/charge", data=data)  # wrong card
        self.assertEqual(self.balance("Bob Smith"), 75_00)
        a.post("/admin/money/charge", data={**data, "card": "DEMOUSER0002"})
        self.assertEqual(self.balance("Bob Smith"), 74_00)

        r = a.post("/admin/accounts/new", data={"csrf_token": csrf(a, "/admin/accounts/new"), "name": "Dave",
                                                "initial_balance": "10", "card_mode": "existing",
                                                "card_token": "DAVECARD01"})
        self.assertEqual(r.status_code, 302)
        dave = self.logged_in("DAVECARD01")
        self.assertIn("A$10", dave.get("/me/").get_data(as_text=True))
        with self.db() as conn:
            self.assertEqual(ledger_problems(conn), [])


if __name__ == "__main__":
    unittest.main()
