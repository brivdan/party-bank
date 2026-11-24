"""Domain logic: money, card tokens, accounts, ledger postings and audit logging.

Functions that write take an open connection. Callers wrap them in
db.transaction() so several steps commit or roll back together.
"""

import re
import secrets
from contextlib import nullcontext
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from werkzeug.security import check_password_hash, generate_password_hash

from .db import TX_KINDS, cash_account_id, reserve_account_id, transaction

MAX_AMOUNT_CENTS = 100_000_000  # A$1,000,000 per transaction

# Card tokens: 12 chars from an alphabet without look-alikes (no 0/O, 1/I/L).
TOKEN_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
TOKEN_LENGTH = 12
CUSTOM_TOKEN_RE = re.compile(r"^[A-Z0-9]{6,32}$")
PIN_RE = re.compile(r"^\d{4,6}$")


class BankError(ValueError):
    """A user-facing error (validation, insufficient funds, ...)."""


# ---------- money ----------

def parse_amount(text, *, allow_zero=False):
    """Parse '12.50' / '12' / '12,50' into integer cents."""
    raw = (text or "").strip().replace(",", ".")
    if raw == "":
        raw = "0"
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise BankError("Enter a valid amount, e.g. 12.50.") from None
    if not value.is_finite():
        raise BankError("Enter a valid amount, e.g. 12.50.")
    if abs(value) * 100 > MAX_AMOUNT_CENTS:
        raise BankError("Amount is too large.")
    cents = value * 100
    if cents != cents.to_integral_value():
        raise BankError("Amounts can have at most 2 decimal places.")
    cents = int(cents)
    if cents < 0 or (cents == 0 and not allow_zero):
        raise BankError("Amount must be greater than zero.")
    return cents


def format_money(cents, symbol="A$"):
    cents = int(cents or 0)
    sign = "-" if cents < 0 else ""
    return f"{sign}{symbol}{abs(cents) / 100:,.2f}"


# ---------- card tokens ----------

def normalize_token(text):
    """Card identifiers are case-insensitive; spaces and dashes are ignored."""
    return re.sub(r"[^A-Za-z0-9]", "", text or "").upper()


def format_token(token):
    return "-".join(token[i:i + 4] for i in range(0, len(token), 4))


def generate_token(conn):
    while True:
        token = "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(TOKEN_LENGTH))
        if not conn.execute("SELECT 1 FROM cards WHERE token = ?", (token,)).fetchone():
            return token


def card_url(base_url, token):
    return f"{base_url.rstrip('/')}/card/{token}"


# ---------- secrets ----------

def validate_pin(pin):
    if not PIN_RE.match(pin or ""):
        raise BankError("PIN must be 4 to 6 digits.")
    return pin


def hash_secret(value):
    return generate_password_hash(value)


def verify_secret(stored_hash, value):
    return bool(stored_hash) and check_password_hash(stored_hash, value or "")


# ---------- PIN / password lockout ----------

MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 5


class PinError(BankError):
    """Wrong PIN or account temporarily locked."""


def is_locked(conn, account_id):
    return conn.execute(
        "SELECT 1 FROM accounts WHERE id = ? AND locked_until > datetime('now')", (account_id,)
    ).fetchone() is not None


def record_failed_secret(conn, account_id, what, ip=None):
    """Count a wrong PIN/password; lock the account after MAX_FAILED_LOGINS.
    Commits on its own so the count survives a rolled-back payment."""
    if conn.in_transaction:
        raise RuntimeError("record_failed_secret must not run inside another transaction")
    with transaction(conn):
        failed = conn.execute(
            "UPDATE accounts SET failed_logins = failed_logins + 1 WHERE id = ? RETURNING failed_logins",
            (account_id,),
        ).fetchone()[0]
        if failed >= MAX_FAILED_LOGINS:
            conn.execute(
                "UPDATE accounts SET failed_logins = 0, locked_until = datetime('now', ?) WHERE id = ?",
                (f"+{LOCK_MINUTES} minutes", account_id),
            )
            audit(conn, "login.locked", f"{what}: too many failed attempts", account_id, ip)
        else:
            audit(conn, "login.failed", what, account_id, ip)


