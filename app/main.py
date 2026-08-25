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

# ✅ Global cache
price_cache = {}

def get_price_cached(symbol):
    symbol = str(symbol).strip().upper()

    if symbol in price_cache:
        return price_cache[symbol]

    try:
        ticker = yf.Ticker(symbol)
        data = ticker.history(period="5d", auto_adjust=False)

        closes = data["Close"].dropna() if "Close" in data else pd.Series(dtype="float64")
        if not closes.empty:
            price = closes.iloc[-1]
            prev = closes.iloc[-2] if len(closes) > 1 else price
        else:
            price = ticker.fast_info.get("lastPrice") or 0
            prev = ticker.fast_info.get("previousClose") or price
    except Exception:
        price, prev = 0, 0

    result = (float(price), float(prev))
    price_cache[symbol] = result
    return result

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
        # ensure downstream code can still rely on these columns existing
        trades = trades.copy()
        trades["trade_amount"] = pd.Series(dtype="float64")
        trades["realized_pnl"] = pd.Series(dtype="float64")
        trades["realized_pct"] = pd.Series(dtype="float64")
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

            trades.at[i, "realized_pnl"] = pnl 
        
            if total_cost != 0:
                trades.at[i, "realized_pct"] = (pnl / total_cost) * 100
            else:
                trades.at[i, "realized_pct"] = 0

        else:
            trades.at[i, "realized_pnl"] = 0
            trades.at[i, "realized_pct"] = 0

    return trades

def get_open_lots(trades, account, symbol):
    trades = trades[(trades["account"] == account) & (trades["symbol"] == symbol)]
    trades = trades.sort_values("date")

    lots = []

    for _, row in trades.iterrows():
        if row["type"] == "BUY":
            lots.append({
                "lot_id": row["id"],   # ✅ use trade id as lot_id
                "shares_remaining": row["shares"],
                "price": row["price"]
            })

        elif row["type"] == "SELL":
            remaining = row["shares"]

            for lot in lots:
                if remaining <= 0:
                    break

                used = min(remaining, lot["shares_remaining"])
                lot["shares_remaining"] -= used
                remaining -= used

    # ✅ only return open lots
    return [l for l in lots if l["shares_remaining"] > 0]

def compute_positions(trades, cash):
    if trades.empty and (cash is None or cash.empty):
        return {}

    result = {}
    
    accounts = set(trades["account"].unique())

    # ✅ include accounts that only have cash (no trades)
    if cash is not None and not cash.empty:
        accounts |= set(cash["account"].unique())

    for acc in accounts:
        acc_trades = trades[trades["account"] == acc] if not trades.empty else pd.DataFrame()
        acc_cash = cash[cash["account"] == acc] if cash is not None and not cash.empty else pd.DataFrame()

        inventory = {}
        positions = {}

        # ✅ START CASH FROM CASH FLOWS (CRITICAL FIX)
        cash_balance = acc_cash["amount"].sum() if not acc_cash.empty else 0
        cash_val = cash_balance

        # -------------------------
        # PROCESS TRADES
        # -------------------------
        for _, row in acc_trades.iterrows():
            sym = row["symbol"]

            inventory.setdefault(sym, [])
            positions.setdefault(sym, 0)

            if row["type"] == "BUY":
                inventory[sym].append({
                    "shares": row["shares"],
                    "price": row["price"]
                })
                positions[sym] += row["shares"]

                # ✅ include fees
                cash_val -= (row["shares"] * row["price"] + row["fees"])

            elif row["type"] == "SELL":
                remaining = row["shares"]
                positions[sym] -= row["shares"]

                # ✅ include fees
                cash_val += (row["shares"] * row["price"] - row["fees"])

                while remaining > 0 and inventory[sym]:
                    lot = inventory[sym][0]
                    used = min(remaining, lot["shares"])

                    lot["shares"] -= used
                    remaining -= used

                    if lot["shares"] == 0:
                        inventory[sym].pop(0)

        # -------------------------
        # BUILD POSITIONS
        # -------------------------
        account_positions = []
        total_value = cash_val

        for sym, shares in positions.items():
            if shares <= 0:
                continue

            price, prev_price = get_price_cached(sym)

            cost = sum(l["shares"] * l["price"] for l in inventory[sym])
            value = shares * price

            unrealized_pnl = value - cost
            today_pnl = shares * (price - prev_price)

            account_positions.append({
                "symbol": sym,
                "shares": shares,
                "price": round(price, 2),
                "value": round(value, 2),
                "cost_basis": round(cost, 2),
                "avg_cost": round(cost / shares, 2) if shares else 0,
                "unrealized_pnl": round(unrealized_pnl, 2),
                "unrealized_pct": round((unrealized_pnl / cost * 100), 2) if cost else 0,
                "today_pnl": round(today_pnl, 2),
                "today_pct": round(((price - prev_price) / prev_price * 100), 2) if prev_price else 0
            })

            total_value += value

        # -------------------------
        # ACCOUNT %
        # -------------------------
        for p in account_positions:
            p["account_pct"] = round(
                (p["value"] / total_value * 100) if total_value else 0,
                2
            )

        result[acc] = {
            "positions": account_positions,
            "cash": round(cash_val, 2),  # ✅ FIXED CASH
            "total_value": round(total_value, 2)
        }

    # -------------------------
    # PORTFOLIO %
    # -------------------------
    grand_total = sum(acc["total_value"] for acc in result.values())

    for acc in result.values():
        for p in acc["positions"]:
            p["portfolio_pct"] = round(
                (p["value"] / grand_total * 100) if grand_total else 0,
                2
            )

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

