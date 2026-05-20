# Python
from flask import Flask, render_template, request, redirect
import sqlite3

app = Flask(__name__)
db_file = "finance.db"

ACCOUNTS = ["401K-B", "401K-R", "B-Vanguard-R", "K-Vanguard-R"]

# ---------------------------
# Database Connection
# ---------------------------
def get_db_connection():
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------
# Initialize Database
# ---------------------------
def init_db():
conn = get_db_connection()
c = conn.cursor()


# ✅ Transactions table (trades only)
c.execute("""
CREATE TABLE IF NOT EXISTS transactions (
id INTEGER PRIMARY KEY AUTOINCREMENT,
account TEXT NOT NULL,
date TEXT NOT NULL,
symbol TEXT,
type TEXT NOT NULL,
shares REAL DEFAULT 0,
price REAL DEFAULT 0,
fees REAL DEFAULT 0,
lot_id INTEGER
)
""")


# ✅ Cash flows (deposits / withdrawals)
c.execute("""
CREATE TABLE IF NOT EXISTS cash_flows (
id INTEGER PRIMARY KEY AUTOINCREMENT,
account TEXT NOT NULL,
date TEXT NOT NULL,
amount REAL NOT NULL,
description TEXT
)
""")


conn.commit()
conn.close()


# ---------------------------
# Utility
# ---------------------------
def safe_float(value, default=0.0):
if value in ("", None):
return default
try:
return float(value)
except:
return default


# ---------------------------
# Home Page
# ---------------------------
@app.route("/")
def index():
selected_account = request.args.get("account", ACCOUNTS[0])


conn = get_db_connection()
c = conn.cursor()


# ✅ Fetch trades
if selected_account == "All":
c.execute("""
SELECT id, account, date, symbol, type, shares, price, fees, lot_id
FROM transactions
ORDER BY date DESC
""")
else:
c.execute("""
SELECT id, account, date, symbol, type, shares, price, fees, lot_id
FROM transactions
WHERE account = ?
ORDER BY date DESC
""", (selected_account,))


transactions = c.fetchall()

#✅ Fetch cash flows
    if selected_account == "All":
        c.execute("""
            SELECT id, account, date, amount, description
            FROM cash_flows
            ORDER BY date DESC
        """)
    else:
        c.execute("""
            SELECT id, account, date, amount, description
            FROM cash_flows
            WHERE account = ?
            ORDER BY date DESC
        """, (selected_account,))


    cash_flows = c.fetchall()


    conn.close()


    return render_template(
        "index.html",
        transactions=transactions,
        cash_flows=cash_flows,
        accounts=["All"] + ACCOUNTS,
        selected_account=selected_account
    )


# ---------------------------
# Add Trade
# ---------------------------
@app.route("/add_trade", methods=["POST"])
def add_trade():
    account = request.form["account"]
    date = request.form["date"]
    symbol = request.form["stock"]
    trade_type = request.form["action"]
    shares = safe_float(request.form.get("shares"))
    price = safe_float(request.form.get("price_share"))
    fees = safe_float(request.form.get("fees"))
    lot_id = int(request.form.get("lot", 0))


    conn = get_db_connection()
    c = conn.cursor()


    c.execute("""
        INSERT INTO transactions (
            account, date, symbol, type, shares, price, fees, lot_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (account, date, symbol, trade_type, shares, price, fees, lot_id))


    conn.commit()
    conn.close()


    return redirect("/")


# ---------------------------
# Add Cash Flow
# ---------------------------
@app.route("/add_cash", methods=["POST"])
def add_cash():
    account = request.form["account"]
    date = request.form["date"]
    amount = safe_float(request.form.get("amount"))