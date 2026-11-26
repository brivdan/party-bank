"""Stage 3 checks: products, terminal orders, customer authorization, fees,
merchant statistics and both-sided history.

Demo (whole A$): Alice 95 (card DEMOUSER0001, PIN 1111), Coffee Corner 5
(card DEMOSHOP0001, PIN 9001) with Espresso 3, Latte 5, Croissant 4.
"""

import re
import unittest

from helpers import BankTestCase, csrf

from partybank.banking import (BankError, UnauthorizedError, compute_fee, ledger_problems,
                               parse_fee_percent, post_transaction)


class Stage3Test(BankTestCase):
    def setUp(self):
        super().setUp()
        self.shop = self.account("Coffee Corner")
        self.alice = self.account("Alice Johnson")
        self.m = self.app.test_client()
        self.card_login(self.m, "DEMOSHOP0001", "9001")

    def product_id(self, name):
        return self.query("SELECT id FROM products WHERE name = ?", (name,))[0]["id"]

    def create_order(self, quantities=None, custom="", label=""):
        data = {"csrf_token": csrf(self.m, "/merchant/terminal"), "custom_amount": custom, "custom_label": label}
        for name, qty in (quantities or {}).items():
            data[f"qty_{self.product_id(name)}"] = str(qty)
        r = self.m.post("/merchant/orders", data=data)
        match = re.search(r"/merchant/orders/(\d+)$", r.location or "")
        return (int(match.group(1)) if match else None), r

    def pay(self, order_id, card="DEMO-USER-0001", pin="1111"):
        token = csrf(self.m, f"/merchant/orders/{order_id}")
        return self.m.post(f"/merchant/orders/{order_id}/pay", data={"csrf_token": token, "card": card, "pin": pin})

    def set_fee(self, percent):
        admin = self.app.test_client()
        self.admin_login(admin)
        settings = self.query("SELECT key, value FROM settings")
        values = {r["key"]: r["value"] for r in settings}
        admin.post("/admin/settings", data={"csrf_token": csrf(admin, "/admin/settings"),
                                            "base_url": values["base_url"], "party_name": values["party_name"],
                                            "fee_percent": percent})

    # --- product management ---

    def test_product_crud(self):
        token = csrf(self.m, "/merchant/products")
        self.m.post("/merchant/products", data={"csrf_token": token, "name": "Muffin", "price": "2.80"})  # rounds to 3
        p = self.query("SELECT * FROM products WHERE name = 'Muffin'")[0]
        self.assertEqual((p["merchant_id"], p["price_cents"], p["active"]), (self.shop["id"], 300, 1))

        self.m.post(f"/merchant/products/{p['id']}/edit",
                    data={"csrf_token": token, "name": "Blueberry Muffin", "price": "3.60", "active": "1"})
        p = self.query("SELECT * FROM products WHERE id = ?", (p["id"],))[0]
        self.assertEqual((p["name"], p["price_cents"]), ("Blueberry Muffin", 400))

        self.m.post(f"/merchant/products/{p['id']}/toggle", data={"csrf_token": token})
        self.assertEqual(self.query("SELECT active FROM products WHERE id = ?", (p["id"],))[0]["active"], 0)
        self.assertNotIn('aria-label="Quantity of Blueberry Muffin"',
                         self.m.get("/merchant/terminal").get_data(as_text=True))

        self.m.post(f"/merchant/products/{p['id']}/delete", data={"csrf_token": token})
        self.assertEqual(self.query("SELECT * FROM products WHERE id = ?", (p["id"],)), [])

    def test_product_with_sales_is_deactivated_not_deleted(self):
        latte = self.product_id("Latte")  # sold in the demo data
        self.m.post(f"/merchant/products/{latte}/delete", data={"csrf_token": csrf(self.m, "/merchant/products")})
        self.assertEqual(self.query("SELECT active FROM products WHERE id = ?", (latte,))[0]["active"], 0)

    def test_invalid_products_rejected(self):
        token = csrf(self.m, "/merchant/products")
        for name, price in (("", "1"), ("X", "0"), ("X", "-1"), ("X", "abc"), ("X" * 61, "1")):
            self.m.post("/merchant/products", data={"csrf_token": token, "name": name, "price": price})
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM products WHERE merchant_id = ?",
                                    (self.shop["id"],))[0]["n"], 3)

    def test_merchant_cannot_touch_other_merchants_products(self):
        chips = self.product_id("Chips")  # belongs to Snack Stand
        token = csrf(self.m, "/merchant/products")
        self.assertEqual(self.m.get(f"/merchant/products/{chips}/edit").status_code, 404)
        self.assertEqual(self.m.post(f"/merchant/products/{chips}/delete", data={"csrf_token": token}).status_code, 404)
        order_id, _ = self.create_order({"Chips": 1})
        self.assertIsNone(order_id)  # not on this merchant's menu

    # --- completion criteria: order -> authorize -> pay ---

    def test_order_payment_end_to_end(self):
        order_id, _ = self.create_order({"Latte": 2, "Croissant": 1}, custom="1.25", label="Tip")  # tip rounds to A$1
        order = self.query("SELECT * FROM orders WHERE id = ?", (order_id,))[0]
        self.assertEqual((order["status"], order["total_cents"]), ("open", 2 * 500 + 400 + 100))  # 15

        page = self.m.get(f"/merchant/orders/{order_id}/pay?card=demouser0001").get_data(as_text=True)
        self.assertIn("enter your PIN", page)
        self.assertNotIn("Alice", page)  # identification reveals nothing
        self.assertNotIn("A$95", page)

        r = self.pay(order_id)
        self.assertEqual(r.status_code, 302)
        receipt = self.m.get(f"/merchant/orders/{order_id}").get_data(as_text=True)
        self.assertIn("Payment approved", receipt)
        self.assertIn("Alice Johnson", receipt)

        self.assertEqual(self.balance("Alice Johnson"), 95_00 - 15_00)
        self.assertEqual(self.balance("Coffee Corner"), 5_00 + 15_00)
        order = self.query("SELECT * FROM orders WHERE id = ?", (order_id,))[0]
        tx = self.query("SELECT * FROM transactions WHERE id = ?", (order["transaction_id"],))[0]
        self.assertEqual((order["status"], tx["kind"], tx["from_account_id"], tx["to_account_id"], tx["amount_cents"]),
                         ("paid", "purchase", self.alice["id"], self.shop["id"], 15_00))
        self.assertEqual(tx["description"], "2× Latte, Croissant, Tip")

        # In both histories.
        self.assertIn("2× Latte, Croissant, Tip", self.m.get("/me/").get_data(as_text=True))
        alice = self.app.test_client()
        self.card_login(alice, "DEMOUSER0001", "1111")
        alice_page = alice.get("/me/history?dir=out").get_data(as_text=True)
        self.assertIn("Coffee Corner", alice_page)
        self.assertIn("-A$15", alice_page)
        with self.db() as conn:
            self.assertEqual(ledger_problems(conn), [])

    def test_server_calculates_total_from_db_prices(self):
        # A client cannot inject a price; only product ids and quantities are read.
        data = {"csrf_token": csrf(self.m, "/merchant/terminal"),
                f"qty_{self.product_id('Espresso')}": "3", "total": "0.01", "price": "0.01"}
        r = self.m.post("/merchant/orders", data=data)
        order_id = int(r.location.rsplit("/", 1)[1])
        self.assertEqual(self.query("SELECT total_cents FROM orders WHERE id = ?", (order_id,))[0]["total_cents"], 900)

    def test_custom_amount_only(self):
        order_id, _ = self.create_order(custom="7.20", label="Raffle ticket")
        self.pay(order_id)
        self.assertEqual(self.balance("Alice Johnson"), 95_00 - 7_00)

    def test_invalid_orders_rejected(self):
        for quantities, custom in (({}, ""), ({"Latte": -1}, ""), ({"Latte": 100}, ""), ({}, "-3"), ({}, "abc")):
            order_id, _ = self.create_order(quantities, custom)
            self.assertIsNone(order_id, (quantities, custom))
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM orders")[0]["n"], 0)

    # --- authorization and failures ---

    def test_wrong_pin_declines_without_charge(self):
        order_id, _ = self.create_order({"Espresso": 1})
        r = self.pay(order_id, pin="0000")
        self.assertEqual(r.status_code, 401)
        self.assertIn("Payment declined", r.get_data(as_text=True))
        self.assertIn("Wrong PIN", r.get_data(as_text=True))
        self.assertEqual(self.balance("Alice Johnson"), 95_00)
        self.assertEqual(self.query("SELECT status FROM orders WHERE id = ?", (order_id,))[0]["status"], "open")

    def test_pin_lockout_applies_at_terminal(self):
        order_id, _ = self.create_order({"Espresso": 1})
        for _ in range(5):
            self.pay(order_id, pin="0000")
        r = self.pay(order_id, pin="1111")
        self.assertEqual(r.status_code, 401)
        self.assertIn("Too many wrong PINs", r.get_data(as_text=True))

    def test_insufficient_funds_declined(self):
        order_id, _ = self.create_order(custom="500")
        r = self.pay(order_id)
        self.assertEqual(r.status_code, 400)
        self.assertIn("Insufficient funds", r.get_data(as_text=True))
        self.assertEqual(self.balance("Alice Johnson"), 95_00)
        self.assertEqual(self.balance("Coffee Corner"), 5_00)
        self.assertTrue(self.query("SELECT 1 FROM audit_logs WHERE action = 'payment.declined'"))

    def test_unknown_blocked_and_merchant_cards_rejected(self):
        order_id, _ = self.create_order({"Espresso": 1})
        with self.db() as conn:
            conn.execute("UPDATE cards SET status = 'blocked' WHERE token = 'DEMOUSER0002'")
        for card in ("NOPE12345", "DEMOUSER0002"):
            r = self.m.get(f"/merchant/orders/{order_id}/pay?card={card}")
            self.assertEqual(r.status_code, 302, card)
            self.assertIn("Try another card", self.m.get(r.location).get_data(as_text=True))

    def test_order_paid_only_once(self):
        order_id, _ = self.create_order({"Latte": 1})
        self.pay(order_id)
        self.pay(order_id)  # double tap on "Approve"
        self.assertEqual(self.balance("Alice Johnson"), 90_00)
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM transactions WHERE kind = 'purchase'")[0]["n"], 2)

        bob = self.pay(order_id, card="DEMOUSER0002", pin="2222")  # someone else tries to pay it again
        self.assertEqual(self.balance("Bob Smith"), 75_00)
        self.assertEqual(bob.status_code, 302)

    def test_cancelled_order_cannot_be_paid(self):
        order_id, _ = self.create_order({"Latte": 1})
        self.m.post(f"/merchant/orders/{order_id}/cancel", data={"csrf_token": csrf(self.m, "/merchant/terminal")})
        self.pay(order_id)
        self.assertEqual(self.balance("Alice Johnson"), 95_00)
        self.assertEqual(self.query("SELECT status FROM orders WHERE id = ?", (order_id,))[0]["status"], "cancelled")

    def test_other_merchant_cannot_see_or_pay_order(self):
        order_id, _ = self.create_order({"Latte": 1})
        other = self.app.test_client()
        self.card_login(other, "DEMOSHOP0002", "9002")
        self.assertEqual(other.get(f"/merchant/orders/{order_id}").status_code, 404)
        self.assertEqual(other.get(f"/merchant/orders/{order_id}/pay?card=DEMOUSER0001").status_code, 404)

    def test_engine_rejects_purchase_without_matching_order(self):
        with self.db() as conn, self.assertRaises(UnauthorizedError):
            post_transaction(conn, kind="purchase", from_id=self.alice["id"], to_id=self.shop["id"],
                             amount_cents=100, actor=self.shop)

    def test_any_participant_can_sell(self):
        alice = self.app.test_client()
        self.card_login(alice, "DEMOUSER0001", "1111")
        self.assertEqual(alice.get("/merchant/terminal").status_code, 200)
        self.assertEqual(alice.get("/merchant/products").status_code, 200)

    # --- card tap / QR scan on the merchant phone ---

    def test_card_url_on_merchant_phone_opens_pin_screen(self):
        order_id, _ = self.create_order({"Espresso": 1})
        r = self.m.get("/card/DEMOUSER0001")
        self.assertIn("/merchant/card/DEMOUSER0001", r.location)
        r = self.m.get(r.location)
        self.assertIn(f"/merchant/orders/{order_id}/pay?card=DEMOUSER0001", r.location)

    def test_card_url_without_open_order(self):
        r = self.m.get("/merchant/card/DEMOUSER0001")
        self.assertTrue(r.location.endswith("/merchant/terminal"))

    def test_nfc_tap_only_no_qr_or_card_entry(self):
        alice = self.app.test_client()
        self.card_login(alice, "DEMOUSER0001", "1111")
        self.assertNotIn("data:image/svg+xml", alice.get("/me/").get_data(as_text=True))
        admin = self.app.test_client()
        self.admin_login(admin)
        for path in ("/admin/cards", "/me/", "/login"):
            page = (admin if path.startswith("/admin") else alice).get(path).get_data(as_text=True)
            self.assertNotIn("QR", page, path)
        order_id, _ = self.create_order({"Latte": 1})
        page = self.m.get(f"/merchant/orders/{order_id}").get_data(as_text=True)
        self.assertIn("Tap to pay", page)
        self.assertNotIn('name="card"', page)

    # --- fees and dashboard ---

    def test_fee_and_dashboard_stats(self):
        self.set_fee("2.5")
        order_id, _ = self.create_order({"Latte": 2})  # 10 -> 2.5% = 0.25 -> below 1, so the fee is A$1
        self.pay(order_id)
        self.assertEqual(self.balance("Alice Johnson"), 95_00 - 10_00)
        self.assertEqual(self.balance("Coffee Corner"), 5_00 + 10_00 - 1_00)
        fee_tx = self.query("SELECT * FROM transactions WHERE kind = 'fee'")[0]
        self.assertEqual((fee_tx["from_account_id"], fee_tx["amount_cents"]), (self.shop["id"], 1_00))

        page = self.m.get("/me/").get_data(as_text=True)  # merged participant dashboard
        self.assertRegex(page, r'<span class="pos">A\$15</span>Total earned')  # sales: 5 demo + 10
        self.assertRegex(page, r'<span class="neg">A\$1</span>Total spent')    # the fee
        self.assertEqual(merchant_stats_net(self), 14_00)                         # net revenue still computed
        with self.db() as conn:
            self.assertEqual(ledger_problems(conn), [])

    def test_fees_hidden_when_disabled(self):
        page = self.m.get("/me/").get_data(as_text=True)
        self.assertRegex(page, r'<span class="neg">A\$0</span>Total spent')

    def test_fee_helpers(self):
        self.assertEqual(parse_fee_percent("2.5"), 250)
        self.assertEqual(parse_fee_percent(""), 0)
        for bad in ("-1", "21", "1.234", "abc"):
            with self.assertRaises(BankError):
                parse_fee_percent(bad)
        self.assertEqual(compute_fee(900, 250), 100)     # 0.225 -> minimum A$1
        self.assertEqual(compute_fee(10000, 250), 300)   # 2.5 -> 3
        self.assertEqual(compute_fee(10000, 240), 200)   # 2.4 -> 2
        self.assertEqual(compute_fee(100, 0), 0)


def merchant_stats_net(test):
    from partybank.banking import merchant_stats
    with test.db() as conn:
        return merchant_stats(conn, test.shop["id"])["net_cents"]


if __name__ == "__main__":
    unittest.main()