def calculate_cash_flow(row):
    if row["type"] == "BUY":
        return -row["shares"] * row["price"] - row["fees"]
    elif row["type"] == "SELL":
        return row["shares"] * row["price"] - row["fees"]
    elif row["type"] == "DIVIDEND":
        return row["price"]

    return 0

def compute_metrics(trades, cash):
    if trades is None or len(trades) == 0:
        return {
        "total_cash": 0,
        "portfolio_value": 0,
        "realized_pnl": 0,
        "unrealized_pnl": 0,
        "total_pnl": 0
    }

    contributions = cash["amount"].sum() if cash is not None and not cash.empty else 0

    trades["cf"] = trades.apply(calculate_cash_flow, axis=1)

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

        price, prev_price = get_price_cached(sym)

        cost = sum(l["shares"] * l["price"] for l in inventory[sym])
        value = shares * price

        remaining_cost = sum(l["shares"] * l["price"] for l in inventory[sym])
        avg_cost = remaining_cost / shares if shares > 0 else 0

        portfolio_value += value
        unrealized_pnl += shares * (price - avg_cost)

    total_pnl = realized_pnl + unrealized_pnl

    return {
        "total_cash": round(cash_balance, 2),
        "portfolio_value": round(portfolio_value, 2),
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "total_pnl": round(total_pnl, 2)
    }

