"""First-run admin bootstrap and demo data."""

import os
import secrets

from .banking import (SYSTEM_ACTOR, assign_card, audit, create_account, create_admin,
                      post_transaction, register_card)
from .db import connect, transaction

DEMO_ADMIN = ("admin", "admin123")

# (name, role, PIN, opening balance in cents, card token)
DEMO_ACCOUNTS = [
    ("Alice Johnson", "user", "1111", 100_00, "DEMOUSER0001"),
    ("Bob Smith", "user", "2222", 75_00, "DEMOUSER0002"),
    ("Carol White", "user", "3333", 50_00, "DEMOUSER0003"),
    ("Coffee Corner", "merchant", "9001", 0, "DEMOSHOP0001"),
    ("Snack Stand", "merchant", "9002", 0, "DEMOSHOP0002"),
]

DEMO_PRODUCTS = {
    "Coffee Corner": [("Espresso", 3_00), ("Latte", 4_50), ("Croissant", 3_50)],
    "Snack Stand": [("Chips", 2_00), ("Soda", 2_50), ("Chocolate Bar", 1_50)],
}


def ensure_admin(db_path):
    """Create the first admin if none exists. Returns (username, password) when
    a new admin was created, otherwise None."""
    conn = connect(db_path)
    try:
        with transaction(conn):
            if conn.execute("SELECT 1 FROM accounts WHERE role = 'admin'").fetchone():
                return None
            password = os.environ.get("PARTYBANK_ADMIN_PASSWORD") or secrets.token_urlsafe(9)
            admin_id = create_admin(conn, username="admin", password=password)
            audit(conn, "admin.bootstrap", "initial admin account created", admin_id)
            return "admin", password
    finally:
        conn.close()


def seed_demo(db_path):
    """Insert demo admin, users, merchants, cards, products and one sample purchase."""
    conn = connect(db_path)
    try:
        with transaction(conn):
            if conn.execute("SELECT 1 FROM cards WHERE token = 'DEMOUSER0001'").fetchone():
                return False
            if not conn.execute("SELECT 1 FROM accounts WHERE role = 'admin'").fetchone():
                create_admin(conn, username=DEMO_ADMIN[0], password=DEMO_ADMIN[1])

            ids = {}
            for name, role, pin, opening, token in DEMO_ACCOUNTS:
                account_id = create_account(conn, name=name, role=role, pin=pin,
                                            initial_cents=opening, actor=SYSTEM_ACTOR)
                card_id, _ = register_card(conn, token, label="Demo card")
                assign_card(conn, card_id, account_id)
                ids[name] = account_id

            product_ids = {}
            for merchant, items in DEMO_PRODUCTS.items():
                for product, price in items:
                    product_ids[product] = conn.execute(
                        "INSERT INTO products (merchant_id, name, price_cents) VALUES (?, ?, ?)",
                        (ids[merchant], product, price),
                    ).lastrowid

            post_transaction(
                conn, kind="purchase", from_id=ids["Alice Johnson"], to_id=ids["Coffee Corner"],
                amount_cents=4_50, description="Latte", product_id=product_ids["Latte"],
                actor=SYSTEM_ACTOR,
            )

            for _ in range(3):
                register_card(conn, label="Spare card")

            audit(conn, "demo.seed", "demo accounts, cards and products created")
            return True
    finally:
        conn.close()
