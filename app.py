"""Start the Party Bank server on port 5000, reachable from the local Wi-Fi network."""

import os

from partybank import create_app
from partybank.db import detect_lan_ip, get_settings, connect
from partybank.seed import ensure_admin

app = create_app()

if __name__ == "__main__":
    db_path = app.config["DATABASE"]
    created = ensure_admin(db_path)
    if created:
        print("=" * 60)
        print(f" Created admin account  username: {created[0]}  password: {created[1]}")
        print(" Write this down. Change it with: python manage.py set-admin-password")
        print("=" * 60)

    conn = connect(db_path)
    base_url = get_settings(conn)["base_url"]
    conn.close()

    port = int(os.environ.get("PARTYBANK_PORT", "5000"))
    print(f" Database:      {db_path}")
    print(f" This computer: http://127.0.0.1:{port}")
    print(f" Wi-Fi devices: http://{detect_lan_ip()}:{port}")
    print(f" Card base URL: {base_url}   (change in Admin > Settings)")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
