"""Administrator pages: accounts, cards, settings and audit log."""

import secrets

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from .auth import role_required
from .banking import (DESK_ACTIONS, BankError, PinError, account_history, assign_card, audit,
                      balance_ranking, bank_overview, card_url, create_account, desk_action,
                      new_idempotency_key,
                      format_money, hash_secret, ledger_problems, normalize_token, parse_amount,
                      parse_fee_percent, register_card, validate_pin, verify_secret)
from .db import get_db, get_settings, set_setting, transaction

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.before_request
@role_required("admin")
def require_admin():
    """Every route in this blueprint is admin-only. The /admin/login page
    belongs to the auth blueprint, so this check does not apply to it."""
    return None


def _audit(conn, action, details=""):
    audit(conn, action, details, g.account["id"], request.remote_addr)


def _get_account(conn, account_id, allow_system=False):
    roles = ("user", "merchant", "system") if allow_system else ("user", "merchant")
    acc = conn.execute(
        f"SELECT * FROM accounts WHERE id = ? AND role IN ({','.join('?' * len(roles))})",
        (account_id, *roles),
    ).fetchone()
    if acc is None:
        abort(404, "Account not found.")
    return acc


# ---------- dashboard ----------

@bp.route("/")
def dashboard():
    conn = get_db()
    accounts = conn.execute(
        "SELECT a.*, (SELECT COUNT(*) FROM cards c WHERE c.account_id = a.id) AS card_count "
        "FROM accounts a WHERE a.role IN ('user', 'merchant') ORDER BY a.role, a.name"
    ).fetchall()
    bank_accounts = conn.execute(
        "SELECT * FROM accounts WHERE role = 'system' ORDER BY system_key DESC"
    ).fetchall()
    return render_template("admin/dashboard.html", accounts=accounts, bank_accounts=bank_accounts,
                           stats=bank_overview(conn), ranking=balance_ranking(conn),
                           ledger_problems=ledger_problems(conn))


@bp.route("/transactions")
def transactions():
    conn = get_db()
    rows = conn.execute(
        "SELECT t.*, fa.name AS from_name, ta.name AS to_name, ca.name AS created_by_name "
        "FROM transactions t "
        "JOIN accounts fa ON fa.id = t.from_account_id "
        "JOIN accounts ta ON ta.id = t.to_account_id "
        "LEFT JOIN accounts ca ON ca.id = t.created_by "
        "ORDER BY t.id DESC LIMIT 500"
    ).fetchall()
    return render_template("admin/transactions.html", rows=rows, ledger_problems=ledger_problems(conn))


# ---------- accounts ----------

@bp.route("/accounts/new", methods=["GET", "POST"])
def account_new():
    conn = get_db()
    form = request.form
    if request.method == "POST":
        try:
            initial = parse_amount(form.get("initial_balance"), allow_zero=True)
            card_mode = form.get("card_mode", "generate")
            pin = form.get("pin")
            if not pin and not current_app.config["REQUIRE_PIN"]:
                # PINs are off: store a random one so the account is safe if PINs are re-enabled.
                pin = f"{secrets.randbelow(10**6):06d}"
            with transaction(conn):
                account_id = create_account(
                    conn, name=form.get("name"), role=form.get("role") or "user", pin=pin,
                    initial_cents=initial, actor=g.account,
                )
                token = None
                if card_mode == "generate":
                    card_id, token = register_card(conn)
                    assign_card(conn, card_id, account_id)
                elif card_mode == "existing":
                    card_id, token = _find_or_register_card(conn, form.get("card_token"))
                    assign_card(conn, card_id, account_id)
                _audit(conn, "account.create",
                       f"#{account_id} {form.get('name', '').strip()} ({form.get('role')}), "
                       f"opening {format_money(initial)}, card {token or 'none'}")
            flash("Account created.", "success")
            return redirect(url_for("admin.account_detail", account_id=account_id))
        except BankError as e:
            flash(str(e), "error")
    free_cards = conn.execute(
        "SELECT * FROM cards WHERE account_id IS NULL AND status = 'active' ORDER BY id"
    ).fetchall()
    return render_template("admin/account_new.html", form=form, free_cards=free_cards)


