"""Shared test setup: a fresh app + demo data in a temporary database."""

import os
import re
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from partybank import create_app  # noqa: E402
from partybank.db import connect  # noqa: E402
from partybank.seed import seed_demo  # noqa: E402


def csrf(client, url):
    html = client.get(url).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


class BankTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.app = self.make_app()
        seed_demo(self.db_path)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def make_app(self):
        return create_app({"DATABASE": self.db_path, "SECRET_KEY": "test", "TESTING": True})

    def db(self):
        return closing(connect(self.db_path))

    def query(self, sql, params=()):
        with self.db() as conn:
            return conn.execute(sql, params).fetchall()

    def account(self, name):
        return self.query("SELECT * FROM accounts WHERE name = ?", (name,))[0]

    def balance(self, name):
        return self.account(name)["balance_cents"]

    def card_login(self, client, card, pin):
        token = csrf(client, "/login")
        return client.post("/login", data={"csrf_token": token, "card": card, "pin": pin})

    def admin_login(self, client, username="admin", password="admin123"):
        token = csrf(client, "/admin/login")
        return client.post("/admin/login", data={"csrf_token": token, "username": username,
                                                 "password": password})
