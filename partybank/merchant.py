"""Merchant pages: dashboard, product management and the payment terminal."""

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)

from .auth import role_required
from .banking import (BankError, PinError, account_history, audit, balance_snapshot,
                      cashback_bps, compute_fee, create_order, customer_for_card, fee_bps, format_money,
                      merchant_stats, normalize_token, order_items, parse_amount, pay_order,
                      validate_product)
from .db import get_db, transaction

bp = Blueprint("merchant", __name__, url_prefix="/merchant")

OPEN_ORDER_MINUTES = 30  # a tapped card is matched to the latest open order this recent


@bp.before_request
@role_required("user", "merchant")  # merged roles: every participant can sell
def require_merchant():
    return None


def _audit(conn, action, details=""):
    audit(conn, action, details, g.account["id"], request.remote_addr)


# ---------- dashboard ----------

@bp.route("/")
def dashboard():
    # Merged roles: the participant dashboard (/me/) replaces this page.
    # The old merchant dashboard is kept below but no longer linked.
    return redirect(url_for("user.dashboard"))


def merchant_dashboard_legacy():
    conn = get_db()
    return render_template(
        "merchant/dashboard.html", account=g.account,
        stats=merchant_stats(conn, g.account["id"]), fee_bps=fee_bps(conn),
        history=account_history(conn, g.account["id"], limit=30),
        snapshot=balance_snapshot(conn, g.account["id"]),
    )


@bp.route("/balance.json")
def balance_json():
    return jsonify(balance_snapshot(get_db(), g.account["id"]))


# ---------- products ----------

def _get_product(conn, product_id):
    p = conn.execute(
        "SELECT * FROM products WHERE id = ? AND merchant_id = ?", (product_id, g.account["id"])
    ).fetchone()
    if p is None:
        abort(404, "Product not found.")
    return p


@bp.route("/products", methods=["GET", "POST"])
def products():
    conn = get_db()
    form = {}
    if request.method == "POST":
        form = request.form
        try:
            name, price = validate_product(form.get("name"), form.get("price"))
            with transaction(conn):
                conn.execute("INSERT INTO products (merchant_id, name, price_cents) VALUES (?, ?, ?)",
                             (g.account["id"], name, price))
                _audit(conn, "product.create", f"{name} {format_money(price)}")
            flash(f"Added {name}.", "success")
            return redirect(url_for("merchant.products"))
        except BankError as e:
            flash(str(e), "error")
    rows = conn.execute(
        "SELECT * FROM products WHERE merchant_id = ? ORDER BY active DESC, name", (g.account["id"],)
    ).fetchall()
    return render_template("merchant/products.html", products=rows, form=form)


@bp.route("/products/<int:product_id>/edit", methods=["GET", "POST"])
def product_edit(product_id):
    conn = get_db()
    product = _get_product(conn, product_id)
    if request.method == "POST":
        try:
            name, price = validate_product(request.form.get("name"), request.form.get("price"))
            active = 1 if request.form.get("active") else 0
            with transaction(conn):
                conn.execute("UPDATE products SET name = ?, price_cents = ?, active = ? WHERE id = ?",
                             (name, price, active, product_id))
                _audit(conn, "product.update",
                       f"#{product_id} {product['name']} {format_money(product['price_cents'])} -> "
                       f"{name} {format_money(price)}{'' if active else ' (inactive)'}")
            flash(f"Saved {name}.", "success")
            return redirect(url_for("merchant.products"))
        except BankError as e:
            flash(str(e), "error")
    return render_template("merchant/product_edit.html", product=product)


@bp.route("/products/<int:product_id>/toggle", methods=["POST"])
def product_toggle(product_id):
    conn = get_db()
    product = _get_product(conn, product_id)
    active = 0 if product["active"] else 1
    with transaction(conn):
        conn.execute("UPDATE products SET active = ? WHERE id = ?", (active, product_id))
        _audit(conn, "product.activate" if active else "product.deactivate", product["name"])
    flash(f"{product['name']} {'is on sale again' if active else 'is hidden from the terminal'}.", "success")
    return redirect(url_for("merchant.products"))


@bp.route("/products/<int:product_id>/delete", methods=["POST"])
def product_delete(product_id):
    conn = get_db()
    product = _get_product(conn, product_id)
    with transaction(conn):
        used = conn.execute(
            "SELECT 1 FROM order_items WHERE product_id = ? UNION ALL "
            "SELECT 1 FROM transactions WHERE product_id = ? LIMIT 1", (product_id, product_id),
        ).fetchone()
        if used:
            # Sales history must keep pointing at the product, so hide it instead.
            conn.execute("UPDATE products SET active = 0 WHERE id = ?", (product_id,))
            _audit(conn, "product.deactivate", f"{product['name']} (delete requested, has sales)")
            message = f"{product['name']} has sales history, so it was deactivated instead of deleted."
        else:
            conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
            _audit(conn, "product.delete", product["name"])
            message = f"Deleted {product['name']}."
    flash(message, "success")
    return redirect(url_for("merchant.products"))


# ---------- payment terminal ----------

@bp.route("/terminal")
def terminal():
    conn = get_db()
    products = conn.execute(
        "SELECT * FROM products WHERE merchant_id = ? AND active = 1 ORDER BY name", (g.account["id"],)
    ).fetchall()
    recent = conn.execute(
        "SELECT o.*, a.name AS customer_name FROM orders o LEFT JOIN accounts a ON a.id = o.customer_id "
        "WHERE o.merchant_id = ? ORDER BY o.id DESC LIMIT 10", (g.account["id"],),
    ).fetchall()
    return render_template("merchant/terminal.html", products=products, recent=recent)


