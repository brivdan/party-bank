# Party Bank

A local web bank for a party with a fictional currency (A$). Guests (users) and stalls (merchants) log in with a card ID and PIN; bank staff (admins) log in with a username and password.

Stack: Python Flask, SQLite, Jinja2 templates, plain HTML/CSS/JS. Dependencies: Flask and segno.

## Quick start (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe manage.py seed-demo   # optional demo data
.\.venv\Scripts\python.exe app.py
```

Open http://127.0.0.1:5000 on this computer. Other devices on the same Wi-Fi use the "Wi-Fi devices" address printed at startup. If Windows asks, allow Python through the firewall on **private** networks.

If you skip `seed-demo`, the first `app.py` start creates an `admin` account with a random password and prints it once. You can also set `PARTYBANK_ADMIN_PASSWORD` before the first start.

## Demo logins (after `seed-demo`)

| Role     | Login                        | Secret     |
|----------|------------------------------|------------|
| Admin    | username `admin`             | `admin123` |
| User     | card `DEMO-USER-0001` (Alice)| PIN `1111` |
| User     | card `DEMO-USER-0002` (Bob)  | PIN `2222` |
| User     | card `DEMO-USER-0003` (Carol)| PIN `3333` |
| Merchant | card `DEMO-SHOP-0001` (Coffee Corner) | PIN `9001` |
| Merchant | card `DEMO-SHOP-0002` (Snack Stand)   | PIN `9002` |

The demo IDs and PINs are easy to guess. For the real party, run `python manage.py reset`, then `init`, and create real accounts from the admin screens.

## Going online (PythonAnywhere, free)

1. Sign up for a free "Beginner" account at https://www.pythonanywhere.com. Your username becomes the address: `https://USERNAME.pythonanywhere.com`.
2. **Files** tab: upload `dist/partybank.zip` to your home folder.
3. **Consoles → Bash**, then run (replace `USERNAME`):
   ```bash
   unzip partybank.zip && cd partybank-app
   python3.12 -m venv ~/venv && ~/venv/bin/pip install -r requirements.txt
   PARTYBANK_BASE_URL=https://USERNAME.pythonanywhere.com ~/venv/bin/python manage.py init
   ```
   `init` creates an empty bank: no demo accounts, every balance 0. It prints the admin password once, so save it.
4. **Web** tab → *Add a new web app* → *Manual configuration* → Python 3.12. Then:
   - *Virtualenv*: `/home/USERNAME/venv`
   - *Force HTTPS*: on
   - Open the *WSGI configuration file* link, delete everything in it, and paste:
     ```python
     import os, sys
     sys.path.insert(0, "/home/USERNAME/partybank-app")
     os.environ["PARTYBANK_TZ"] = "Australia/Sydney"   # your time zone
     from wsgi import application
     ```
   - Click **Reload**.
5. Log in at `https://USERNAME.pythonanywhere.com/admin/login` and change the password under **Password**.

On the free plan, open the Web tab and click "Run until 3 months from today" every 3 months, or the site is switched off. Back up `partybank-app/instance/` (the database and session key) from the Files tab.

**Site switch:** **Admin → Dashboard → Turn site OFF/ON**. When the site is off, participants see a "closed" page and can't log in, pay or send money. Admins can still log in and work.

## Running updates on the website from GitHub
  ```bash
  cd ~/partybank
  git pull
  ~/venv/bin/pip install -r requirements.txt
  ```


## Maintenance commands

```
python manage.py init                  create the database (safe to repeat)
python manage.py seed-demo             add demo accounts, cards and products
python manage.py set-admin-password    reset or create an admin (--username NAME)
python manage.py reset                 delete the database (asks for confirmation)
```

Data lives in `instance/partybank.db`. The session key is in `instance/secret_key`. Back up the `instance/` folder to keep the bank's state.

## Sending money

Each account has an account number, shown on the dashboard (e.g. `0004`). A user sends money in three steps:

1. Enter the recipient's account number and the amount.
2. Check the recipient's name on the confirmation screen.
3. Confirm with the PIN.

The dashboard checks for new ledger entries every 8 seconds and reloads when one appears, so incoming money shows up without a manual refresh.

Admins can see every transaction under **Admin → Transactions**. That page and the admin dashboard also run a ledger integrity check.

## Merchant point of sale

- **Products:** add, edit, deactivate or delete products and set their prices. A product that has sales can't be deleted, so it is deactivated instead. Price changes only affect new orders.
- **Terminal:**
  1. Pick products with the +/− buttons and/or enter a custom amount, then tap **Charge customer**.
  2. The server calculates the total from the prices in the database.
- **Paying (NFC tap only):** the customer taps their NFC card on the seller's phone. The phone opens the card URL, and because the seller is logged in, the app pays the seller's latest open order. The tap must happen within 1 minute of creating the order; otherwise cancel the order and create it again. The order page has no card ID entry.
- **Authorization:** the customer enters their PIN on the terminal. Identifying the card shows nothing about the account. Wrong PINs count toward the normal 5-attempt lockout.
- **Result:** the terminal shows a green "Payment approved" receipt or a red "Payment declined" box with the reason (wrong PIN, insufficient funds, blocked card).
- **Paying once:** an order can only be paid once. A second tap on Approve does not charge again.
- **Dashboard:** current balance, total sales, number of sales, fees paid (shown when fees are on), net revenue, and the transaction history.
- **Fees:** set a merchant fee percentage in **Admin → Settings** (0 = off). On each sale the fee goes from the merchant to the reserve, in the same database transaction as the purchase. Customers always pay the listed price.

