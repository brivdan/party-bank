-- Party Bank schema. All money values are integer cents of A$.
-- Every statement is idempotent so the schema can be applied on each start.

CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY,
    name            TEXT    NOT NULL,
    role            TEXT    NOT NULL CHECK (role IN ('user', 'merchant', 'admin', 'system')),
    username        TEXT    UNIQUE COLLATE NOCASE,      -- admins only
    password_hash   TEXT,                               -- admins only
    pin_hash        TEXT,                               -- users and merchants
    system_key      TEXT,                               -- 'reserve' or 'cash' for the bank's own accounts
    balance_cents   INTEGER NOT NULL DEFAULT 0,
    status          TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'frozen')),
    failed_logins   INTEGER NOT NULL DEFAULT 0,
    locked_until    TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    -- Only the system reserve account may go negative (it issues money).
    CHECK (role = 'system' OR balance_cents >= 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_system_key ON accounts(system_key)
    WHERE system_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS cards (
    id              INTEGER PRIMARY KEY,
    token           TEXT    NOT NULL UNIQUE,            -- the only data written to the NFC tag / QR
    account_id      INTEGER REFERENCES accounts(id),    -- NULL = registered but unassigned
    status          TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'blocked')),
    label           TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    assigned_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_cards_account ON cards(account_id);

CREATE TABLE IF NOT EXISTS products (
    id              INTEGER PRIMARY KEY,
    merchant_id     INTEGER NOT NULL REFERENCES accounts(id),
    name            TEXT    NOT NULL,
    price_cents     INTEGER NOT NULL CHECK (price_cents > 0),
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_products_merchant ON products(merchant_id);

CREATE TABLE IF NOT EXISTS transactions (
    id              INTEGER PRIMARY KEY,
    kind            TEXT    NOT NULL CHECK (kind IN ('issue', 'transfer', 'purchase', 'refund', 'adjustment', 'fee', 'deposit', 'withdrawal', 'charge', 'cashback')),
    status          TEXT    NOT NULL DEFAULT 'completed'
                            CHECK (status IN ('pending', 'completed', 'failed', 'cancelled')),
    amount_cents    INTEGER NOT NULL CHECK (amount_cents > 0),
    from_account_id INTEGER NOT NULL REFERENCES accounts(id),
    to_account_id   INTEGER NOT NULL REFERENCES accounts(id),
    product_id      INTEGER REFERENCES products(id),
    description     TEXT,
    created_by      INTEGER REFERENCES accounts(id),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    completed_at    TEXT,
    idempotency_key TEXT,                               -- one key per payment form; blocks double submits
    CHECK (from_account_id <> to_account_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tx_idempotency ON transactions(idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tx_from ON transactions(from_account_id);
CREATE INDEX IF NOT EXISTS idx_tx_to ON transactions(to_account_id);

-- Posted money facts never change. Only status/completed_at may be updated
-- (reserved for a later pending -> completed flow); rows are never deleted.
CREATE TRIGGER IF NOT EXISTS trg_tx_immutable
BEFORE UPDATE OF kind, amount_cents, from_account_id, to_account_id, product_id,
                 created_by, created_at, idempotency_key ON transactions
BEGIN
    SELECT RAISE(ABORT, 'transactions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_tx_no_delete
BEFORE DELETE ON transactions
BEGIN
    SELECT RAISE(ABORT, 'transactions cannot be deleted');
END;

-- Double-entry: each completed transaction has one debit and one credit row that sum to zero.
CREATE TABLE IF NOT EXISTS ledger_entries (
    id                  INTEGER PRIMARY KEY,
    transaction_id      INTEGER NOT NULL REFERENCES transactions(id),
    account_id          INTEGER NOT NULL REFERENCES accounts(id),
    amount_cents        INTEGER NOT NULL,
    balance_after_cents INTEGER NOT NULL,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ledger_account ON ledger_entries(account_id, id);
CREATE INDEX IF NOT EXISTS idx_ledger_tx ON ledger_entries(transaction_id);

-- No direct balance overwrites: an account balance can only ever equal the
-- running balance of its latest ledger entry, and each ledger entry must
-- continue the previous one. So every balance change needs a ledger posting.
CREATE TRIGGER IF NOT EXISTS trg_accounts_start_at_zero
BEFORE INSERT ON accounts WHEN NEW.balance_cents <> 0
BEGIN
    SELECT RAISE(ABORT, 'accounts must start with a zero balance; post a transaction instead');
END;
CREATE TRIGGER IF NOT EXISTS trg_accounts_balance_from_ledger
BEFORE UPDATE OF balance_cents ON accounts
WHEN NEW.balance_cents <> COALESCE(
    (SELECT balance_after_cents FROM ledger_entries WHERE account_id = NEW.id ORDER BY id DESC LIMIT 1), 0)
BEGIN
    SELECT RAISE(ABORT, 'balances can only change through ledger entries');
END;
CREATE TRIGGER IF NOT EXISTS trg_ledger_chain
BEFORE INSERT ON ledger_entries
WHEN NEW.amount_cents = 0 OR NEW.balance_after_cents <> NEW.amount_cents + COALESCE(
    (SELECT balance_after_cents FROM ledger_entries WHERE account_id = NEW.account_id ORDER BY id DESC LIMIT 1), 0)
BEGIN
    SELECT RAISE(ABORT, 'ledger entry does not continue the account''s running balance');
END;

CREATE TRIGGER IF NOT EXISTS trg_ledger_no_update
BEFORE UPDATE ON ledger_entries
BEGIN
    SELECT RAISE(ABORT, 'ledger entries are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_ledger_no_delete
BEFORE DELETE ON ledger_entries
BEGIN
    SELECT RAISE(ABORT, 'ledger entries are immutable');
END;

-- Point-of-sale orders. Prepared by a merchant (status 'open'), then paid once
-- by a customer with card + PIN. The money movement itself is the linked
-- 'purchase' transaction; orders only describe what was bought.
CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY,
    merchant_id     INTEGER NOT NULL REFERENCES accounts(id),
    status          TEXT    NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'paid', 'cancelled')),
    total_cents     INTEGER NOT NULL CHECK (total_cents > 0),
    payment_key     TEXT    NOT NULL UNIQUE,            -- idempotency key of the purchase transaction
    customer_id     INTEGER REFERENCES accounts(id),
    transaction_id  INTEGER REFERENCES transactions(id),
    fee_cents       INTEGER NOT NULL DEFAULT 0,
    cashback_cents  INTEGER NOT NULL DEFAULT 0,             -- paid by the reserve to the customer
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    paid_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_merchant ON orders(merchant_id, id);

-- Line items keep a copy of name and price, so later product edits do not change old orders.
CREATE TABLE IF NOT EXISTS order_items (
    id                  INTEGER PRIMARY KEY,
    order_id            INTEGER NOT NULL REFERENCES orders(id),
    product_id          INTEGER REFERENCES products(id),  -- NULL = custom amount
    name                TEXT    NOT NULL,
    unit_price_cents    INTEGER NOT NULL CHECK (unit_price_cents > 0),
    quantity            INTEGER NOT NULL CHECK (quantity > 0),
    line_total_cents    INTEGER NOT NULL CHECK (line_total_cents = unit_price_cents * quantity)
);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_order_items_product ON order_items(product_id);

CREATE TABLE IF NOT EXISTS settings (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id          INTEGER PRIMARY KEY,
    actor_id    INTEGER REFERENCES accounts(id),
    action      TEXT    NOT NULL,
    details     TEXT,
    ip          TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(id DESC);
