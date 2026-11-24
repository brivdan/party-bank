"""Login, logout, session loading and role-based access control."""

from functools import wraps

from flask import (Blueprint, abort, flash, g, redirect, render_template, request,
                   session, url_for)

from .banking import (LOCK_MINUTES, audit, check_pin, is_locked, normalize_token,
                      record_failed_secret, verify_secret)
from .db import get_db, transaction

bp = Blueprint("auth", __name__)

HOME_ENDPOINT = {"admin": "admin.dashboard", "user": "user.dashboard", "merchant": "merchant.dashboard"}



# ---------- session handling ----------

@bp.before_app_request
def load_logged_in_account():
    """Re-validate the session on every request so frozen accounts or
    blocked cards are logged out immediately."""
    g.account = None
    account_id = session.get("account_id")
    if account_id is None:
        return
    conn = get_db()
    acc = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    valid = acc is not None and acc["status"] == "active" and acc["role"] == session.get("role")
    if valid and session.get("card_id") is not None:
        card = conn.execute("SELECT * FROM cards WHERE id = ?", (session["card_id"],)).fetchone()
        valid = card is not None and card["status"] == "active" and card["account_id"] == account_id
    if valid:
        g.account = acc
    else:
        session.clear()


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.account is None:
                target = "auth.admin_login" if roles == ("admin",) else "auth.login"
                return redirect(url_for(target, next=request.full_path))
            if g.account["role"] not in roles:
                abort(403, "You do not have access to this page.")
            return view(*args, **kwargs)
        return wrapped
    return decorator


def _safe_next(target):
    # Only allow local paths to prevent open redirects.
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


def _start_session(acc, card_id=None):
    session.clear()  # prevents session fixation; a new CSRF token is issued lazily
    session.permanent = True
    session["account_id"] = acc["id"]
    session["role"] = acc["role"]
    if card_id is not None:
        session["card_id"] = card_id


def _is_locked(conn, account_id):
    return is_locked(conn, account_id)


def _record_failure(conn, acc, what):
    record_failed_secret(conn, acc["id"], what, request.remote_addr)


def _record_success(conn, acc, what):
    with transaction(conn):
        conn.execute("UPDATE accounts SET failed_logins = 0, locked_until = NULL WHERE id = ?", (acc["id"],))
        audit(conn, "login.success", what, acc["id"], request.remote_addr)


def confirm_pin(conn, acc, pin, what):
    """Re-check the PIN of a logged-in account before a payment.
    Returns None when correct, otherwise an error message."""
    return check_pin(conn, acc["id"], pin, what, request.remote_addr)


# ---------- card + PIN login (users and merchants) ----------

@bp.route("/login", methods=["GET", "POST"])
def login():
    card_input = request.values.get("card", "")
    if request.method == "POST":
        conn = get_db()
        token = normalize_token(card_input)
        pin = request.form.get("pin", "")
        row = conn.execute(
            "SELECT c.id AS card_id, c.status AS card_status, a.* "
            "FROM cards c JOIN accounts a ON a.id = c.account_id WHERE c.token = ?",
            (token,),
        ).fetchone() if token else None

        error = "Invalid card or PIN."
        if row is None or row["role"] not in ("user", "merchant"):
            pass
        elif _is_locked(conn, row["id"]):
            error = f"Too many failed attempts. Try again in {LOCK_MINUTES} minutes."
        elif not verify_secret(row["pin_hash"], pin):
            _record_failure(conn, row, f"card {token}")
        elif row["card_status"] != "active":
            error = "This card is blocked. Please contact the bank desk."
        elif row["status"] != "active":
            error = "This account is frozen. Please contact the bank desk."
        else:
            _record_success(conn, row, f"card {token}")
            _start_session(row, card_id=row["card_id"])
            return redirect(_safe_next(request.args.get("next")) or url_for(HOME_ENDPOINT[row["role"]]))
        flash(error, "error")
    return render_template("login.html", card=card_input), 200 if request.method == "GET" else 401


@bp.route("/card/<token>")
def card_landing(token):
    """Target of the NFC tag / QR URL. Identifies the card only; never
    reveals account data and never authorizes anything without the PIN."""
    acc = g.account
    if acc is not None and acc["role"] == "merchant":
        # A merchant's phone read a customer card (NFC tap or camera QR scan):
        # hand the card to the payment terminal. The customer still needs a PIN.
        return redirect(url_for("merchant.card_tapped", token=normalize_token(token)))
    if acc is not None and session.get("card_id") is not None:
        card = get_db().execute("SELECT id FROM cards WHERE token = ?", (normalize_token(token),)).fetchone()
        if card is not None and card["id"] == session["card_id"]:
            return redirect(url_for(HOME_ENDPOINT[acc["role"]]))
    return render_template("login.html", card=normalize_token(token), from_card=True)


# ---------- admin username + password login ----------

@bp.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        conn = get_db()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        acc = conn.execute(
            "SELECT * FROM accounts WHERE role = 'admin' AND username = ?", (username,)
        ).fetchone()
        error = "Invalid username or password."
        if acc is None:
            pass
        elif _is_locked(conn, acc["id"]):
            error = f"Too many failed attempts. Try again in {LOCK_MINUTES} minutes."
        elif not verify_secret(acc["password_hash"], password):
            _record_failure(conn, acc, f"admin {username}")
        elif acc["status"] != "active":
            error = "This admin account is disabled."
        else:
            _record_success(conn, acc, f"admin {username}")
            _start_session(acc)
            return redirect(_safe_next(request.args.get("next")) or url_for("admin.dashboard"))
        flash(error, "error")
        return render_template("admin_login.html", username=username), 401
    return render_template("admin_login.html", username="")


@bp.route("/logout", methods=["POST"])
def logout():
    if g.account is not None:
        conn = get_db()
        with transaction(conn):
            audit(conn, "logout", "", g.account["id"], request.remote_addr)
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))