def _find_or_register_card(conn, raw_token):
    """Use an already-registered unassigned card, or register the typed identifier."""
    token = normalize_token(raw_token)
    if not token:
        raise BankError("Enter or scan a card identifier.")
    card = conn.execute("SELECT id FROM cards WHERE token = ?", (token,)).fetchone()
    if card is not None:
        return card["id"], token
    return register_card(conn, token)


@bp.route("/accounts/<int:account_id>")
def account_detail(account_id):
    conn = get_db()
    acc = _get_account(conn, account_id, allow_system=True)
    cards = conn.execute("SELECT * FROM cards WHERE account_id = ? ORDER BY id", (account_id,)).fetchall()
    base_url = get_settings(conn)["base_url"]
    products = conn.execute(
        "SELECT * FROM products WHERE merchant_id = ? ORDER BY name", (account_id,)
    ).fetchall() if acc["role"] == "merchant" else []
    free_cards = conn.execute(
        "SELECT * FROM cards WHERE account_id IS NULL AND status = 'active' ORDER BY id"
    ).fetchall()
    return render_template(
        "admin/account_detail.html", account=acc, cards=cards, products=products,
        free_cards=free_cards, history=account_history(conn, account_id, limit=100),
        card_urls={c["token"]: card_url(base_url, c["token"]) for c in cards},
    )


@bp.route("/accounts/<int:account_id>/pin", methods=["POST"])
def account_reset_pin(account_id):
    conn = get_db()
    acc = _get_account(conn, account_id)
    try:
        pin = validate_pin(request.form.get("pin"))
        with transaction(conn):
            conn.execute(
                "UPDATE accounts SET pin_hash = ?, failed_logins = 0, locked_until = NULL WHERE id = ?",
                (hash_secret(pin), account_id),
            )
            _audit(conn, "account.pin_reset", f"#{account_id} {acc['name']}")
        flash("PIN updated.", "success")
    except BankError as e:
        flash(str(e), "error")
    return redirect(url_for("admin.account_detail", account_id=account_id))


@bp.route("/accounts/<int:account_id>/status", methods=["POST"])
def account_status(account_id):
    conn = get_db()
    acc = _get_account(conn, account_id)
    new_status = "frozen" if acc["status"] == "active" else "active"
    with transaction(conn):
        conn.execute("UPDATE accounts SET status = ? WHERE id = ?", (new_status, account_id))
        _audit(conn, f"account.{'freeze' if new_status == 'frozen' else 'unfreeze'}",
               f"#{account_id} {acc['name']}")
    flash(f"Account {'frozen' if new_status == 'frozen' else 'reactivated'}.", "success")
    return redirect(url_for("admin.account_detail", account_id=account_id))


@bp.route("/accounts/<int:account_id>/cards", methods=["POST"])
def account_add_card(account_id):
    conn = get_db()
    acc = _get_account(conn, account_id)
    try:
        with transaction(conn):
            if request.form.get("card_mode") == "existing":
                card_id, token = _find_or_register_card(conn, request.form.get("card_token"))
            else:
                card_id, token = register_card(conn)
            assign_card(conn, card_id, account_id)
            _audit(conn, "card.assign", f"card {token} -> #{account_id} {acc['name']}")
        flash("Card assigned.", "success")
    except BankError as e:
        flash(str(e), "error")
    return redirect(url_for("admin.account_detail", account_id=account_id))


# ---------- bank desk: cash, transfers, charges, credits and debits ----------

def _desk_accounts(conn):
    return conn.execute(
        "SELECT id, name, role, system_key, balance_cents, status FROM accounts "
        "WHERE role IN ('user', 'merchant') OR system_key = 'reserve' "
        "ORDER BY role = 'system' DESC, role, name"
    ).fetchall()


@bp.route("/money")
def money():
    conn = get_db()
    return render_template(
        "admin/money.html", accounts=_desk_accounts(conn), stats=bank_overview(conn),
        selected=request.args.get("account", type=int), actions=DESK_ACTIONS,
        keys={a: new_idempotency_key() for a in DESK_ACTIONS},
    )


