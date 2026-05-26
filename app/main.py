#Python
from flask import Flask, render_template, request, redirect
import sqlite3
import pandas as pd
import plotly.express as px
import yfinance as yf

app = Flask(__name__)
db_file = "finance.db"


# ---------------------------
# DB
# ---------------------------
def get_db_connection():
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    c = conn.cursor()


    c.execute("""
    CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE
   )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account TEXT,
        date TEXT,
        symbol TEXT,
        type TEXT,
        shares REAL,
        price REAL,
        fees REAL,
        lot_id INTEGER
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS cash_flows (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account TEXT,
        date TEXT,
        amount REAL,
        description TEXT
    )
    """)

    conn.commit()
    conn.close()

# ---------------------------
# UTIL
# ---------------------------
def safe_float(v):
    try:
        return float(v)
    except:
        return 0.0

# ---------------------------
# LOAD
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

def load_accounts():
    conn = get_db_connection()
    df = pd.read_sql("SELECT name FROM accounts ORDER BY name", conn)
    conn.close()

    return df["name"].tolist()

# ---------------------------
# ANALYTICS
# ---------------------------
def enrich_trades(trades):
    if trades.empty:
        return trades

    trades = trades.sort_values("date").copy()
    trades["trade_amount"] = trades["shares"] * trades["price"]
    trades["realized_pnl"] = 0.0
    trades["realized_pct"] = 0.0

    inventory = {}

    for i, row in trades.iterrows():
        sym = row["symbol"]

        if sym not in inventory:
            inventory[sym] = []

        if row["type"] == "BUY":
            inventory[sym].append({
                "shares": row["shares"],
                "price": row["price"]
            })

        elif row["type"] == "SELL":
            remaining = row["shares"]
            pnl = 0
            total_cost = 0

            while remaining > 0 and len(inventory[sym]) > 0:
                lot = inventory[sym][0]

                matched = min(remaining, lot["shares"])

                pnl += matched * (row["price"] - lot["price"])
                total_cost += matched * lot["price"]  

                lot["shares"] -= matched
                remaining -= matched

                if lot["shares"] == 0:
                    inventory[sym].pop(0)

            pnl = pnl - row["fees"]

            trades.at[i, "realized_pnl"] = pnl - row["fees"]
        
            if total_cost != 0:
                trades.at[i, "realized_pct"] = (pnl / total_cost) * 100
            else:
                trades.at[i, "realized_pct"] = 0

        else:
            trades.at[i, "realized_pnl"] = 0
            trades.at[i, "realized_pct"] = 0

    return trades