def build_activity(trades, cash):
    
    # ✅ SAFETY FILTER
    trades = trades[trades["type"].isin(["BUY", "SELL", "DIVIDEND"])]

    rows = []

    # ------------------------
    # TRADES (ONLY trades)
    # ------------------------
    for _, t in trades.iterrows():

        shares = t.get("shares", 0)
        price = t.get("price", 0)
        fees = t.get("fees", 0)
        trade_amount = shares * price
        lot_id = t.get("lot_id", "")

        account = t.get("account", "default")

        if t["type"] == "BUY":
            net_cash = -(trade_amount + fees)
            pl_dollar = 0
            pl_percent = 0

        elif t["type"] == "SELL":
            net_cash = trade_amount - fees

            pl_dollar = t.get("realized_pnl", 0)

            cost_basis = trade_amount - pl_dollar if trade_amount != 0 else 0
            pl_percent = (pl_dollar / cost_basis * 100) if cost_basis != 0 else 0

        elif t["type"] == "DIVIDEND":
            trade_amount = t.get("amount", price)
            net_cash = trade_amount

            pl_dollar = 0
            pl_percent = 0

        else:
            # Safety fallback (keeps trades isolated from contributions)
            trade_amount = 0
            net_cash = 0
            pl_dollar = 0
            pl_percent = 0

        rows.append({
            "id": t["id"],
            "date": t["date"],
            "account": account,             # ✅ multi-account restored
            "symbol": t.get("symbol", ""),
            "action": t["type"],
            "lot_id": lot_id,
            "shares": shares,
            "share_price": price,
            "trade_amount": trade_amount,
            "fees": fees,
            "net_cash_flow": net_cash,
            "pl_dollar": pl_dollar,         # ✅ always 0-safe
            "pl_percent": pl_percent,
            "source": "trade"
        })


    # ------------------------
    # CASH FLOWS (ONLY contributions Starting balance, Dividends/ withdrawals)
    # ------------------------
    for _, c in cash.iterrows():

        amount = c["amount"]
        account = c.get("account", "default")
        action = c["description"]

        # ✅ EXPLICIT LOGIC (recommended)
        if action == "WITHDRAWAL":
            net_cash = amount  # already negative
        elif action in ["CONTRIBUTION", "STARTINGCASH", "DIVIDEND"]:
            net_cash = amount  # positive inflow
        else:
            net_cash = amount  # fallback safeguard

        rows.append({
            "id": c["id"],
            "date": c["date"],
            "account": account,            # ✅ multi-account here too
            "symbol": "",
            "action": c["description"],    # CONTRIBUTION / WITHDRAWAL
            "lot_id": "",
            "shares": 0,
            "share_price": abs(amount),
            "trade_amount": abs(amount),
            "fees": 0,
            "net_cash_flow": amount,
            "pl_dollar": 0,                # ✅ UI consistency
            "pl_percent": 0,
            "source": "cash"               # ✅ explicit separation
        })


    df = pd.DataFrame(rows)

    if df.empty:
        return []

    # ✅ Improved deterministic sorting
    df = df.sort_values(["date", "id"])

    # ✅ Running balance PER ACCOUNT (critical fix for multi-account)
    df["balance"] = df.groupby("account")["net_cash_flow"].cumsum()

    return df.to_dict("records")


def account_balances(activity):
    if not activity:
        return {}

    df = pd.DataFrame(activity)

    # ✅ guard for missing columns 
    if "balance" not in df.columns:
        return {}

    # ✅ Get LAST balance per account
    df = df.sort_values(["account", "date", "id"])
    balances = df.groupby("account")["balance"].last().to_dict()

    return {k: round(v, 2) for k, v in balances.items()}

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
            price, _ = get_price_cached(sym)

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

def equity_chart(trades, cash, period="1Y"):
    if trades.empty and cash.empty:
        return ""


    df = trades.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()


    cash_df = cash.copy()
    if not cash_df.empty:
        cash_df["date"] = pd.to_datetime(cash_df["date"]).dt.normalize()

    # ✅ Determine date range
    today = pd.Timestamp.today().normalize()

    if period == "1M":
        start = today - pd.DateOffset(months=1)
    elif period == "YTD":
        start = pd.Timestamp(year=today.year, month=1, day=1)
    elif period == "3Y":
        start = today - pd.DateOffset(years=3)
    else:  # default 1Y
        start = today - pd.DateOffset(years=1)

    df = df[df["date"] >= start]
    cash_df = cash_df[cash_df["date"] >= start]

    # ✅ Combine into timeline
    all_dates = pd.date_range(start=start, end=today, freq="D")
    equity = pd.DataFrame(index=all_dates)
    equity["value"] = 0.0

    # ✅ compute cumulative equity
    running_value = 0

    for date in all_dates:
        # trades
        day_trades = df[df["date"] == date]
        running_value += day_trades["realized_pnl"].sum()

        # cash
        day_cash = cash_df[cash_df["date"] == date]
        running_value += day_cash["amount"].sum() if not cash_df.empty else 0

        equity.loc[date, "value"] = running_value

    fig = px.line(
        equity,
        x=equity.index,
        y="value",
        title="Portfolio Balance"
    )

    return fig.to_html(full_html=False)