def check_pin(conn, account_id, pin, what, ip=None):
    """Returns None if the PIN is right, otherwise a user-facing error message."""
    if is_locked(conn, account_id):
        return f"Too many wrong PINs. Try again in {LOCK_MINUTES} minutes."
    row = conn.execute("SELECT pin_hash FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is not None and verify_secret(row["pin_hash"], pin):
        with transaction(conn):
            conn.execute("UPDATE accounts SET failed_logins = 0 WHERE id = ?", (account_id,))
        return None
    record_failed_secret(conn, account_id, what, ip)
    return "Wrong PIN."


# ---------- audit ----------

def audit(conn, action, details="", actor_id=None, ip=None):
    conn.execute(
        "INSERT INTO audit_logs (actor_id, action, details, ip) VALUES (?, ?, ?, ?)",
        (actor_id, action, details, ip),
    )


# ---------- transaction engine ----------
#
# post_transaction() is the only code that changes balances. Every money
# movement (opening balances, transfers, and later purchases/refunds) goes
# through it, so validation, authorization, idempotency and double-entry
# bookkeeping are enforced in one place.

SYSTEM_ACTOR = "system"  # trusted internal callers only (seed data, CLI), never web requests

MAX_NOTE_LENGTH = 100
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


class UnauthorizedError(BankError):
    """The actor is not allowed to make this money movement."""


@dataclass(frozen=True)
class TxResult:
    tx_id: int
    duplicate: bool            # True if this idempotency key had already been processed
    from_balance_cents: int
    to_balance_cents: int


def new_idempotency_key():
    return secrets.token_urlsafe(18)


PARTICIPANT_ROLES = ("user", "merchant")
REASON_KINDS = ("adjustment", "charge")  # plus admin transfers; see post_transaction
MIN_REASON_LENGTH = 3


def is_system(acc, key):
    """key: 'reserve' (bank's own money) or 'cash' (physical cash vault)."""
    return acc["role"] == "system" and acc["system_key"] == key


def _authorize(conn, kind, actor, src, dst, amount_cents, idempotency_key):
    """Last line of defence: who may move money between which accounts.
    Views/pay_order still verify the session and the customer's PIN first."""
    if actor == SYSTEM_ACTOR:
        return
    role = actor["role"]
    admin = role == "admin"
    participant = lambda acc: acc["role"] in PARTICIPANT_ROLES  # noqa: E731
    reserve = lambda acc: is_system(acc, "reserve")  # noqa: E731
    cash = lambda acc: is_system(acc, "cash")  # noqa: E731

    if kind == "transfer":
        if admin:  # bank desk: between any customer/merchant accounts and the reserve
            allowed = all(participant(a) or reserve(a) for a in (src, dst))
        else:
            allowed = participant(actor) and actor["id"] == src["id"] and participant(dst)
    elif kind == "deposit":      # physical cash handed in at the desk
        allowed = admin and cash(src) and (participant(dst) or reserve(dst))
    elif kind == "withdrawal":   # physical cash paid out at the desk
        allowed = admin and (participant(src) or reserve(src)) and cash(dst)
    elif kind == "adjustment":   # credit or debit with a reason
        allowed = admin and ((reserve(src) and participant(dst)) or (participant(src) and reserve(dst)))
    elif kind == "charge":       # customer-approved charge, earned by the bank
        allowed = admin and participant(src) and reserve(dst)
    elif kind == "issue":        # legacy (Stages 1-3): money created by the reserve
        allowed = admin and reserve(src) and participant(dst)
    elif kind == "purchase":
        # A merchant can only collect into its own account, from a customer,
        # for exactly the total of one of its own open orders.
        allowed = (role == "merchant" and actor["id"] == dst["id"] and src["role"] == "user"
                   and idempotency_key is not None
                   and conn.execute(
                       "SELECT 1 FROM orders WHERE payment_key = ? AND merchant_id = ? "
                       "AND status = 'open' AND total_cents = ?",
                       (idempotency_key, actor["id"], amount_cents)).fetchone() is not None)
    elif kind == "fee":
        allowed = role == "merchant" and actor["id"] == src["id"] and reserve(dst)
    else:
        allowed = False  # refunds arrive with their feature
    if not allowed:
        raise UnauthorizedError("You are not allowed to make this payment.")


def _balance(conn, account_id):
    return conn.execute("SELECT balance_cents FROM accounts WHERE id = ?", (account_id,)).fetchone()[0]


def post_transaction(conn, *, kind, from_id, to_id, amount_cents, actor, description="",
                     idempotency_key=None, product_id=None):
    """Move money from one account to another, atomically.

    - actor: the logged-in account row making the request, or SYSTEM_ACTOR.
    - amount_cents: positive int in the smallest currency unit.
    - idempotency_key: optional; a repeated key for the same payment returns the
      original result (duplicate=True) instead of paying twice.

    Writes one transactions row and two ledger_entries rows (debit and credit
    summing to zero). Joins the caller's open transaction, or opens its own.
    Raises BankError / UnauthorizedError; nothing is written on error.
    """
    if actor is None:
        raise RuntimeError("post_transaction requires an actor")
    if kind not in TX_KINDS:
        raise BankError("Unknown transaction type.")
    if isinstance(amount_cents, bool) or not isinstance(amount_cents, int):
        raise BankError("Amount must be a whole number of cents.")
    if amount_cents <= 0:
        raise BankError("Amount must be greater than zero.")
    if amount_cents > MAX_AMOUNT_CENTS:
        raise BankError("Amount is too large.")
    if from_id == to_id:
        raise BankError("You cannot send money to the same account.")
    if idempotency_key is not None and not IDEMPOTENCY_KEY_RE.match(idempotency_key):
        raise BankError("Invalid payment reference. Start the payment again.")
    description = (description or "").strip()[:MAX_NOTE_LENGTH] or None
    admin_actor = actor != SYSTEM_ACTOR and actor["role"] == "admin"
    if (kind in REASON_KINDS or (kind == "transfer" and admin_actor)) and \
            len(description or "") < MIN_REASON_LENGTH:
        raise BankError("A reason is required (at least 3 characters).")

    with nullcontext() if conn.in_transaction else transaction(conn):
        return _post(conn, kind, from_id, to_id, amount_cents, actor, description,
                     idempotency_key, product_id)


def _post(conn, kind, from_id, to_id, amount_cents, actor, description, idempotency_key, product_id):
    actor_id = None if actor == SYSTEM_ACTOR else actor["id"]

    if idempotency_key is not None:
        prev = conn.execute(
            "SELECT * FROM transactions WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if prev is not None:
            same = (prev["kind"], prev["from_account_id"], prev["to_account_id"],
                    prev["amount_cents"], prev["created_by"]) == (kind, from_id, to_id, amount_cents, actor_id)
            if not same:
                raise BankError("This payment form was already used. Start a new payment.")
            return TxResult(prev["id"], True, _balance(conn, from_id), _balance(conn, to_id))

    # Re-read the actor inside the write lock: a frozen account cannot pay.
    if actor_id is not None:
        actor = conn.execute("SELECT * FROM accounts WHERE id = ?", (actor_id,)).fetchone()
        if actor is None or actor["status"] != "active":
            raise UnauthorizedError("Your account is not active.")

    src = conn.execute("SELECT * FROM accounts WHERE id = ?", (from_id,)).fetchone()
    dst = conn.execute("SELECT * FROM accounts WHERE id = ?", (to_id,)).fetchone()
    if src is None or dst is None:
        raise BankError("Account not found.")
    for acc in (src, dst):
        if acc["role"] == "admin":
            raise BankError("Admin accounts do not hold money.")
        if acc["status"] != "active":
            raise BankError(f"Account '{acc['name']}' is frozen.")
    _authorize(conn, kind, actor, src, dst, amount_cents, idempotency_key)
    if src["role"] != "system" and src["balance_cents"] < amount_cents:
        raise BankError("Insufficient funds.")
    # The vault balance is minus the physical cash held, so it can't pay out more than that.
    if kind == "withdrawal" and -dst["balance_cents"] < amount_cents:
        raise BankError(f"Not enough physical cash in the vault ({format_money(-dst['balance_cents'])} held).")

    tx_id = conn.execute(
        "INSERT INTO transactions (kind, status, amount_cents, from_account_id, to_account_id, "
        "product_id, description, created_by, completed_at, idempotency_key) "
        "VALUES (?, 'completed', ?, ?, ?, ?, ?, ?, datetime('now'), ?)",
        (kind, amount_cents, from_id, to_id, product_id, description, actor_id, idempotency_key),
    ).lastrowid
    # Ledger entry first, then the balance: the database triggers only accept a
    # balance that equals the running balance of the account's latest entry.
    balances = {from_id: src["balance_cents"] - amount_cents, to_id: dst["balance_cents"] + amount_cents}
    for account_id, delta in ((from_id, -amount_cents), (to_id, amount_cents)):
        conn.execute(
            "INSERT INTO ledger_entries (transaction_id, account_id, amount_cents, balance_after_cents) "
            "VALUES (?, ?, ?, ?)",
            (tx_id, account_id, delta, balances[account_id]),
        )
        conn.execute("UPDATE accounts SET balance_cents = ? WHERE id = ?", (balances[account_id], account_id))
    return TxResult(tx_id, False, balances[from_id], balances[to_id])


def cash_deposit(conn, account_id, amount_cents, description, actor, idempotency_key=None):
    """Physical cash received at the desk becomes digital money (Cash Vault -> account)."""
    return post_transaction(
        conn, kind="deposit", from_id=cash_account_id(conn), to_id=account_id,
        amount_cents=amount_cents, description=description or "Cash deposit", actor=actor,
        idempotency_key=idempotency_key,
    )


def ledger_problems(conn):
    """Integrity check. Returns a list of human-readable problems (empty = OK)."""
    problems = []
    for row in conn.execute(
        "SELECT transaction_id, COUNT(*) AS n, SUM(amount_cents) AS total "
        "FROM ledger_entries GROUP BY transaction_id HAVING n <> 2 OR total <> 0"
    ):
        problems.append(f"Transaction #{row['transaction_id']} is unbalanced "
                        f"({row['n']} entries, sum {row['total']}).")
    for row in conn.execute(
        "SELECT t.id FROM transactions t WHERE t.status = 'completed' "
        "AND NOT EXISTS (SELECT 1 FROM ledger_entries le WHERE le.transaction_id = t.id)"
    ):
        problems.append(f"Transaction #{row['id']} has no ledger entries.")
    for row in conn.execute(
        "SELECT a.id, a.name, a.balance_cents, COALESCE(SUM(le.amount_cents), 0) AS ledger "
        "FROM accounts a LEFT JOIN ledger_entries le ON le.account_id = a.id "
        "GROUP BY a.id HAVING a.balance_cents <> ledger"
    ):
        problems.append(f"Account #{row['id']} {row['name']}: balance {row['balance_cents']} "
                        f"but ledger sums to {row['ledger']}.")
    return problems


def balance_snapshot(conn, account_id):
    """Small payload for the balance auto-refresh."""
    row = conn.execute(
        "SELECT a.balance_cents, (SELECT MAX(id) FROM ledger_entries WHERE account_id = a.id) AS last_entry "
        "FROM accounts a WHERE a.id = ?", (account_id,),
    ).fetchone()
    return {"balance_cents": row["balance_cents"], "last_entry": row["last_entry"] or 0}


def account_history(conn, account_id, limit=50, direction=None):
    """Ledger lines for one account, newest first. direction: None, 'in' or 'out'."""
    where = {"in": " AND le.amount_cents > 0", "out": " AND le.amount_cents < 0"}.get(direction, "")
    return conn.execute(
        f"""
        SELECT le.amount_cents, le.balance_after_cents, le.created_at,
               CASE WHEN le.amount_cents < 0 THEN 'out' ELSE 'in' END AS direction,
               t.id AS tx_id, t.kind, t.description,
               CASE WHEN le.amount_cents < 0 THEN ta.name ELSE fa.name END AS counterparty,
               CASE WHEN le.amount_cents < 0 THEN ta.id ELSE fa.id END AS counterparty_id
        FROM ledger_entries le
        JOIN transactions t ON t.id = le.transaction_id
        JOIN accounts fa ON fa.id = t.from_account_id
        JOIN accounts ta ON ta.id = t.to_account_id
        WHERE le.account_id = ?{where}
        ORDER BY le.id DESC
        LIMIT ?
        """,
        (account_id, limit),
    ).fetchall()


# ---------- account numbers ----------

def format_account_number(account_id):
    return f"{int(account_id):04d}"


def parse_account_number(text):
    digits = re.sub(r"[\s#-]", "", text or "")
    if not digits.isdigit() or len(digits) > 9:
        raise BankError("Enter the recipient's account number, e.g. 0004.")
    return int(digits)


# ---------- accounts and cards ----------

def create_account(conn, *, name, role, pin, actor, initial_cents=0):
    """Create a user or merchant account. Must run inside db.transaction()."""
    name = (name or "").strip()
    if not name or len(name) > 80:
        raise BankError("Name is required (max 80 characters).")
    if role not in ("user", "merchant"):
        raise BankError("Role must be user or merchant.")
    validate_pin(pin)
    account_id = conn.execute(
        "INSERT INTO accounts (name, role, pin_hash) VALUES (?, ?, ?)",
        (name, role, hash_secret(pin)),
    ).lastrowid
    if initial_cents:
        cash_deposit(conn, account_id, initial_cents, "Opening cash deposit", actor=actor)
    return account_id


def create_admin(conn, *, username, password, name="Administrator"):
    username = (username or "").strip()
    if not re.match(r"^[A-Za-z0-9_.-]{3,32}$", username):
        raise BankError("Username must be 3-32 letters, digits, '.', '_' or '-'.")
    if len(password or "") < 6:
        raise BankError("Password must be at least 6 characters.")
    return conn.execute(
        "INSERT INTO accounts (name, role, username, password_hash) VALUES (?, 'admin', ?, ?)",
        (name, username, hash_secret(password)),
    ).lastrowid


def register_card(conn, token=None, label=None):
    """Register a card. Without a token a random one is generated."""
    if token:
        token = normalize_token(token)
        if not CUSTOM_TOKEN_RE.match(token):
            raise BankError("Card identifier must be 6-32 letters or digits.")
        if conn.execute("SELECT 1 FROM cards WHERE token = ?", (token,)).fetchone():
            raise BankError("That card identifier is already registered.")
    else:
        token = generate_token(conn)
    card_id = conn.execute(
        "INSERT INTO cards (token, label) VALUES (?, ?)", (token, (label or "").strip() or None)
    ).lastrowid
    return card_id, token


def assign_card(conn, card_id, account_id):
    card = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    if card is None:
        raise BankError("Card not found.")
    if card["account_id"] is not None:
        raise BankError("Card is already assigned to an account.")
    acc = conn.execute("SELECT role FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if acc is None or acc["role"] not in ("user", "merchant"):
        raise BankError("Cards can only be assigned to user or merchant accounts.")
    conn.execute(
        "UPDATE cards SET account_id = ?, assigned_at = datetime('now') WHERE id = ?",
        (account_id, card_id),
    )


# ---------- products ----------

MAX_PRODUCT_NAME = 60
MAX_QUANTITY = 99


def validate_product(name, price_text):
    name = (name or "").strip()
    if not name or len(name) > MAX_PRODUCT_NAME:
        raise BankError(f"Product name is required (max {MAX_PRODUCT_NAME} characters).")
    return name, parse_amount(price_text)


# ---------- fees ----------

MAX_FEE_BPS = 2000  # 20%


def parse_fee_percent(text):
    """'2.5' -> 250 basis points. Empty = 0 (fees disabled)."""
    raw = (text or "").strip().replace(",", ".").rstrip("%") or "0"
    try:
        bps = Decimal(raw) * 100
    except InvalidOperation:
        raise BankError("Fee must be a percentage, e.g. 2.5.") from None
    if not bps.is_finite() or bps != bps.to_integral_value() or not 0 <= bps <= MAX_FEE_BPS:
        raise BankError("Fee must be between 0 and 20%, with at most 2 decimals.")
    return int(bps)


def fee_bps(conn):
    row = conn.execute("SELECT value FROM settings WHERE key = 'merchant_fee_bps'").fetchone()
    return int(row["value"]) if row else 0


def compute_fee(total_cents, bps):
    """Integer fee, rounded half up to the nearest cent."""
    return (total_cents * bps + 5000) // 10000


# ---------- orders (point of sale) ----------

@dataclass(frozen=True)
class PaymentResult:
    order_id: int
    tx_id: int
    total_cents: int
    fee_cents: int
    customer_name: str
    duplicate: bool


def create_order(conn, *, merchant, quantities, custom_cents=0, custom_label=""):
    """Create an open order. quantities: {product_id: qty}. Prices come from
    the database, never from the client. Returns the order id."""
    lines = []
    wanted = {}
    for product_id, qty in quantities.items():
        if isinstance(qty, bool) or not isinstance(qty, int) or not 0 <= qty <= MAX_QUANTITY:
            raise BankError(f"Quantity must be between 0 and {MAX_QUANTITY}.")
        if qty:
            wanted[int(product_id)] = qty
    if wanted:
        marks = ",".join("?" * len(wanted))
        products = {p["id"]: p for p in conn.execute(
            f"SELECT * FROM products WHERE merchant_id = ? AND active = 1 AND id IN ({marks})",
            (merchant["id"], *wanted),
        )}
        if len(products) != len(wanted):
            raise BankError("A selected product is no longer available. Reload the terminal.")
        for product_id, qty in wanted.items():
            p = products[product_id]
            lines.append((p["id"], p["name"], p["price_cents"], qty))
    if custom_cents:
        label = (custom_label or "").strip()[:MAX_PRODUCT_NAME] or "Custom amount"
        lines.append((None, label, custom_cents, 1))
    if not lines:
        raise BankError("Add at least one product or a custom amount.")
    total = sum(price * qty for _, _, price, qty in lines)
    if total > MAX_AMOUNT_CENTS:
        raise BankError("Order total is too large.")

    with nullcontext() if conn.in_transaction else transaction(conn):
        order_id = conn.execute(
            "INSERT INTO orders (merchant_id, total_cents, payment_key) VALUES (?, ?, ?)",
            (merchant["id"], total, new_idempotency_key()),
        ).lastrowid
        conn.executemany(
            "INSERT INTO order_items (order_id, product_id, name, unit_price_cents, quantity, line_total_cents) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(order_id, pid, name, price, qty, price * qty) for pid, name, price, qty in lines],
        )
    return order_id


def order_items(conn, order_id):
    return conn.execute("SELECT * FROM order_items WHERE order_id = ? ORDER BY id", (order_id,)).fetchall()


def order_summary(conn, order_id):
    """'2× Latte, Croissant' - used as the purchase description in both histories."""
    parts = [f"{i['quantity']}× {i['name']}" if i["quantity"] > 1 else i["name"]
             for i in order_items(conn, order_id)]
    text = ", ".join(parts)
    return text if len(text) <= MAX_NOTE_LENGTH else text[:MAX_NOTE_LENGTH - 1] + "…"


def customer_for_card(conn, token):
    """Identify the paying customer from a card. Identification only:
    pay_order() still requires the customer's PIN."""
    row = conn.execute(
        "SELECT c.status AS card_status, a.id, a.name, a.role, a.status "
        "FROM cards c JOIN accounts a ON a.id = c.account_id WHERE c.token = ?",
        (normalize_token(token),),
    ).fetchone()
    if row is None or row["role"] != "user":
        raise BankError("Card not recognised.")
    if row["card_status"] != "active":
        raise BankError("This card is blocked.")
    if row["status"] != "active":
        raise BankError("This customer's account is frozen.")
    return row


def _paid_result(order, customer, duplicate):
    if order["status"] == "cancelled":
        raise BankError("This order was cancelled.")
    if order["customer_id"] != customer["id"]:
        raise BankError("This order has already been paid.")
    return PaymentResult(order["id"], order["transaction_id"], order["total_cents"],
                         order["fee_cents"], customer["name"], duplicate)


def pay_order(conn, *, order_id, card_token, pin, actor, ip=None):
    """Customer pays a merchant's open order with card + PIN.

    One atomic SQLite transaction posts the purchase (customer -> merchant),
    the optional fee (merchant -> reserve) and marks the order paid. Paying
    the same order twice returns the first result with duplicate=True.
    """
    if conn.in_transaction:
        raise RuntimeError("pay_order manages its own transactions")
    customer = customer_for_card(conn, card_token)

    def load_order():
        order = conn.execute(
            "SELECT * FROM orders WHERE id = ? AND merchant_id = ?", (order_id, actor["id"])
        ).fetchone()
        if order is None:
            raise BankError("Order not found.")
        return order

    order = load_order()
    if order["status"] != "open":  # don't spend a PIN attempt on a finished order
        return _paid_result(order, customer, duplicate=True)

    pin_error = check_pin(conn, customer["id"], pin, f"payment PIN for order #{order_id}", ip)
    if pin_error:
        raise PinError(pin_error)

    with transaction(conn):
        order = load_order()  # re-check under the write lock
        if order["status"] != "open":
            return _paid_result(order, customer, duplicate=True)
        total = order["total_cents"]
        fee = compute_fee(total, fee_bps(conn))
        purchase = post_transaction(
            conn, kind="purchase", from_id=customer["id"], to_id=actor["id"], amount_cents=total,
            actor=actor, description=order_summary(conn, order_id), idempotency_key=order["payment_key"],
        )
        if fee:
            post_transaction(
                conn, kind="fee", from_id=actor["id"], to_id=reserve_account_id(conn), amount_cents=fee,
                actor=actor, description=f"Fee for order #{order_id}",
                idempotency_key=order["payment_key"] + "-fee",
            )
        conn.execute(
            "UPDATE orders SET status = 'paid', customer_id = ?, transaction_id = ?, fee_cents = ?, "
            "paid_at = datetime('now') WHERE id = ?",
            (customer["id"], purchase.tx_id, fee, order_id),
        )
    return PaymentResult(order_id, purchase.tx_id, total, fee, customer["name"], False)


def merchant_stats(conn, merchant_id):
    row = conn.execute(
        """
        SELECT
          (SELECT COALESCE(SUM(amount_cents), 0) FROM transactions
            WHERE kind = 'purchase' AND status = 'completed' AND to_account_id = :m) AS sales_cents,
          (SELECT COUNT(*) FROM transactions
            WHERE kind = 'purchase' AND status = 'completed' AND to_account_id = :m) AS sales_count,
          (SELECT COALESCE(SUM(amount_cents), 0) FROM transactions
            WHERE kind = 'fee' AND status = 'completed' AND from_account_id = :m) AS fees_cents
        """,
        {"m": merchant_id},
    ).fetchone()
    stats = dict(row)
    stats["net_cents"] = stats["sales_cents"] - stats["fees_cents"]
    return stats


# ---------- bank desk (admin financial actions) ----------

# action -> (label, transaction kind)
DESK_ACTIONS = {
    "deposit": ("Cash deposit", "deposit"),
    "withdrawal": ("Cash withdrawal", "withdrawal"),
    "transfer": ("Transfer", "transfer"),
    "charge": ("Charge", "charge"),
    "credit": ("Credit", "adjustment"),
    "debit": ("Debit", "adjustment"),
}


def desk_action(conn, *, action, actor, account_id, amount_cents, reason="", to_account_id=None,
                pin=None, idempotency_key=None, ip=None):
    """The single entry point for admin money actions. Each one is a normal
    ledger transaction (via post_transaction) plus an audit log entry,
    committed together. Balances are never written directly.

    - deposit / withdrawal: physical cash in / out; moves against the Cash Vault.
    - transfer: account_id -> to_account_id (customers, merchants or the reserve).
    - charge: account -> reserve, approved by the account holder's PIN; bank earnings.
    - credit / debit: reserve <-> account correction with a mandatory reason.
    """
    if actor == SYSTEM_ACTOR or actor["role"] != "admin":
        raise UnauthorizedError("Only administrators can use the bank desk.")
    if action not in DESK_ACTIONS:
        raise BankError("Unknown action.")
    label, kind = DESK_ACTIONS[action]
    account = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if account is None or account["role"] == "admin" or (account["role"] == "system" and action not in
                                                         ("deposit", "withdrawal", "transfer")):
        raise BankError("Choose a customer or merchant account.")
    reserve, cash = reserve_account_id(conn), cash_account_id(conn)
    if account_id == cash:
        raise BankError("The Cash Vault only changes through deposits and withdrawals.")
    routes = {
        "deposit": (cash, account_id),
        "withdrawal": (account_id, cash),
        "transfer": (account_id, to_account_id),
        "charge": (account_id, reserve),
        "credit": (reserve, account_id),
        "debit": (account_id, reserve),
    }
    from_id, to_id = routes[action]
    if to_id is None:
        raise BankError("Choose the account to transfer to.")
    reason = (reason or "").strip()
    # Cash movements are self-explanatory; everything else needs a reason.
    if action not in ("deposit", "withdrawal") and len(reason) < MIN_REASON_LENGTH:
        raise BankError("A reason is required (at least 3 characters).")

    if action == "charge":
        # Customer approval: the account holder enters their PIN at the desk.
        # Checked (and counted toward lockout) before anything moves.
        if conn.in_transaction:
            raise RuntimeError("charges check the PIN in their own transaction")
        pin_error = check_pin(conn, account_id, pin, "charge PIN at the bank desk", ip)
        if pin_error:
            raise PinError(pin_error)

    with nullcontext() if conn.in_transaction else transaction(conn):
        result = post_transaction(
            conn, kind=kind, from_id=from_id, to_id=to_id, amount_cents=amount_cents, actor=actor,
            description=f"{label}: {reason}" if action in ("credit", "debit") else (reason or label),
            idempotency_key=idempotency_key,
        )
        if not result.duplicate:
            names = dict(conn.execute("SELECT id, name FROM accounts WHERE id IN (?, ?)", (from_id, to_id)))
            audit(conn, f"money.{action}",
                  f"ref #{result.tx_id} {format_money(amount_cents)} {names[from_id]} -> {names[to_id]}"
                  f"{': ' + reason if reason else ''}", actor["id"], ip)
    return result


def bank_overview(conn):
    """Figures for the admin dashboard. Customer deposits are cash held for
    customers, not bank earnings: earnings are only fees and charges."""
    row = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM accounts WHERE role = 'user') AS users,
          (SELECT COUNT(*) FROM accounts WHERE role = 'merchant') AS merchants,
          (SELECT COALESCE(SUM(balance_cents), 0) FROM accounts WHERE role IN ('user', 'merchant')) AS digital_cents,
          (SELECT balance_cents FROM accounts WHERE system_key = 'reserve') AS reserve_cents,
          (SELECT -balance_cents FROM accounts WHERE system_key = 'cash') AS cash_cents,
          (SELECT COUNT(*) FROM transactions WHERE status = 'completed') AS tx_count,
          (SELECT COALESCE(SUM(amount_cents), 0) FROM transactions
            WHERE status = 'completed' AND kind IN ('purchase', 'transfer')) AS volume_cents,
          (SELECT COALESCE(SUM(t.amount_cents), 0) FROM transactions t
            JOIN accounts a ON a.id = t.to_account_id
            WHERE t.status = 'completed' AND t.kind IN ('fee', 'charge') AND a.system_key = 'reserve') AS earnings_cents,
          (SELECT COALESCE(SUM(amount_cents), 0) FROM transactions
            WHERE status = 'completed' AND kind = 'deposit') AS deposits_cents,
          (SELECT COALESCE(SUM(amount_cents), 0) FROM transactions
            WHERE status = 'completed' AND kind = 'withdrawal') AS withdrawals_cents,
          (SELECT COUNT(*) FROM cards) AS cards,
          (SELECT COUNT(*) FROM cards WHERE account_id IS NULL) AS free_cards
        """
    ).fetchone()
    stats = dict(row)
    stats["accounts"] = stats["users"] + stats["merchants"]
    return stats


def balance_ranking(conn, limit=10):
    return conn.execute(
        "SELECT id, name, role, balance_cents FROM accounts WHERE role IN ('user', 'merchant') "
        "ORDER BY balance_cents DESC, name LIMIT ?", (limit,),
    ).fetchall()