@bp.route("/money/<action>", methods=["POST"])
def money_action(action):
    if action not in DESK_ACTIONS:
        abort(404, "Unknown action.")
    conn = get_db()
    form = request.form
    if action in ("credit", "debit") and form.get("direction") in ("credit", "debit"):
        action = form["direction"]  # one form with a credit/debit choice
    account_id = form.get("account_id", type=int)
    try:
        if account_id is None:
            raise BankError("Choose an account.")
        result = desk_action(
            conn, action=action, actor=g.account, account_id=account_id,
            amount_cents=parse_amount(form.get("amount")), reason=form.get("reason", ""),
            to_account_id=form.get("to_account_id", type=int), pin=form.get("pin"),
            idempotency_key=form.get("key") or None, ip=request.remote_addr,
            require_pin=current_app.config["REQUIRE_PIN"], card_token=form.get("card"),
        )
    except BankError as e:
        if not isinstance(e, PinError):
            with transaction(conn):
                _audit(conn, f"money.{action}.rejected", f"account #{account_id}: {e}")
        flash(f"{DESK_ACTIONS[action][0]} failed: {e}", "error")
        return redirect(url_for("admin.money", account=account_id))
    if result.duplicate:
        flash(f"Already recorded as ref #{result.tx_id}. Nothing was posted twice.", "info")
    else:
        flash(f"{DESK_ACTIONS[action][0]} recorded (ref #{result.tx_id}).", "success")
    return redirect(url_for("admin.account_detail", account_id=account_id))


# ---------- cards ----------

@bp.route("/cards", methods=["GET", "POST"])
def cards():
    conn = get_db()
    if request.method == "POST":
        try:
            token = request.form.get("token", "").strip()
            label = request.form.get("label", "")
            with transaction(conn):
                if token:
                    _, token = register_card(conn, token, label)
                    created = [token]
                else:
                    count = int(request.form.get("count") or 1)
                    if not 1 <= count <= 100:
                        raise BankError("You can register 1 to 100 cards at a time.")
                    created = [register_card(conn, None, label)[1] for _ in range(count)]
                _audit(conn, "card.register", ", ".join(created))
            flash(f"Registered {len(created)} card(s).", "success")
        except ValueError as e:  # BankError or bad count
            flash(str(e) if isinstance(e, BankError) else "Enter a valid number of cards.", "error")
        return redirect(url_for("admin.cards"))

    rows = conn.execute(
        "SELECT c.*, a.name AS account_name, a.role AS account_role "
        "FROM cards c LEFT JOIN accounts a ON a.id = c.account_id ORDER BY c.id DESC"
    ).fetchall()
    base_url = get_settings(conn)["base_url"]
    return render_template(
        "admin/cards.html", cards=rows,
        card_urls={c["token"]: card_url(base_url, c["token"]) for c in rows},
    )


def _get_card(conn, card_id):
    card = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    if card is None:
        abort(404, "Card not found.")
    return card


def _back(card):
    if request.form.get("back") == "account" and card["account_id"]:
        return redirect(url_for("admin.account_detail", account_id=card["account_id"]))
    return redirect(url_for("admin.cards"))


@bp.route("/cards/print")
def cards_print():
    """Printable sheet of card QR codes: one card (?id=) or all active cards."""
    conn = get_db()
    card_id = request.args.get("id", type=int)
    if card_id:
        rows = [_get_card(conn, card_id)]
    else:
        rows = conn.execute("SELECT * FROM cards WHERE status = 'active' ORDER BY id").fetchall()
    base_url = get_settings(conn)["base_url"]
    return render_template("admin/cards_print.html", cards=rows,
                           card_urls={c["token"]: card_url(base_url, c["token"]) for c in rows})


@bp.route("/cards/<int:card_id>/status", methods=["POST"])
def card_status(card_id):
    conn = get_db()
    card = _get_card(conn, card_id)
    new_status = "blocked" if card["status"] == "active" else "active"
    with transaction(conn):
        conn.execute("UPDATE cards SET status = ? WHERE id = ?", (new_status, card_id))
        _audit(conn, f"card.{'block' if new_status == 'blocked' else 'unblock'}", f"card {card['token']}")
    flash(f"Card {new_status}.", "success")
    return _back(card)


