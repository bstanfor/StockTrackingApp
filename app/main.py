# Python
from flask import Flask, render_template, request, redirect
import sqlite3
import pandas as pd
import plotly.express as px

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
# LOAD DATA (PANDAS)
# ---------------------------
def load_data():
    conn = get_db_connection()

    trades = pd.read_sql("SELECT * FROM transactions", conn)
    cash = pd.read_sql("SELECT * FROM cash_flows", conn)

    conn.close()

    if not trades.empty:
        trades["date"] = pd.to_datetime(trades["date"])

    if not cash.empty:
        cash["date"] = pd.to_datetime(cash["date"])

    return trades, cash

# ---------------------------
# ANALYTICS
# ---------------------------
def compute_metrics(trades, cash):
    # total external cash
    total_cash = cash["amount"].sum() if not cash.empty else 0

    if trades.empty:
        return {
            "total_cash": 0,
            "total_invested": 0,
            "portfolio_value": 0,
            "total_pnl": 0
        }

    trades["cash_flow"] = trades.apply(
        lambda x: -x["shares"] * x["price"] - x["fees"]
        if x["type"] == "BUY"
        else x["shares"] * x["price"] - x["fees"]
        if x["type"] == "SELL"
        else 0,
        axis=1
    )

    total_invested = -trades[trades["type"] == "BUY"]["cash_flow"].sum()

    # positions
    positions = trades.groupby("symbol").apply(
        lambda df: df[df["type"] == "BUY"]["shares"].sum()
        - df[df["type"] == "SELL"]["shares"].sum()
    )

    # dummy prices (replace later with yfinance)
    prices = {symbol: 100 for symbol in positions.index}

    portfolio_value = sum(positions[s] * prices[s] for s in positions.index)

    total_pnl = portfolio_value + trades["cash_flow"].sum() + total_cash

    return {
        "total_cash": round(total_cash, 2),
        "total_invested": round(total_invested, 2),
        "portfolio_value": round(portfolio_value, 2),
        "total_pnl": round(total_pnl, 2)
    }

# ---------------------------
# EQUITY CURVE
# ---------------------------
def create_equity_curve(trades, cash):
    if trades.empty and cash.empty:
        return "<p>No data</p>"

    trades["cash_flow"] = trades.apply(
        lambda x: -x["shares"] * x["price"] - x["fees"]
        if x["type"] == "BUY"
        else x["shares"] * x["price"] - x["fees"]
        if x["type"] == "SELL"
        else 0,
        axis=1
    )

    t = trades[["date", "cash_flow"]] if not trades.empty else pd.DataFrame()
    c = cash.rename(columns={"amount": "cash_flow"})[["date", "cash_flow"]] if not cash.empty else pd.DataFrame()

    combined = pd.concat([t, c])
    combined = combined.sort_values("date")

    combined["equity"] = combined["cash_flow"].cumsum()

    fig = px.line(combined, x="date", y="equity", title="Equity Curve")

    return fig.to_html(full_html=False)
# ---------------------------
# Home Page
# ---------------------------
@app.route("/")
def index():
    selected_account = request.args.get("account", "All")

    trades, cash = load_data()

    # ✅ Fetch trades
    if selected_account != "All":

        trades = trades[trades["account"] == selected_account]
        cash = cash[cash["account"] == selected_account]

    metrics = compute_metrics(trades, cash)
    equity_chart = create_equity_curve(trades, cash)

    transactions = trades.to_dict("records") if not trades.empty else []
    cash_flows = cash.to_dict("records") if not cash.empty else []

    return render_template(
        "index.html",
        transactions=transactions,
        cash_flows=cash_flows,
        accounts=["All"] + ACCOUNTS,
        selected_account=selected_account,
        equity_chart=equity_chart,
        **metrics
    )
# ---------------------------
# Add Trade
# ---------------------------
@app.route("/add_trade", methods=["POST"])
def add_trade():
    conn = get_db_connection()
    c = conn.cursor()

    c.execute("""
        INSERT INTO transactions (account, date, symbol, type, shares, price, fees, lot_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        request.form["account"],
        request.form["date"],
        request.form["stock"],
        request.form["action"],
        safe_float(request.form.get("shares")),
        safe_float(request.form.get("price")),
        safe_float(request.form.get("fees")),
        int(request.form.get("lot", 0))
    ))

    conn.commit()
    conn.close()

    return redirect("/")
# ---------------------------
# Add Cash
# ---------------------------
@app.route("/add_cash", methods=["POST"])
def add_cash():
    conn = get_db_connection()
    c = conn.cursor()

    c.execute("""
        INSERT INTO cash_flows (account, date, amount, description)
        VALUES (?, ?, ?, ?)
    """, (
        request.form["account"],
        request.form["date"],
        safe_float(request.form.get("amount")),
        request.form.get("description", "")
    ))

    conn.commit()
    conn.close()

    return redirect("/")
# ---------------------------
# DELETE
# ---------------------------
@app.route("/delete/<int:id>")
def delete(id):
    conn = get_db_connection()
    conn.execute("DELETE FROM transactions WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    return redirect("/")

@app.route("/delete_cash/<int:id>")
def delete_cash(id):
    conn = get_db_connection()
    conn.execute("DELETE FROM cash_flows WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    return redirect("/")
# ---------------------------
# Run App
# ---------------------------
if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)