@bp.route("/orders", methods=["POST"])
def order_create():
    conn = get_db()
    form = request.form
    try:
        quantities = {}
        for key, value in form.items():
            if key.startswith("qty_") and value.strip():
                try:
                    quantities[int(key[4:])] = int(value)
                except ValueError:
                    raise BankError("Quantities must be whole numbers.") from None
        custom = parse_amount(form.get("custom_amount"), allow_zero=True)
        order_id = create_order(conn, merchant=g.account, quantities=quantities,
                                custom_cents=custom, custom_label=form.get("custom_label"))
        session.pop("pending_transfer", None)  # the next card tap is for this order
    except BankError as e:
        flash(str(e), "error")
        return redirect(url_for("merchant.terminal"))
    return redirect(url_for("merchant.order", order_id=order_id))


def _get_order(conn, order_id):
    order = conn.execute(
        "SELECT o.*, a.name AS customer_name FROM orders o LEFT JOIN accounts a ON a.id = o.customer_id "
        "WHERE o.id = ? AND o.merchant_id = ?", (order_id, g.account["id"]),
    ).fetchone()
    if order is None:
        abort(404, "Order not found.")
    return order


def _order_context(conn, order):
    fee = order["fee_cents"] if order["status"] == "paid" else compute_fee(order["total_cents"], fee_bps(conn))
    cashback = (order["cashback_cents"] if order["status"] == "paid"
                else compute_fee(order["total_cents"], cashback_bps(conn)))
    return {"order": order, "items": order_items(conn, order["id"]), "fee_cents": fee,
            "cashback_cents": cashback}


@bp.route("/orders/<int:order_id>")
def order(order_id):
    """Open order: identify the customer. Paid order: receipt."""
    conn = get_db()
    return render_template("merchant/order.html", **_order_context(conn, _get_order(conn, order_id)))


@bp.route("/orders/<int:order_id>/cancel", methods=["POST"])
def order_cancel(order_id):
    conn = get_db()
    with transaction(conn):
        changed = conn.execute(
            "UPDATE orders SET status = 'cancelled' WHERE id = ? AND merchant_id = ? AND status = 'open'",
            (order_id, g.account["id"]),
        ).rowcount
    flash("Order cancelled." if changed else "Only open orders can be cancelled.",
          "info" if changed else "error")
    return redirect(url_for("merchant.terminal"))


@bp.route("/orders/<int:order_id>/pay", methods=["GET", "POST"])
def order_pay(order_id):
    """Customer authorization: the identified customer enters their PIN on the terminal."""
    conn = get_db()
    order = _get_order(conn, order_id)
    token = normalize_token(request.values.get("card"))
    if order["status"] != "open":
        return redirect(url_for("merchant.order", order_id=order_id))
    try:
        customer_for_card(conn, token)  # identification only; reveals nothing about the account
    except BankError as e:
        flash(f"{e} Try another card.", "error")
        return redirect(url_for("merchant.order", order_id=order_id))

    if request.method == "GET":
        return render_template("merchant/pay.html", token=token, **_order_context(conn, order))
    return _pay(conn, order_id, token)


def _pay(conn, order_id, token):
    try:
        result = pay_order(conn, order_id=order_id, card_token=token, pin=request.form.get("pin", ""),
                           actor=g.account, ip=request.remote_addr,
                           require_pin=current_app.config["REQUIRE_PIN"])
    except BankError as e:
        if not isinstance(e, PinError):
            with transaction(conn):
                _audit(conn, "payment.declined", f"order #{order_id} card {token}: {e}")
        return render_template("merchant/pay.html", token=token, declined=str(e),
                               **_order_context(conn, _get_order(conn, order_id))), \
            401 if isinstance(e, PinError) else 400
    if result.duplicate:
        flash("This order was already paid. It was not charged again.", "info")
    return redirect(url_for("merchant.order", order_id=order_id))


@bp.route("/card/<token>")
def card_tapped(token):
    """A card was tapped/scanned on a logged-in participant's phone.

    1. A transfer is waiting for confirmation -> the sender's own card confirms it.
    2. An open order exists -> the customer's card pays it (with a PIN only if
       REQUIRE_PIN). The seller stays logged in.
    3. Otherwise: own card -> dashboard; other card -> explain.
    """
    conn = get_db()
    token = normalize_token(token)
    if session.get("pending_transfer"):
        return redirect(url_for("user.send_scan", token=token))
    order = conn.execute(
        "SELECT id FROM orders WHERE merchant_id = ? AND status = 'open' "
        "AND created_at > datetime('now', ?) ORDER BY id DESC LIMIT 1",
        (g.account["id"], f"-{OPEN_ORDER_MINUTES} minutes"),
    ).fetchone()
    if order is None:
        own = conn.execute("SELECT 1 FROM cards WHERE token = ? AND account_id = ?",
                           (token, g.account["id"])).fetchone()
        if own:
            return redirect(url_for("user.dashboard"))
        flash("Card read, but there is no open order. Create the order first, then tap the card.", "error")
        return redirect(url_for("merchant.terminal"))
    if current_app.config["REQUIRE_PIN"]:
        return redirect(url_for("merchant.order_pay", order_id=order["id"], card=token))
    # No-PIN mode: the tap is the customer's authorization. Validate the card,
    # then pay and redirect (the order can only be paid once).
    try:
        customer_for_card(conn, token)
    except BankError as e:
        flash(f"{e} Try another card.", "error")
        return redirect(url_for("merchant.order", order_id=order["id"]))
    return _pay(conn, order["id"], token)