def compute_positions(trades):
    if trades.empty:
        return []

    inventory = {}
    positions = {}

    # Build FIFO inventory
    for _, row in trades.iterrows():
        sym = row["symbol"]

        inventory.setdefault(sym, [])
        positions.setdefault(sym, 0)

        if row["type"] == "BUY":
            inventory[sym].append({
                "shares": row["shares"],
                "price": row["price"]
            })
            positions[sym] += row["shares"]

        elif row["type"] == "SELL":
            remaining = row["shares"]
            positions[sym] -= row["shares"]

            while remaining > 0 and len(inventory[sym]) > 0:
                lot = inventory[sym][0]

                used = min(remaining, lot["shares"])
                lot["shares"] -= used
                remaining -= used

                if lot["shares"] == 0:
                    inventory[sym].pop(0)

    result = []

    total_value = 0

    # First pass: compute value
    temp = []
    for sym, shares in positions.items():
        if shares <= 0:
            continue

        try:
            data = yf.Ticker(sym).history(period="1d")
            price = data["Close"].iloc[-1] if not data.empty else 0
        except:
            price = 0

        remaining_cost = sum(l["shares"] * l["price"] for l in inventory[sym])
        value = shares * price
        unrealized_pnl = value - remaining_cost

        temp.append({
            "symbol": sym,
            "shares": shares,
            "price": price,
            "value": value,
            "cost_basis_total": remaining_cost,
            "unrealized_pnl": unrealized_pnl
        })

        total_value += value

    # Second pass: add %
    for p in temp:
        shares = p["shares"]
        value = p["value"]
        cost = p["cost_basis_total"]
        unrealized_pnl = p["unrealized_pnl"]

        avg_cost = cost / shares if shares > 0 else 0

        unrealized_pct = (unrealized_pnl / cost * 100) if cost != 0 else 0
        allocation_pct = (value / total_value * 100) if total_value != 0 else 0

        result.append({
            "symbol": p["symbol"],
            "shares": shares,
            "price": round(p["price"], 2),
            "value": round(value, 2),
            "cost_basis": round(avg_cost, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "unrealized_pct": round(unrealized_pct, 2),
            "allocation_pct": round(allocation_pct, 2)
        })

    return result

def allocation_chart(positions, total_cash):
    data = []
    
    for p in positions:
        data.append({
           "symbol": p["symbol"],
            "value": p["value"]
        })
    
    if total_cash > 0:
        data.append({
            "symbol": "Cash",
            "value": total_cash
        })
    if not data:
        return ""

    df = pd.DataFrame(data)

    fig = px.pie(
        df,
        names="symbol",
        values="value",
        title="Portfolio Allocation"
    )

    return fig.to_html(full_html=False)

def compute_metrics(trades, cash):
    if trades.empty:
        return {
            "total_cash": 0,
            "portfolio_value": 0,
            "realized_pnl": 0,
            "unrealized_pnl": 0,
            "total_pnl": 0
        }

    contributions = cash["amount"].sum() if not cash.empty else 0

    trades["cf"] = trades.apply(
        lambda x: -x["shares"] * x["price"] - x["fees"]
        if x["type"] == "BUY"
        else x["shares"] * x["price"] - x["fees"],
        axis=1
    )

    cash_balance = contributions + trades["cf"].sum()
    realized_pnl = trades["realized_pnl"].sum()

    # ✅ TRUE FIFO inventory for cost basis
    inventory = {}
    positions = {}

    for _, row in trades.iterrows():
        sym = row["symbol"]

        inventory.setdefault(sym, [])
        positions.setdefault(sym, 0)

        if row["type"] == "BUY":
            inventory[sym].append({
                "shares": row["shares"],
                "price": row["price"]
            })
            positions[sym] += row["shares"]

        elif row["type"] == "SELL":
            remaining = row["shares"]
            positions[sym] -= row["shares"]

            while remaining > 0 and len(inventory[sym]) > 0:
                lot = inventory[sym][0]

                used = min(remaining, lot["shares"])
                lot["shares"] -= used
                remaining -= used

                if lot["shares"] == 0:
                    inventory[sym].pop(0)

    portfolio_value = 0
    unrealized_pnl = 0

    for sym, shares in positions.items():
        if shares <= 0:
            continue

        try:
            price = yf.Ticker(sym).history(period="1d")["Close"].iloc[-1]
        except:
            price = 0

        remaining_cost = sum(l["shares"] * l["price"] for l in inventory[sym])
        avg_cost = remaining_cost / shares if shares > 0 else 0

        portfolio_value += shares * price
        unrealized_pnl += shares * (price - avg_cost)

    total_pnl = realized_pnl + unrealized_pnl

    return {
        "total_cash": round(cash_balance, 2),
        "portfolio_value": round(portfolio_value, 2),
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "total_pnl": round(total_pnl, 2)
    }

def account_performance(trades, cash): # ✅ Account Performance Dashboard
    if trades.empty:
        return []

    trades = trades.copy()
    cash = cash.copy()

    results = []

    accounts = trades["account"].unique()

    for acc in accounts:
        acc_trades = trades[trades["account"] == acc]
        acc_cash = cash[cash["account"] == acc]

        # ✅ realized P&L
        realized = acc_trades["realized_pnl"].sum()

        # ✅ invested capital (BUY trades)
        invested = acc_trades[acc_trades["type"] == "BUY"]["trade_amount"].sum()

        # ✅ remaining cost (open positions)
        inventory = {}
        value = 0

        for _, row in acc_trades.iterrows():
            sym = row["symbol"]
            inventory.setdefault(sym, [])
            
            if row["type"] == "BUY":
                inventory[sym].append([row["shares"], row["price"]])
            elif row["type"] == "SELL":
                remaining = row["shares"]
                while remaining > 0 and inventory[sym]:
                    lot = inventory[sym][0]
                    used = min(remaining, lot[0])
                    lot[0] -= used
                    remaining -= used
                    if lot[0] == 0:
                        inventory[sym].pop(0)

        # ✅ calculate unrealized
        unrealized = 0
        for sym, lots in inventory.items():
            try:
                price = yf.Ticker(sym).history(period="1d")["Close"].iloc[-1]
            except:
                price = 0

            for shares, cost in lots:
                unrealized += shares * (price - cost)
                value += shares * price

        total_pnl = realized + unrealized

        # ✅ return %
        total_cost = invested if invested != 0 else 1
        pct = (total_pnl / total_cost) * 100

        results.append({
            "account": acc,
            "pnl": round(total_pnl, 2),
            "pct": round(pct, 2)
        })

    return results

def equity_chart(trades, cash):
    if trades.empty and cash.empty:
        return ""

    trades["cf"] = trades.apply(
        lambda x: -x["shares"] * x["price"] - x["fees"]
        if x["type"] == "BUY"
        else x["shares"] * x["price"] - x["fees"],
        axis=1
    )

    t = trades[["date", "cf"]]
    c = cash.rename(columns={"amount": "cf"})[["date", "cf"]]

    df = pd.concat([t, c]).sort_values("date")
    df["equity"] = df["cf"].cumsum()

    fig = px.line(df, x="date", y="equity", title="Equity Curve")
    return fig.to_html(full_html=False)

# ---------------------------
# ROUTES
# ---------------------------
@app.route("/")
def index():

    # ✅ get selected accounts (multi-select)
    selected_accounts = request.args.getlist("account")
    
    # ✅ load accounts from DB (NEW)
    db_accounts = load_accounts()
        
    # ✅ fallback to All
    if not selected_accounts or "All" in selected_accounts:
        selected_accounts = db_accounts

    # ✅ load data First (Critical Fix)
    trades, cash = load_data()
    
    # ✅ ✅ Filter and EXTRA SAFETY STARTS HERE
    if not trades.empty and "account" in trades.columns:
        trades = trades[trades["account"].isin(selected_accounts)]

    if not cash.empty and "account" in cash.columns:
        cash = cash[cash["account"].isin(selected_accounts)]
    
    # ✅ analytic
    trades = enrich_trades(trades)
    metrics = compute_metrics(trades, cash)
    chart = equity_chart(trades, cash)
    positions = compute_positions(trades)
    account_perf = account_performance(trades, cash) # ✅ Account Performance update

    # ✅ allocation chart (with cash)    
    alloc_chart = allocation_chart(positions, metrics["total_cash"])

    return render_template(
        "index.html",
        transactions=trades.to_dict("records"),
        cash_flows=cash.to_dict("records"),
        positions=positions,
        allocation_chart=alloc_chart,
        #  ✅ dropdown list
        accounts= ["All"] + db_accounts,
        #  ✅ pass selected accounts
        selected_account=selected_accounts,
        account_performance=account_perf,
        equity_chart=chart,
        **metrics
    )

@app.route("/delete_account/<name>")
def delete_account(name):
    conn = get_db_connection()

    # ✅ prevent deleting accounts in use
    trades = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE account=?", (name,)
    ).fetchone()[0]

    if trades == 0:
        conn.execute("DELETE FROM accounts WHERE name=?", (name,))
        conn.commit()

    conn.close()
    return redirect("/")