@bp.route("/cards/<int:card_id>/unassign", methods=["POST"])
def card_unassign(card_id):
    conn = get_db()
    card = _get_card(conn, card_id)
    response = _back(card)  # compute before account_id is cleared
    with transaction(conn):
        conn.execute("UPDATE cards SET account_id = NULL, assigned_at = NULL WHERE id = ?", (card_id,))
        _audit(conn, "card.unassign", f"card {card['token']} from #{card['account_id']}")
    flash("Card unassigned.", "success")
    return response


# ---------- settings and audit ----------

@bp.route("/settings", methods=["GET", "POST"])
def settings_page():
    conn = get_db()
    if request.method == "POST":
        base_url = request.form.get("base_url", "").strip().rstrip("/")
        party_name = request.form.get("party_name", "").strip()
        try:
            bps = parse_fee_percent(request.form.get("fee_percent"))
            try:
                cb_bps = parse_fee_percent(request.form.get("cashback_percent"))
            except BankError:
                raise BankError("Cashback must be between 0 and 20%, with at most 2 decimals.") from None
            if not base_url.startswith(("http://", "https://")) or len(base_url) > 200:
                raise BankError("Base URL must start with http:// or https://")
            if not party_name or len(party_name) > 60:
                raise BankError("Party name is required (max 60 characters).")
            with transaction(conn):
                set_setting(conn, "base_url", base_url)
                set_setting(conn, "party_name", party_name)
                set_setting(conn, "merchant_fee_bps", str(bps))
                set_setting(conn, "cashback_bps", str(cb_bps))
                _audit(conn, "settings.update",
                       f"base_url={base_url}, party_name={party_name}, merchant_fee={bps / 100:g}%, cashback={cb_bps / 100:g}%")
            flash("Settings saved.", "success")
            return redirect(url_for("admin.settings_page"))
        except BankError as e:
            flash(str(e), "error")
    return render_template("admin/settings.html", current=get_settings(conn))


@bp.route("/site", methods=["POST"])
def site_toggle():
    """Open or close the site for participants. Admins can always get in."""
    conn = get_db()
    now_open = get_settings(conn).get("site_open", "1") == "1"
    with transaction(conn):
        set_setting(conn, "site_open", "0" if now_open else "1")
        _audit(conn, "site.close" if now_open else "site.open", "")
    flash("The site is now CLOSED for participants." if now_open else "The site is now OPEN.",
          "info" if now_open else "success")
    return redirect(request.referrer if _same_site(request.referrer) else url_for("admin.dashboard"))


def _same_site(url):
    return bool(url) and url.startswith(request.host_url)


MIN_ADMIN_PASSWORD = 10


@bp.route("/password", methods=["GET", "POST"])
def password():
    """The logged-in admin changes their own password (current one required)."""
    conn = get_db()
    if request.method == "POST":
        current, new, repeat = (request.form.get(k, "") for k in ("current", "new", "repeat"))
        admin = conn.execute("SELECT * FROM accounts WHERE id = ?", (g.account["id"],)).fetchone()
        if not verify_secret(admin["password_hash"], current):
            with transaction(conn):
                _audit(conn, "admin.password_change.rejected", "wrong current password")
            flash("Current password is wrong.", "error")
        elif len(new) < MIN_ADMIN_PASSWORD:
            flash(f"New password must be at least {MIN_ADMIN_PASSWORD} characters.", "error")
        elif new != repeat:
            flash("The two new passwords don't match.", "error")
        elif new == current:
            flash("The new password must be different.", "error")
        else:
            with transaction(conn):
                conn.execute("UPDATE accounts SET password_hash = ? WHERE id = ?",
                             (hash_secret(new), admin["id"]))
                _audit(conn, "admin.password_change", f"admin {admin['username']}")
            flash("Password changed.", "success")
            return redirect(url_for("admin.dashboard"))
    return render_template("admin/password.html", min_length=MIN_ADMIN_PASSWORD), \
        (400 if request.method == "POST" else 200)


@bp.route("/audit")
def audit_log():
    rows = get_db().execute(
        "SELECT l.*, a.name AS actor_name FROM audit_logs l "
        "LEFT JOIN accounts a ON a.id = l.actor_id ORDER BY l.id DESC LIMIT 300"
    ).fetchall()
    return render_template("admin/audit.html", rows=rows)
