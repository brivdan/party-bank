"""Participant pages: dashboard, history and sending money.

Users and merchants are merged: every non-admin account uses these pages,
plus the terminal and products pages in merchant.py.
"""

import time

from flask import (Blueprint, current_app, flash, g, jsonify, redirect, render_template, request,
                   session, url_for)

from .auth import confirm_pin, role_required
from .banking import (BankError, PinError, account_history, audit, balance_snapshot,
                      card_owner_check, format_account_number, format_money, new_idempotency_key,
                      parse_account_number, parse_amount, participant_stats, post_transaction,
                      MAX_NOTE_LENGTH)
from .db import get_db, transaction

bp = Blueprint("user", __name__, url_prefix="/me")

PENDING_TRANSFER_SECONDS = 10 * 60  # a prepared transfer waits this long for the card scan


@bp.before_request
@role_required("user", "merchant")
def require_user():
    return None


@bp.route("/")
def dashboard():
    conn = get_db()
    cards = conn.execute(
        "SELECT * FROM cards WHERE account_id = ? ORDER BY id", (g.account["id"],)
    ).fetchall()
    return render_template(
        "user/dashboard.html", account=g.account, cards=cards,
        history=account_history(conn, g.account["id"], limit=10),
        snapshot=balance_snapshot(conn, g.account["id"]),
        stats=participant_stats(conn, g.account["id"]),
    )


@bp.route("/history")
def history():
    direction = request.args.get("dir")
    if direction not in ("in", "out"):
        direction = None
    conn = get_db()
    return render_template(
        "user/history.html", account=g.account, direction=direction,
        history=account_history(conn, g.account["id"], limit=500, direction=direction),
    )


@bp.route("/balance.json")
def balance_json():
    return jsonify(balance_snapshot(get_db(), g.account["id"]))


# ---------- send money: form -> confirm (PIN, or scan own card) -> post ----------

def _recipient(conn, account_id):
    acc = conn.execute(
        "SELECT id, name, role, status FROM accounts WHERE id = ? AND role IN ('user', 'merchant')",
        (account_id,),
    ).fetchone()
    if acc is None or acc["status"] != "active":
        raise BankError(f"No active account with number {format_account_number(account_id)}.")
    if acc["id"] == g.account["id"]:
        raise BankError("That is your own account.")
    return acc


def _recipients(conn):
    """Dropdown: every other active participant. No admins or bank accounts."""
    return conn.execute(
        "SELECT id, name FROM accounts WHERE role IN ('user', 'merchant') AND status = 'active' "
        "AND id <> ? ORDER BY name COLLATE NOCASE", (g.account["id"],),
    ).fetchall()


def _send_form(form, status=200):
    return render_template("user/send.html", account=g.account, form=form,
                           recipients=_recipients(get_db()), max_note=MAX_NOTE_LENGTH), status


def _pending_transfer():
    p = session.get("pending_transfer")
    if p and p.get("from_id") == g.account["id"] and time.time() - p.get("at", 0) < PENDING_TRANSFER_SECONDS:
        return p
    session.pop("pending_transfer", None)
    return None


def _render_confirm(conn, p, status=200):
    return render_template(
        "user/send_confirm.html", account=g.account, recipient=_recipient(conn, p["to_id"]),
        amount_cents=p["amount_cents"], note=p["note"], key=p["key"],
        require_pin=current_app.config["REQUIRE_PIN"],
    ), status


@bp.route("/send", methods=["GET", "POST"])
def send():
    if request.method == "GET":
        return _send_form({"to": request.args.get("to", "")})
    form = request.form
    conn = get_db()
    try:
        recipient = _recipient(conn, parse_account_number(form.get("to")))
        amount = parse_amount(form.get("amount"))
        if amount > g.account["balance_cents"]:
            raise BankError(f"Insufficient funds. Your balance is {format_money(g.account['balance_cents'])}.")
    except BankError as e:
        flash(str(e), "error")
        return _send_form(form, 400)
    # Remember the prepared transfer so a card tap (/card/<token>) can confirm it.
    pending = {"from_id": g.account["id"], "to_id": recipient["id"], "amount_cents": amount,
               "note": (form.get("note") or "").strip()[:MAX_NOTE_LENGTH],
               "key": new_idempotency_key(), "at": time.time()}
    session["pending_transfer"] = pending
    return _render_confirm(conn, pending)


@bp.route("/send/confirm", methods=["GET", "POST"])
def send_confirm():
    conn = get_db()
    if request.method == "GET":  # e.g. back here after scanning the wrong card
        p = _pending_transfer()
        return _render_confirm(conn, p) if p else redirect(url_for("user.send"))

    form = request.form
    note = (form.get("note") or "").strip()[:MAX_NOTE_LENGTH]
    key = form.get("key") or None
    try:
        to_id = int(form.get("to_id", ""))
        amount = int(form.get("amount_cents", ""))
        recipient = _recipient(conn, to_id)
    except (ValueError, BankError) as e:
        flash(str(e) if isinstance(e, BankError) else "Invalid payment. Start again.", "error")
        return redirect(url_for("user.send"))

    if current_app.config["REQUIRE_PIN"]:
        auth_error = confirm_pin(conn, g.account, form.get("pin", ""), "payment PIN")
    else:
        try:
            card_owner_check(conn, form.get("card", ""), g.account["id"])
            auth_error = None
        except PinError as e:
            auth_error = str(e)
    if auth_error:
        flash(auth_error, "error")
        return render_template(
            "user/send_confirm.html", account=g.account, recipient=recipient,
            amount_cents=amount, note=note, key=key, require_pin=current_app.config["REQUIRE_PIN"],
        ), 401
    return _execute_transfer(conn, to_id, amount, note, key, recipient["name"])


@bp.route("/send/scan/<token>")
def send_scan(token):
    """The sender tapped/scanned their own card to confirm the pending transfer."""
    conn = get_db()
    p = _pending_transfer()
    if p is None:
        flash("No transfer is waiting for confirmation.", "error")
        return redirect(url_for("user.send"))
    try:
        card_owner_check(conn, token, g.account["id"])
        recipient = _recipient(conn, p["to_id"])
    except BankError as e:
        flash(str(e), "error")
        return redirect(url_for("user.send_confirm"))
    return _execute_transfer(conn, p["to_id"], p["amount_cents"], p["note"], p["key"], recipient["name"])


def _execute_transfer(conn, to_id, amount, note, key, recipient_name):
    try:
        result = post_transaction(
            conn, kind="transfer", from_id=g.account["id"], to_id=to_id, amount_cents=amount,
            actor=g.account, description=note, idempotency_key=key,
        )
    except BankError as e:
        with transaction(conn):
            audit(conn, "transfer.rejected",
                  f"to #{to_id} {format_money(amount)}: {e}", g.account["id"], request.remote_addr)
        flash(str(e), "error")
        session.pop("pending_transfer", None)
        return _send_form({"to": str(to_id), "amount": str(amount // 100), "note": note}, 400)

    session.pop("pending_transfer", None)
    if result.duplicate:
        flash(f"This payment was already sent (ref #{result.tx_id}). It was not sent again.", "info")
    else:
        flash(f"Sent {format_money(amount)} to {recipient_name} (ref #{result.tx_id}). "
              f"New balance: {format_money(result.from_balance_cents)}.", "success")
    return redirect(url_for("user.dashboard"))