@app.route("/add_account", methods=["POST"])
def add_account():
    conn = get_db_connection()

    try:
        conn.execute(
            "INSERT INTO accounts(name) VALUES (?)",
            (request.form["account_name"],)
        )
        conn.commit()
    except:
        pass  # avoid duplicate crash

    conn.close()
    return redirect("/")

@app.route("/rename_account", methods=["POST"])
def rename_account():
    old = request.form["old_name"]
    new = request.form["new_name"]

    conn = get_db_connection()

    # ✅ update across tables
    conn.execute("UPDATE accounts SET name=? WHERE name=?", (new, old))
    conn.execute("UPDATE transactions SET account=? WHERE account=?", (new, old))
    conn.execute("UPDATE cash_flows SET account=? WHERE account=?", (new, old))

    conn.commit()
    conn.close()

    return redirect("/")

@app.route("/add_trade", methods=["POST"])
def add_trade():
    conn = get_db_connection()
    conn.execute("""
    INSERT INTO transactions(account,date,symbol,type,shares,price,fees,lot_id)
    VALUES(?,?,?,?,?,?,?,?)
    """, (
        request.form["account"],
        request.form["date"],
        request.form["stock"],
        request.form["action"],
        safe_float(request.form["shares"]),
        safe_float(request.form["price"]),
        safe_float(request.form["fees"]),
        int(request.form.get("lot", 0))
    ))
    conn.commit()
    conn.close()
    return redirect("/")

@app.route("/add_cash", methods=["POST"])
def add_cash():
    conn = get_db_connection()
    conn.execute("""
    INSERT INTO cash_flows(account,date,amount,description)
    VALUES(?,?,?,?)
    """, (
        request.form["account"],
        request.form["date"],
        safe_float(request.form["amount"]),
        request.form.get("description", "")
    ))
    conn.commit()
    conn.close()
    return redirect("/")

@app.route("/edit/<int:id>")
def edit(id):
    conn = get_db_connection()
    tx = conn.execute("SELECT * FROM transactions WHERE id=?", (id,)).fetchone()
    conn.close()
    return render_template("edit.html", transaction=tx, accounts=ACCOUNTS)

@app.route("/update/<int:id>", methods=["POST"])
def update(id):
    conn = get_db_connection()
    conn.execute("""
    UPDATE transactions SET account=?,date=?,symbol=?,type=?,shares=?,price=?,fees=?,lot_id=?
    WHERE id=?
    """, (
        request.form["account"],
        request.form["date"],
        request.form["stock"],
        request.form["action"],
        safe_float(request.form["shares"]),
        safe_float(request.form["price"]),
        safe_float(request.form["fees"]),
        int(request.form["lot"]),
        id
    ))
    conn.commit()
    conn.close()
    return redirect("/")

@app.route("/delete/<int:id>")
def delete(id):
    conn = get_db_connection()
    conn.execute("DELETE FROM transactions WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return redirect("/")

# ---------------------------
if __name__ == "__main__":
    init_db()
    app.run(debug=True)