"""Party Bank maintenance commands.

    python manage.py init                   create the database (safe to repeat)
    python manage.py seed-demo              add demo accounts, cards and products
    python manage.py set-admin-password     set/reset an admin password (creates the admin if missing)
    python manage.py reset                  DELETE the database and start empty
"""

import argparse
import getpass
import os
import sys

from partybank import create_app
from partybank.banking import BankError, audit, create_admin, hash_secret
from partybank.db import connect, transaction
from partybank.seed import DEMO_ACCOUNTS, DEMO_ADMIN, ensure_admin, seed_demo


def cmd_init(app, _args):
    created = ensure_admin(app.config["DATABASE"])
    print(f"Database ready: {app.config['DATABASE']}")
    if created:
        print(f"Created admin  username: {created[0]}  password: {created[1]}")


def cmd_seed_demo(app, _args):
    if not seed_demo(app.config["DATABASE"]):
        print("Demo data already present; nothing changed.")
        return
    print("Demo data created.")
    print(f"  Admin: {DEMO_ADMIN[0]} / {DEMO_ADMIN[1]}  (only if no admin existed before)")
    for name, role, pin, _, token in DEMO_ACCOUNTS:
        print(f"  {role:8} {name:15} card {token}  PIN {pin}")


def cmd_set_admin_password(app, args):
    password = os.environ.get("PARTYBANK_ADMIN_PASSWORD") or getpass.getpass("New admin password: ")
    conn = connect(app.config["DATABASE"])
    try:
        with transaction(conn):
            row = conn.execute(
                "SELECT id FROM accounts WHERE role = 'admin' AND username = ?", (args.username,)
            ).fetchone()
            if row is None:
                admin_id = create_admin(conn, username=args.username, password=password)
                audit(conn, "admin.create", f"admin {args.username} created via manage.py", admin_id)
                print(f"Created admin '{args.username}'.")
            else:
                if len(password) < 6:
                    raise BankError("Password must be at least 6 characters.")
                conn.execute(
                    "UPDATE accounts SET password_hash = ?, failed_logins = 0, locked_until = NULL "
                    "WHERE id = ?", (hash_secret(password), row["id"]),
                )
                audit(conn, "admin.password_reset", f"admin {args.username} via manage.py", row["id"])
                print(f"Password updated for '{args.username}'.")
    except BankError as e:
        sys.exit(f"Error: {e}")
    finally:
        conn.close()


def cmd_reset(app, args):
    path = app.config["DATABASE"]
    if not args.yes:
        answer = input(f"Delete ALL data in {path}? Type 'delete' to confirm: ")
        if answer.strip() != "delete":
            print("Cancelled.")
            return
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    print("Database deleted. Run 'python manage.py init' or 'python manage.py seed-demo'.")


def main():
    parser = argparse.ArgumentParser(description="Party Bank maintenance")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("seed-demo")
    p = sub.add_parser("set-admin-password")
    p.add_argument("--username", default="admin")
    p = sub.add_parser("reset")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()

    app = create_app()  # creates the schema if needed
    {"init": cmd_init, "seed-demo": cmd_seed_demo,
     "set-admin-password": cmd_set_admin_password, "reset": cmd_reset}[args.command](app, args)


if __name__ == "__main__":
    main()