## Administrator: bank desk and dashboard

The bank has two accounts of its own:

- **Party Bank Reserve** holds the bank's own money. Fees and charges are paid into it, and credits and corrections are paid out of it. It may go negative, which means the bank has given out more than it has earned.
- **Cash Vault** tracks the physical cash at the desk. Its ledger balance is the negative of the cash held, so the dashboard shows it flipped to a positive "cash held" figure. It changes only through deposits and withdrawals.

**Admin → Bank desk** records every money action as a normal ledger transaction plus an audit log entry:

| Action | Money moves | Counts as bank earnings? |
|---|---|---|
| Cash deposit | Cash Vault → account (or → reserve, for a bank cash float) | No |
| Cash withdrawal | account → Cash Vault; limited by the account balance and the cash held | No |
| Transfer | any customer, merchant or reserve account → another, with a reason | No |
| Charge | account → reserve, with a reason, approved by the account holder's PIN | Yes |
| Credit / Debit | reserve → account / account → reserve, with a mandatory reason | No |

The opening amount on a new account is recorded as a cash deposit.

The dashboard shows:
- the number of accounts and the total digital balance
- transaction volume (purchases + transfers)
- bank earnings (fees + charges only, so customer deposits are never counted as profit)
- physical cash held and the reserve balance
- a top-10 ranking by balance

By construction, cash held = digital balances + reserve balance.

**No direct balance overwrites:** database triggers reject any change to `accounts.balance_cents` that doesn't match the account's latest ledger entry. They also reject any ledger entry that doesn't continue the account's running balance, and any new account created with a non-zero balance.

## Transaction engine

`banking.post_transaction()` is the only code that changes balances. It does the following:

- **Validates amounts:** positive whole cents, at most A$1,000,000.
- **Checks accounts:** both must exist and be active, and the sender must have enough money.
- **Authorizes the actor:** a user can only send from their own account. Only admins can issue money from the reserve.
- **Blocks duplicates:** each payment form carries an idempotency key. If the same key is submitted again, the original result is returned and nothing is paid twice.
- **Writes atomically:** the transaction row, the debit and the credit are written in one `BEGIN IMMEDIATE` SQLite transaction, so two payments from the same account cannot overspend.
- **Records everything:** transaction ID, timestamp, sender, recipient, amount, type, note and who made it.

Database triggers stop anyone from updating or deleting ledger entries, and from changing the money fields of transactions or deleting them.

## Cards and NFC

- Each card has a random 12-character ID such as `K7QM-2XDH-9PRA`. Typing is case-insensitive and ignores dashes.
- The only thing written to an NFC tag (as an NDEF URL record) is the card URL, `<base URL>/card/<card ID>`, for example `http://192.168.1.100:5000/card/K7QM2XDH9PRA`. That is about 45 bytes, well under the 492-byte tag limit. Balances, PINs and names stay in SQLite.
- Opening a card URL only pre-fills the login form. The PIN is still required, and the page shows nothing about the account.
- Set the base URL in **Admin → Settings** before you write tags. You can also set it with the `PARTYBANK_BASE_URL` environment variable before the first start. Tags that are already written keep their old URL, so give the server computer a fixed IP on the party Wi-Fi.
- You can type the card ID by hand on any device.
- The "Scan NFC card" button appears only where Web NFC works: Chrome on Android over HTTPS (or localhost). Over plain HTTP on the LAN, phones open the tag URL through the OS instead, which needs no Web NFC.
- Admins can register blank cards in bulk, register a specific pre-written ID, assign cards to accounts, and block, unblock or unassign them.

## Security notes

- Each account (user, merchant or admin) gets 5 wrong PIN or password attempts, then it is locked for 5 minutes.
- Every form has CSRF protection. Session cookies are HttpOnly and SameSite=Lax, and sessions expire after 12 hours.
- A session ends at the next request if its account is frozen or its card is blocked.
- Logins, account changes, card changes and settings changes are recorded in the audit log (**Admin → Audit**).
- Money is stored as integer cents. Every balance change is a double-entry ledger posting inside one SQLite transaction. Only the system "Party Bank Reserve" account can go negative, because it is the account that issues new money.
- The server is Flask's built-in server, which is fine for a party LAN. Do not expose it to the internet.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Project layout

```
app.py                 start the server (0.0.0.0:5000)
manage.py              maintenance commands
partybank/
  __init__.py          app factory, CSRF, template filters
  db.py                SQLite connection, transactions, settings
  schema.sql           tables: accounts, cards, transactions, ledger_entries, products, settings, audit_logs
  banking.py           money, card IDs, ledger postings, account/card creation, audit
  auth.py              card+PIN login, admin login, logout, role checks
  admin.py / user.py / merchant.py   role-specific pages (merchant.py = products + terminal)
  seed.py              first admin + demo data
  templates/, static/  HTML, CSS, JS
tests/                 Stage 1-4 checks (helpers.py = shared setup)
```
