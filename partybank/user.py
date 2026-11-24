"""Customer (user role) pages: balance, history and sending money."""

from flask import (Blueprint, flash, g, jsonify, redirect, render_template, request,
                   url_for)

from .auth import confirm_pin, role_required
from .banking import (BankError, account_history, audit, balance_snapshot, format_account_number,
                      format_money, new_idempotency_key, parse_account_number, parse_amount,
                      post_transaction, MAX_NOTE_LENGTH)
from .db import get_db, transaction

bp = Blueprint("user", __name__, url_prefix="/me")


@bp.before_request
@role_required("user")
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


# ---------- send money: form -> confirm (with PIN) -> post ----------

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


def _send_form(form, status=200):
    return render_template("user/send.html", account=g.account, form=form,
                           max_note=MAX_NOTE_LENGTH), status


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
    return render_template(
        "user/send_confirm.html", account=g.account, recipient=recipient, amount_cents=amount,
        note=(form.get("note") or "").strip()[:MAX_NOTE_LENGTH], key=new_idempotency_key(),
    )


@bp.route("/send/confirm", methods=["POST"])
def send_confirm():
    form = request.form
    conn = get_db()
    note = (form.get("note") or "").strip()[:MAX_NOTE_LENGTH]
    key = form.get("key") or None
    try:
        to_id = int(form.get("to_id", ""))
        amount = int(form.get("amount_cents", ""))
        recipient = _recipient(conn, to_id)
    except (ValueError, BankError) as e:
        flash(str(e) if isinstance(e, BankError) else "Invalid payment. Start again.", "error")
        return redirect(url_for("user.send"))

    pin_error = confirm_pin(conn, g.account, form.get("pin", ""), "payment PIN")
    if pin_error:
        flash(pin_error, "error")
        return render_template(
            "user/send_confirm.html", account=g.account, recipient=recipient,
            amount_cents=amount, note=note, key=key,
        ), 401

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
        return _send_form({"to": format_account_number(to_id), "amount": f"{amount / 100:.2f}",
                           "note": note}, 400)

    if result.duplicate:
        flash(f"This payment was already sent (ref #{result.tx_id}). It was not sent again.", "info")
    else:
        flash(f"Sent {format_money(amount)} to {recipient['name']} (ref #{result.tx_id}). "
              f"New balance: {format_money(result.from_balance_cents)}.", "success")
    return redirect(url_for("user.dashboard"))
