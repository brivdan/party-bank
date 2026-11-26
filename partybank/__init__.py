"""Party Bank: a local NFC/PIN payment system for a party with fictional currency."""

import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import segno
from flask import Flask, abort, g, redirect, render_template, request, session, url_for

from . import db
from .banking import format_account_number, format_money, format_token


# False: a card tap (or typed card ID) is enough to log in, approve a purchase
# or confirm a transfer. True: the original PIN checks apply everywhere.
# Admin username/password login is not affected.
REQUIRE_PIN = False


def _load_secret_key(instance_path):
    """Persist the session key so logins survive a server restart."""
    if os.environ.get("PARTYBANK_SECRET_KEY"):
        return os.environ["PARTYBANK_SECRET_KEY"]
    key_file = Path(instance_path) / "secret_key"
    if not key_file.exists():
        key_file.write_text(secrets.token_hex(32), encoding="utf-8")
    return key_file.read_text(encoding="utf-8").strip()


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)
    app.config.from_mapping(
        DATABASE=os.environ.get("PARTYBANK_DB") or os.path.join(app.instance_path, "partybank.db"),
        SECRET_KEY=_load_secret_key(app.instance_path),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        REQUIRE_PIN=REQUIRE_PIN,
    )
    if test_config:
        app.config.update(test_config)

    db.init_db(app.config["DATABASE"])
    app.teardown_appcontext(db.close_db)

    _register_csrf(app)
    _register_template_helpers(app)

    from . import admin, auth, merchant, user
    app.register_blueprint(auth.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(user.bp)
    app.register_blueprint(merchant.bp)

    # Registered after the blueprints, so g.account is already loaded.
    @app.before_request
    def site_switch():
        """When an admin turns the site off, only admins (and the admin login
        page) get through. Card logins, payments and transfers all stop."""
        if request.endpoint in (None, "static", "auth.admin_login", "auth.logout"):
            return None
        acc = g.get("account")
        if acc is not None and acc["role"] == "admin":
            return None
        if db.get_settings(db.get_db()).get("site_open", "1") == "1":
            return None
        return render_template("closed.html"), 503

    @app.route("/")
    def index():
        acc = g.get("account")
        if acc is None:
            return redirect(url_for("auth.login"))
        return redirect(url_for(auth.HOME_ENDPOINT[acc["role"]]))

    for code in (400, 403, 404):
        app.register_error_handler(code, lambda e, code=code: (
            render_template("error.html", code=code, message=e.description), code))

    return app


def _register_csrf(app):
    def csrf_token():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return session["csrf_token"]

    @app.before_request
    def check_csrf():
        if request.method == "POST":
            sent = request.form.get("csrf_token", "")
            expected = session.get("csrf_token", "")
            if not expected or not hmac.compare_digest(sent, expected):
                abort(400, "Form expired or invalid. Go back, reload the page and try again.")

    app.jinja_env.globals["csrf_token"] = csrf_token


def _register_template_helpers(app):
    def settings():
        if "settings" not in g:
            g.settings = db.get_settings(db.get_db())
        return g.settings

    @app.template_filter("money")
    def money_filter(cents):
        return format_money(cents, settings().get("currency_symbol", "A$"))

    @app.template_filter("card")
    def card_filter(token):
        return format_token(token or "")

    app.template_filter("acctno")(format_account_number)

    @app.template_filter("qr")
    def qr_filter(text):
        """QR code as an inline SVG data URI (no image files, works offline)."""
        return segno.make(text, error="m").svg_data_uri(scale=5, border=2)

    @app.template_filter("localtime")
    def localtime_filter(value):
        # SQLite stores UTC 'YYYY-MM-DD HH:MM:SS'; show it in the server's local time.
        if not value:
            return ""
        dt = datetime.fromisoformat(value).replace(tzinfo=timezone.utc).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M")

    @app.context_processor
    def inject_globals():
        return {"settings": settings(), "current_account": g.get("account")}
