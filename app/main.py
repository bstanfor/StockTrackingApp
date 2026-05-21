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
    description = request.form.get("description", "")

    conn = get_db_connection()
    c = conn.cursor()

    c.execute("""
        INSERT INTO cash_flows (account, date, amount, description)
        VALUES (?, ?, ?, ?)
    """, (account, date, amount, description))

    conn.commit()
    conn.close()

    return redirect("/")

# ---------------------------
# Edit Trade
# ---------------------------
@app.route("/edit/<int:transaction_id>")
def edit(transaction_id):
    conn = get_db_connection()
    c = conn.cursor()

    c.execute("""
        SELECT id, account, date, symbol, type, shares, price, fees, lot_id
        FROM transactions WHERE id = ?
    """, (transaction_id,))

    transaction = c.fetchone()
    conn.close()

    if transaction is None:
        return redirect("/")

    return render_template("edit.html", transaction=transaction, accounts=ACCOUNTS)

# ---------------------------
# Update Trade
# ---------------------------
@app.route("/update/<int:transaction_id>", methods=["POST"])
def update(transaction_id):
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
        UPDATE transactions SET
        account = ?, date = ?, symbol = ?, type = ?, shares = ?, price = ?, fees = ?, lot_id = ?
        WHERE id = ?
    """, (account, date, symbol, trade_type, shares, price, fees, lot_id, transaction_id))

    conn.commit()
    conn.close()

    return redirect("/")

# ---------------------------
# Delete Trade
# ---------------------------
@app.route("/delete/<int:transaction_id>")
def delete(transaction_id):
    conn = get_db_connection()
    c = conn.cursor()

    c.execute("DELETE FROM transactions WHERE id = ?", (transaction_id,))

    conn.commit()
    conn.close()

    return redirect("/")

# ---------------------------
# Delete Cash Flow
# ---------------------------
@app.route("/delete_cash/<int:cash_id>")
def delete_cash(cash_id):
    conn = get_db_connection()
    c = conn.cursor()

    c.execute("DELETE FROM cash_flows WHERE id = ?", (cash_id,))

    conn.commit()
    conn.close()

    return redirect("/")

# ---------------------------
# Run App
# ---------------------------
if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)