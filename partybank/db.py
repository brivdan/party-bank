"""SQLite access: connections, explicit transactions, schema setup and settings."""

import os
import re
import socket
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from flask import current_app, g

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

RESERVE_NAME = "Party Bank Reserve"
CASH_VAULT_NAME = "Cash Vault"


def connect(path):
    # isolation_level=None: no implicit transactions; money operations use transaction().
    conn = sqlite3.connect(path, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


@contextmanager
def transaction(conn):
    """Run a block atomically. BEGIN IMMEDIATE takes the write lock up front,
    so concurrent balance checks cannot interleave."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def detect_lan_ip():
    """Best-effort LAN IP (no packets are sent by a UDP connect)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def default_settings():
    base_url = os.environ.get("PARTYBANK_BASE_URL") or f"http://{detect_lan_ip()}:5000"
    return {
        "base_url": base_url.rstrip("/"),
        "party_name": "Party Bank",
        "currency_symbol": "A$",
        "merchant_fee_bps": "0",  # merchant fee in basis points (250 = 2.5%); 0 = fees disabled
    }


def init_db(path):
    """Create tables, default settings and the reserve account if missing. Idempotent."""
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        _add_missing_columns(conn)
        _ensure_tx_kinds(conn)
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        with transaction(conn):
            for key, value in default_settings().items():
                conn.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value)
                )
            # The original reserve (Stages 1-3) is the first system account.
            conn.execute(
                "UPDATE accounts SET system_key = 'reserve' WHERE id = "
                "(SELECT MIN(id) FROM accounts WHERE role = 'system') "
                "AND NOT EXISTS (SELECT 1 FROM accounts WHERE system_key IS NOT NULL)"
            )
            for key, name in (("reserve", RESERVE_NAME), ("cash", CASH_VAULT_NAME)):
                if not conn.execute("SELECT 1 FROM accounts WHERE system_key = ?", (key,)).fetchone():
                    conn.execute(
                        "INSERT INTO accounts (name, role, system_key) VALUES (?, 'system', ?)", (name, key)
                    )
    finally:
        conn.close()


# Columns added after a table was first created: (table, column, SQL type).
# Applied before schema.sql so its indexes can reference them on older databases.
_ADDED_COLUMNS = [
    ("transactions", "idempotency_key", "TEXT"),  # Stage 2
    ("accounts", "system_key", "TEXT"),           # Stage 4
]


def _add_missing_columns(conn):
    for table, column, sql_type in _ADDED_COLUMNS:
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if cols and column not in cols:  # empty set = table not created yet
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")


# Allowed transactions.kind values. Must match the CHECK in schema.sql.
TX_KINDS = ("issue", "transfer", "purchase", "refund", "adjustment", "fee",
            "deposit", "withdrawal", "charge")
_KIND_CHECK_RE = re.compile(r"CHECK \(kind IN \(([^)]*)\)\)")


def _ensure_tx_kinds(conn):
    """Allow new transaction kinds on older databases (Stage 3: fee; Stage 4:
    deposit, withdrawal, charge). SQLite cannot alter a CHECK constraint, so
    rebuild the table, copying every row unchanged. schema.sql then re-creates
    the indexes and triggers that DROP TABLE removed."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'transactions'"
    ).fetchone()
    if row is None:
        return
    match = _KIND_CHECK_RE.search(row["sql"])
    if match is None:
        raise RuntimeError("Unexpected transactions schema; cannot update transaction kinds automatically.")
    current = {k.strip().strip("'") for k in match.group(1).split(",")}
    if set(TX_KINDS) <= current:
        return
    kinds_sql = ", ".join(f"'{k}'" for k in TX_KINDS)
    new_sql = (row["sql"][:match.start()] + f"CHECK (kind IN ({kinds_sql}))" + row["sql"][match.end():])
    # After an earlier rebuild SQLite stores the name quoted: CREATE TABLE "transactions".
    new_sql = re.sub(r'^CREATE TABLE\s+"?transactions"?', "CREATE TABLE transactions_new", new_sql, count=1)
    if not new_sql.startswith("CREATE TABLE transactions_new"):
        raise RuntimeError("Unexpected transactions schema; cannot update transaction kinds automatically.")
    count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")  # only takes effect outside a transaction
    try:
        with transaction(conn):
            conn.execute("DROP TRIGGER IF EXISTS trg_tx_immutable")
            conn.execute("DROP TRIGGER IF EXISTS trg_tx_no_delete")
            conn.execute(new_sql)
            conn.execute("INSERT INTO transactions_new SELECT * FROM transactions")
            if conn.execute("SELECT COUNT(*) FROM transactions_new").fetchone()[0] != count:
                raise RuntimeError("Row count mismatch while migrating transactions.")
            conn.execute("DROP TABLE transactions")
            conn.execute("ALTER TABLE transactions_new RENAME TO transactions")
            if conn.execute("PRAGMA foreign_key_check").fetchone():
                raise RuntimeError("Foreign key check failed while migrating transactions.")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def system_account_id(conn, key):
    """key: 'reserve' (the bank's own money) or 'cash' (the physical cash vault)."""
    return conn.execute("SELECT id FROM accounts WHERE system_key = ?", (key,)).fetchone()["id"]


def reserve_account_id(conn):
    return system_account_id(conn, "reserve")


def cash_account_id(conn):
    return system_account_id(conn, "cash")


def get_settings(conn):
    return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings")}


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