def performance_analytics(trades, cash, period="90D"):

    if trades.empty and cash.empty:
        return {
            "monthly_dividends": {},
            "yearly_dividends": {},
            "net_contributions": 0,
            "total_pnl": 0,
            "true_return_pct": 0,
            "chart": ""
        }

    today = pd.Timestamp.today()

    # ✅ timeframe selection
    if period == "30D":
        start = today - pd.Timedelta(days=30)
    elif period == "60D":
        start = today - pd.Timedelta(days=60)
    elif period == "90D":
        start = today - pd.Timedelta(days=90)
    elif period == "Q":
        start = today - pd.DateOffset(months=3)
    elif period == "Y":
        start = today - pd.DateOffset(years=1)
    else:
        start = today - pd.Timedelta(days=90)

    # ✅ invested capital to date (all-time, not just this period) is the correct return denominator
    all_time_cash = cash[cash["date"] <= today]
    all_time_contrib = all_time_cash[all_time_cash["description"] == "CONTRIBUTION"]["amount"].sum() if not all_time_cash.empty else 0
    all_time_withdraw = abs(all_time_cash[all_time_cash["description"] == "WITHDRAWAL"]["amount"].sum()) if not all_time_cash.empty else 0
    invested_capital = all_time_contrib - all_time_withdraw

    trades = trades[trades["date"] >= start]
    cash = cash[cash["date"] >= start]

    # ✅ dividends
    dividends = trades[trades["type"] == "DIVIDEND"]

    if not dividends.empty:
        monthly_div = dividends.groupby(dividends["date"].dt.to_period("M"))["price"].sum()
        yearly_div = dividends.groupby(dividends["date"].dt.to_period("Y"))["price"].sum()
    else:
        monthly_div = pd.Series()
        yearly_div = pd.Series()

    # ✅ contributions / withdrawals (within selected period, for display)
    contrib = cash[cash["description"] == "CONTRIBUTION"]["amount"].sum() if not cash.empty else 0
    withdraw = abs(cash[cash["description"] == "WITHDRAWAL"]["amount"].sum()) if not cash.empty else 0

    net_contribution = contrib - withdraw

    # ✅ pnl
    total_pnl = trades["realized_pnl"].sum() if not trades.empty else 0

    # ✅ true return: growth this period vs total invested capital to date (avoid divide-by-zero)
    true_return_pct = (total_pnl / invested_capital) * 100 if invested_capital else 0

    # ✅ chart
    df = pd.DataFrame({
        "Metric": ["Contributions", "Growth"],
        "Value": [net_contribution, total_pnl]
    })

    fig = px.bar(df, x="Metric", y="Value", title="Growth vs Contributions")
    chart_html = fig.to_html(full_html=False)

    # ✅ FINAL RETURN (properly indented)
    return {
        "monthly_dividends": monthly_div.to_dict(),
        "yearly_dividends": yearly_div.to_dict(),
        "net_contributions": round(net_contribution, 2),
        "total_pnl": round(total_pnl, 2),
        "true_return_pct": round(true_return_pct, 2),
        "chart": chart_html
    }

# ---------------------------
# ROUTES
# ---------------------------
@app.route("/")
def index():

    # ✅ clear price cache (fresh data per page load)
    price_cache.clear()

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
    activity = build_activity(trades, cash)
    account_bal = account_balances(activity)
    period = request.args.get("period", "1Y")
    chart = equity_chart(trades, cash, period="1Y")
    account_positions = compute_positions(trades, cash)
    account_perf = account_performance(trades, cash) # ✅ Account Performance update
    
    # analytics function
    period = request.args.get("period", "90D")
    analytics = performance_analytics(trades, cash, period)


    # ✅ allocation chart (with cash)    
    all_positions = []

    for acc in account_positions.values():
        all_positions.extend(acc["positions"])

    alloc_chart = allocation_chart(all_positions, metrics["total_cash"])


    return render_template(
        "index.html",
        transactions=trades.to_dict("records"),
        cash_flows=cash.to_dict("records"),
        account_positions=account_positions,
        activity=activity,
        account_bal = account_balances(activity),
        allocation_chart=alloc_chart,
        #  ✅ dropdown list
        accounts= ["All"] + db_accounts,
        #  ✅ pass selected accounts
        selected_account=selected_accounts,
        account_performance=account_perf,
        equity_chart=chart,
        analytics=analytics,
        selected_period=period,
        **metrics
    )

@app.route("/lots/<account>/<symbol>")
def lots(account, symbol):
    trades, _ = load_data()
    trades = enrich_trades(trades)

    lots = get_open_lots(trades, account, symbol)

    return {"lots": lots}

@app.route("/price/<symbol>")
def get_price(symbol):
    symbol = symbol.strip().upper()
    if not symbol or symbol in {"UNDEFINED", "NULL", "NONE"}:
        return {"error": "A valid symbol is required"}, 400

    try:
        price, _ = get_price_cached(symbol)
    except Exception:
        price = 0

    return {"price": float(price)}


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
        0
    ))

    conn.commit()
    conn.close()
    return redirect("/")

@app.route("/add_cash", methods=["POST"])
def add_cash():
    conn = get_db_connection()

    account = request.form["account"]
    date = request.form["date"]
    amount = safe_float(request.form["amount"])
    txn_type = request.form.get("type")
    symbol = request.form.get("symbol", "")

    # ✅ normalize sign
    if txn_type == "WITHDRAWAL":
        signed_amount = -amount
    else:
        signed_amount = amount

        # ✅ INSERT INTO cash_flows (for metrics)
    conn.execute("""
    INSERT INTO cash_flows(account,date,amount,description)
    VALUES(?,?,?,?)
    """, (
        account,
        date,
        signed_amount,
        txn_type
    ))

    conn.commit()
    conn.close()

    return redirect("/")

@app.route("/edit/<int:id>")
def edit(id):
    conn = get_db_connection()
    tx = conn.execute("SELECT * FROM transactions WHERE id=?", (id,)).fetchone()
    conn.close()
    return render_template("edit.html", transaction=tx, accounts=load_accounts()
    )

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

@app.route("/delete_cash/<int:id>")
def delete_cash(id):
    conn = get_db_connection()
    cash_row = conn.execute(
        "SELECT * FROM cash_flows WHERE id=?", (id,)
    ).fetchone()

    if cash_row:
        # delete cash record
        conn.execute("DELETE FROM cash_flows WHERE id=?", (id,))

        # delete matching transaction record
        conn.execute("""
            DELETE FROM transactions
            WHERE account=? AND date=? AND type=?
        """, (
            cash_row["account"],
            cash_row["date"],
            cash_row["description"]
        ))

    conn.commit()
    conn.close()
    return redirect("/")
    
@app.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("file")

    if not file:
        return redirect("/")

    try:
        # ✅ load file
        if file.filename.endswith(".csv"):
            df = pd.read_csv(file)
        else:
            df = pd.read_excel(file)

        # ✅ normalize column names
        df.columns = df.columns.str.lower()

        conn = get_db_connection()

        for _, row in df.iterrows():

            account = row.get("account", "Default")
            date = row.get("date")
            symbol = row.get("symbol") or row.get("ticker") or ""
            action = str(row.get("type", row.get("action", "BUY"))).upper()

            shares = safe_float(row.get("shares"))
            price = safe_float(row.get("price"))
            fees = safe_float(row.get("fees"))

            # ✅ detect amount-based rows
            amount = safe_float(row.get("amount") or row.get("value"))

            # ✅ normalize actions
            if "DIV" in action:
                action = "DIVIDEND"
            elif "BUY" in action:
                action = "BUY"
            elif "SELL" in action:
                action = "SELL"
            elif "DEP" in action:
                action = "CONTRIBUTION"
            elif "WDR" in action:
                action = "WITHDRAWAL"

            # ✅ skip empty rows safely
            if not date:
                continue

            # ✅ insert trades
            if action in ["BUY", "SELL"]:
                conn.execute("""
                INSERT INTO transactions(account,date,symbol,type,shares,price,fees,lot_id)
                VALUES(?,?,?,?,?,?,?,?)
                """, (account, date, symbol, action, shares, price, fees, 0))

            # ✅ insert cash flows
            else:
                conn.execute("""
                INSERT INTO cash_flows(account,date,amount,description)
                VALUES(?,?,?,?)
                """, (
                    account,
                    date,
                    amount if action != "WITHDRAWAL" else -amount,
                    action
                ))

        conn.commit()
        conn.close()

    except Exception as e:
        print("Upload error:", e)

    return redirect("/")
# ---------------------------
if __name__ == "__main__":
    init_db()
    app.run(debug=True)
