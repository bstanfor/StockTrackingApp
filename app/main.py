#Python
from flask import Flask, render_template, request, redirect, Response
import json
import sqlite3
import re
from io import BytesIO
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
        name TEXT UNIQUE,
        account_number TEXT
   )
    """)

    # Existing databases may have been created before account numbers existed.
    account_columns = [row[1] for row in c.execute("PRAGMA table_info(accounts)").fetchall()]
    if "account_number" not in account_columns:
        c.execute("ALTER TABLE accounts ADD COLUMN account_number TEXT")

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

    transaction_columns = {
        "account_number": "TEXT",
        "fidelity_action": "TEXT",
        "description": "TEXT",
        "fidelity_type": "TEXT",
        "commission": "REAL",
        "accrued_interest": "REAL",
        "source_amount": "REAL",
        "settlement_date": "TEXT",
    }
    existing_transaction_columns = {
        row[1] for row in c.execute("PRAGMA table_info(transactions)").fetchall()
    }
    for column, data_type in transaction_columns.items():
        if column not in existing_transaction_columns:
            c.execute(f"ALTER TABLE transactions ADD COLUMN {column} {data_type}")

    c.execute("""
    CREATE TABLE IF NOT EXISTS cash_flows (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account TEXT,
        date TEXT,
        amount REAL,
        description TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS dividends (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account TEXT,
        account_number TEXT,
        date TEXT,
        symbol TEXT,
        quantity REAL,
        amount REAL,
        description TEXT,
        type TEXT
    )
    """)

    dividend_columns = {
        "account_number": "TEXT",
        "quantity": "REAL",
        "amount": "REAL",
        "description": "TEXT",
        "type": "TEXT",
        "fidelity_action": "TEXT",
        "price": "REAL",
        "fees": "REAL",
        "commission": "REAL",
        "accrued_interest": "REAL",
        "source_amount": "REAL",
        "settlement_date": "TEXT",
    }
    existing_dividend_columns = {
        row[1] for row in c.execute("PRAGMA table_info(dividends)").fetchall()
    }
    for column, data_type in dividend_columns.items():
        if column not in existing_dividend_columns:
            c.execute(f"ALTER TABLE dividends ADD COLUMN {column} {data_type}")

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
        if pd.isna(v):
            return 0.0
        value = str(v).strip().replace(",", "").replace("$", "")
        if value in {"", "-", "--"}:
            return 0.0
        return float(value.replace("(", "-").replace(")", ""))
    except:
        return 0.0


def fidelity_action(action):
    action = str(action or "").upper()
    if "DIVIDEND RECEIVED" in action:
        return "DIVIDEND"
    if "REINVESTMENT" in action or "YOU BOUGHT" in action:
        return "BUY"
    if "YOU SOLD" in action or "REDEMPTION FROM CORE" in action:
        return "SELL"
    if "PURCHASE INTO CORE" in action:
        return "BUY"
    return None


def fidelity_account_number(value):
    value = str(value or "").strip()
    return "" if value.lower() in {"", "nan", "none"} else value


def is_fdrxx_cash(symbol):
    return str(symbol or "").strip().upper() == "FDRXX"


def is_contribution_description(description):
    normalized = re.sub(r"[\s_-]+", "", str(description or "")).upper()
    return normalized in {"CONTRIBUTION", "STARTINGCASH"}


def is_legacy_core_cash_transaction(row):
    return (
        str(row.get("type", "")).upper() == "BUY"
        and is_fdrxx_cash(row.get("symbol", ""))
        and "PURCHASE INTO CORE" in str(row.get("fidelity_action", "")).upper()
    )


def legacy_core_cash_amounts(trades):
    if trades.empty or "source_amount" not in trades.columns:
        return pd.Series(dtype="float64")
    mask = trades.apply(is_legacy_core_cash_transaction, axis=1)
    return pd.to_numeric(trades.loc[mask, "source_amount"], errors="coerce").abs().fillna(0)


def fidelity_transaction_type(action, type_flag, quantity, amount):
    """Classify Fidelity rows using both the action text and signed fields."""
    action_text = str(action or "").upper()
    type_text = str(type_flag or "").upper()

    if "PURCHASE INTO CORE" in action_text and (
        "FDRXX" in action_text or "CASH" in action_text or "CORE" in type_text
    ):
        return "CONTRIBUTION"

    if quantity == 0 and (
        "DIVIDEND" in action_text
        or "DIVIDEND" in type_text
        or "DISTRIBUTION" in type_text
    ):
        return "DIVIDEND"

    if quantity != 0 and amount < 0 and (
        "PURCHASE" in type_text
        or "REINVESTMENT" in type_text
        or "REINVESTMENT" in action_text
        or "YOU BOUGHT" in action_text
        or "PURCHASE INTO CORE" in action_text
    ):
        return "BUY"

    if "YOU SOLD" in action_text or "REDEMPTION FROM CORE" in action_text:
        return "SELL"

    return None


def is_fidelity_core_cash_action(action):
    action_text = str(action or "").upper()
    return "PURCHASE INTO CORE ACCOUNT" in action_text and "FDRXX" in action_text


def is_fidelity_core_cash_dividend(action):
    action_text = str(action or "").upper()
    return "DIVIDEND RECEIVED FIDELITY GOVERNMENT CASH RESERVES" in action_text and "FDRXX" in action_text


def import_fidelity_activity(file):
    df = pd.read_csv(BytesIO(file.read()), dtype=str)
    df.columns = [re.sub(r"\s+", " ", str(column).strip().lower()) for column in df.columns]

    required = {"run date", "account", "account number", "action", "symbol", "price ($)",
                "quantity", "fees ($)", "amount ($)"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError("Missing Fidelity columns: " + ", ".join(sorted(missing)))

    core_cash_dividends = set()
    for _, row in df.iterrows():
        if not is_fidelity_core_cash_dividend(row.get("action", "")):
            continue
        dividend_date = pd.to_datetime(row["run date"], errors="coerce")
        if pd.isna(dividend_date):
            continue
        core_cash_dividends.add(
            (str(row["account"]).strip(), dividend_date.strftime("%Y-%m-%d"),
             abs(safe_float(row["amount ($)"])))
        )

    imported = 0
    conn = get_db_connection()
    try:
        for _, row in df.iterrows():
            date = pd.to_datetime(row["run date"], errors="coerce")
            quantity = safe_float(row["quantity"])
            signed_amount = safe_float(row["amount ($)"])
            account = str(row["account"]).strip()
            row_date = date.strftime("%Y-%m-%d") if not pd.isna(date) else ""
            is_core_cash_reinvestment = (
                is_fidelity_core_cash_action(row.get("action", ""))
                and quantity != 0
                and signed_amount < 0
                and (account, row_date, abs(quantity)) in core_cash_dividends
            )
            action = fidelity_transaction_type(
                row["action"], row.get("type", ""), quantity, signed_amount
            )
            if is_core_cash_reinvestment:
                action = "BUY"
            if action is None or pd.isna(date):
                continue

            account_number = fidelity_account_number(row["account number"])
            symbol = str(row["symbol"]).strip().upper()
            shares = abs(quantity)
            price = abs(safe_float(row["price ($)"]))
            fees = abs(safe_float(row["fees ($)"]))
            commission = safe_float(row.get("commission", ""))
            accrued_interest = safe_float(row.get("accrued interest", ""))
            amount = abs(signed_amount)
            settlement_date = pd.to_datetime(
                row.get("settlement date", ""), errors="coerce"
            )
            settlement_date = (
                settlement_date.strftime("%Y-%m-%d")
                if not pd.isna(settlement_date) else ""
            )
            fidelity_description = str(row.get("description", "") or "").strip()
            fidelity_type = str(row.get("type", "") or "").strip()
            fidelity_action_text = str(row.get("action", "") or "").strip()

            conn.execute(
                "INSERT OR IGNORE INTO accounts(name, account_number) VALUES (?, ?)",
                (account, account_number or None),
            )
            if account_number:
                conn.execute(
                    "UPDATE accounts SET account_number=? WHERE name=? AND (account_number IS NULL OR account_number='')",
                    (account_number, account),
                )

            if action == "CONTRIBUTION":
                conn.execute("""
                INSERT INTO cash_flows(account,date,amount,description)
                VALUES(?,?,?,?)
                """, (account, date.strftime("%Y-%m-%d"), amount, action))
            elif action == "BUY":
                if not symbol or shares == 0:
                    continue
                conn.execute("""
                INSERT INTO transactions(account,date,symbol,type,shares,price,fees,lot_id)
                VALUES(?,?,?,?,?,?,?,?)
                """, (account, date.strftime("%Y-%m-%d"), symbol, action,
                       shares, price, fees, 0))
                conn.execute("""
                UPDATE transactions
                SET account_number=?, fidelity_action=?, description=?, fidelity_type=?,
                    commission=?, accrued_interest=?, source_amount=?, settlement_date=?
                WHERE id=last_insert_rowid()
                """, (account_number or None, fidelity_action_text,
                       fidelity_description, fidelity_type, commission,
                       accrued_interest, signed_amount, settlement_date))
            elif action == "SELL":
                if not symbol or shares == 0:
                    continue
                conn.execute("""
                INSERT INTO transactions(account,date,symbol,type,shares,price,fees,lot_id)
                VALUES(?,?,?,?,?,?,?,?)
                """, (account, date.strftime("%Y-%m-%d"), symbol, action,
                       shares, price, fees, 0))
                conn.execute("""
                UPDATE transactions
                SET account_number=?, fidelity_action=?, description=?, fidelity_type=?,
                    commission=?, accrued_interest=?, source_amount=?, settlement_date=?
                WHERE id=last_insert_rowid()
                """, (account_number or None, fidelity_action_text,
                       fidelity_description, fidelity_type, commission,
                       accrued_interest, signed_amount, settlement_date))
            else:
                conn.execute("""
                  INSERT INTO dividends(account,account_number,date,symbol,quantity,amount,description,type,
                      fidelity_action,price,fees,commission,accrued_interest,source_amount,settlement_date)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (account, account_number or None, date.strftime("%Y-%m-%d"),
                      symbol, quantity, amount, fidelity_description, fidelity_type,
                      fidelity_action_text, price, fees, commission, accrued_interest,
                      signed_amount, settlement_date))
            imported += 1

        conn.commit()
    finally:
        conn.close()

    return imported

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


def load_dividends():
    conn = get_db_connection()
    dividends = pd.read_sql("SELECT * FROM dividends", conn)
    conn.close()

    if not dividends.empty:
        dividends["date"] = pd.to_datetime(dividends["date"])

    return dividends

def load_accounts():
    conn = get_db_connection()
    df = pd.read_sql("SELECT name FROM accounts ORDER BY name", conn)
    conn.close()

    return df["name"].tolist()

def load_account_details():
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT name, account_number FROM accounts ORDER BY name"
    ).fetchall()
    conn.close()
    return {
        row["name"]: row["account_number"]
        for row in rows
    }

def account_label(account, account_details):
    account_number = account_details.get(account)
    return f"{account} (Acct # {account_number})" if account_number else account

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

    trades = trades.sort_values(["date", "id"]).copy()
    trades["trade_amount"] = trades["shares"] * trades["price"]
    trades["realized_pnl"] = 0.0
    trades["realized_pct"] = 0.0

    inventory = {}

    for i, row in trades.iterrows():
        inventory_key = (row["account"], row["symbol"])

        if inventory_key not in inventory:
            inventory[inventory_key] = []

        if row["type"] == "BUY":
            inventory[inventory_key].append({
                "shares": row["shares"],
                "price": row["price"]
            })

        elif row["type"] == "SELL":
            remaining = row["shares"]
            pnl = 0
            total_cost = 0

            while remaining > 0 and len(inventory[inventory_key]) > 0:
                lot = inventory[inventory_key][0]

                matched = min(remaining, lot["shares"])

                pnl += matched * (row["price"] - lot["price"])
                total_cost += matched * lot["price"]  

                lot["shares"] -= matched
                remaining -= matched

                if lot["shares"] == 0:
                    inventory[inventory_key].pop(0)

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
        cash_equivalent_lots = []

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
                if is_fdrxx_cash(sym):
                    cash_equivalent_lots.append({
                        "shares": row["shares"],
                        "price": row["price"],
                    })

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

                if is_fdrxx_cash(sym):
                    cash_equivalent_lots.append({
                        "shares": -row["shares"],
                        "price": row["price"],
                    })

        cash_val += sum(lot["shares"] * lot["price"] for lot in cash_equivalent_lots)

        # -------------------------
        # BUILD POSITIONS
        # -------------------------
        account_positions = []
        total_value = cash_val
        has_fdrxx_cash = any(is_fdrxx_cash(symbol) for symbol in positions)

        for sym, shares in positions.items():
            if shares <= 0:
                continue

            if is_fdrxx_cash(sym):
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
            "total_value": round(total_value, 2),
            "cash_symbol": "FDRXX (Cash)" if has_fdrxx_cash else "CASH",
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
    contributions = cash["amount"].sum() if cash is not None and not cash.empty else 0

    if trades is None or len(trades) == 0:
        return {
        "total_cash": round(contributions, 2),
        "portfolio_value": round(contributions, 2),
        "realized_pnl": 0,
        "unrealized_pnl": 0,
        "total_pnl": 0
    }

    trades["cf"] = trades.apply(calculate_cash_flow, axis=1)

    cash_balance = contributions + trades["cf"].sum()
    realized_pnl = trades["realized_pnl"].sum()

    # ✅ TRUE FIFO inventory for cost basis
    inventory = {}
    positions = {}
    cash_equivalent_positions = {}

    for _, row in trades.iterrows():
        sym = row["symbol"]
        position_key = (row["account"], sym)

        inventory.setdefault(position_key, [])
        positions.setdefault(position_key, 0)

        if row["type"] == "BUY":
            inventory[position_key].append({
                "shares": row["shares"],
                "price": row["price"]
            })
            positions[position_key] += row["shares"]
            if is_fdrxx_cash(sym):
                cash_equivalent_positions[position_key] = (
                    cash_equivalent_positions.get(position_key, 0) + row["shares"]
                )

        elif row["type"] == "SELL":
            remaining = row["shares"]
            positions[position_key] -= row["shares"]
            if is_fdrxx_cash(sym):
                cash_equivalent_positions[position_key] = (
                    cash_equivalent_positions.get(position_key, 0) - row["shares"]
                )

            while remaining > 0 and len(inventory[position_key]) > 0:
                lot = inventory[position_key][0]

                used = min(remaining, lot["shares"])
                lot["shares"] -= used
                remaining -= used

                if lot["shares"] == 0:
                    inventory[position_key].pop(0)

    cash_balance += sum(
        shares * get_price_cached("FDRXX")[0]
        for (account, symbol), shares in cash_equivalent_positions.items()
        if shares > 0 and is_fdrxx_cash(symbol)
    )

    holdings_value = 0
    unrealized_pnl = 0

    for (account, sym), shares in positions.items():
        if shares <= 0:
            continue

        if is_fdrxx_cash(sym):
            continue

        price, prev_price = get_price_cached(sym)

        cost = sum(l["shares"] * l["price"] for l in inventory[(account, sym)])
        value = shares * price

        remaining_cost = sum(l["shares"] * l["price"] for l in inventory[(account, sym)])
        avg_cost = remaining_cost / shares if shares > 0 else 0

        holdings_value += value
        unrealized_pnl += shares * (price - avg_cost)

    total_pnl = realized_pnl + unrealized_pnl
    portfolio_value = cash_balance + holdings_value

    return {
        "total_cash": round(cash_balance, 2),
        "portfolio_value": round(portfolio_value, 2),
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "total_pnl": round(total_pnl, 2)
    }

def build_activity(trades, cash, dividends=None):
    
    # ✅ SAFETY FILTER
    trades = trades[trades["type"].isin(["BUY", "SELL", "DIVIDEND"])]
    dividends = dividends if dividends is not None else pd.DataFrame()

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
        action = t["type"]

        if is_legacy_core_cash_transaction(t):
            action = "CONTRIBUTION"
            trade_amount = abs(t.get("source_amount", 0) or 0)
            shares = 0
            price = 0
            fees = 0
            net_cash = trade_amount
            pl_dollar = 0
            pl_percent = 0

        elif t["type"] == "BUY":
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
            "account_number": t.get("account_number", ""),
            "symbol": t.get("symbol", ""),
            "action": action,
            "description": t.get("description", ""),
            "fidelity_action": t.get("fidelity_action", ""),
            "fidelity_type": t.get("fidelity_type", ""),
            "commission": t.get("commission", 0) or 0,
            "accrued_interest": t.get("accrued_interest", 0) or 0,
            "source_amount": t.get("source_amount", ""),
            "settlement_date": t.get("settlement_date", ""),
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

    for _, d in dividends.iterrows():
        rows.append({
            "id": d["id"],
            "date": d["date"],
            "account": d.get("account", "default"),
            "account_number": d.get("account_number", "") or "",
            "symbol": d.get("symbol", ""),
            "action": "DIVIDEND",
            "description": d.get("description", "") or "",
            "fidelity_action": d.get("fidelity_action", "") or "",
            "fidelity_type": d.get("type", "") or "",
            "shares": d.get("quantity", 0) or 0,
            "share_price": d.get("price", 0) or 0,
            "trade_amount": 0,
            "fees": d.get("fees", 0) or 0,
            "commission": d.get("commission", 0) or 0,
            "accrued_interest": d.get("accrued_interest", 0) or 0,
            "source_amount": d.get("source_amount", d.get("amount", 0)) or 0,
            "settlement_date": d.get("settlement_date", "") or "",
            "net_cash_flow": 0,
            "pl_dollar": 0,
            "pl_percent": 0,
            "source": "dividend",
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

def equity_chart(trades, cash, period="1Y", start_date=None, end_date=None):
    if trades.empty and cash.empty:
        return ""


    df = trades.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()


    cash_df = cash.copy()
    if not cash_df.empty:
        cash_df["date"] = pd.to_datetime(cash_df["date"]).dt.normalize()

    # ✅ Determine date range
    today = pd.Timestamp.today().normalize()

    if period == "CUSTOM" and start_date:
        start = pd.to_datetime(start_date, errors="coerce")
        if pd.isna(start):
            start = today - pd.DateOffset(years=1)
    elif period == "1M":
        start = today - pd.DateOffset(months=1)
    elif period == "YTD":
        start = pd.Timestamp(year=today.year, month=1, day=1)
    elif period == "3Y":
        start = today - pd.DateOffset(years=3)
    elif period == "YTD":
        start = pd.Timestamp(year=today.year, month=1, day=1)
    else:  # default 1Y
        start = today - pd.DateOffset(years=1)

    end = pd.to_datetime(end_date, errors="coerce") if end_date else today
    if pd.isna(end):
        end = today
    end = end.normalize() + pd.Timedelta(days=1)

    df = df[(df["date"] >= start) & (df["date"] < end)]
    cash_df = cash_df[(cash_df["date"] >= start) & (cash_df["date"] < end)]
    core_cash_df = df[df.apply(is_legacy_core_cash_transaction, axis=1)].copy()
    if not core_cash_df.empty:
        core_cash_df["contribution"] = pd.to_numeric(
            core_cash_df["source_amount"], errors="coerce"
        ).abs().fillna(0)

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

        if not core_cash_df.empty:
            day_core_cash = core_cash_df[core_cash_df["date"] == date]
            running_value += day_core_cash["contribution"].sum()

        equity.loc[date, "value"] = running_value

    fig = px.line(
        equity,
        x=equity.index,
        y="value",
        title="Portfolio Balance"
    )

    return fig.to_html(full_html=False)

def performance_analytics(trades, cash, period="1Y", dividends=None,
                           start_date=None, end_date=None):

    dividends = dividends if dividends is not None else pd.DataFrame()

    if trades.empty and cash.empty and dividends.empty:
        return {
            "monthly_dividends": {},
            "yearly_dividends": {},
            "total_dividends": 0,
            "net_contributions": 0,
            "total_pnl": 0,
            "true_return_pct": 0,
            "chart": ""
        }

    today = pd.Timestamp.today()

    # ✅ timeframe selection
    if period == "CUSTOM" and start_date:
        start = pd.to_datetime(start_date, errors="coerce")
        if pd.isna(start):
            start = today - pd.DateOffset(years=1)
    elif period == "30D":
        start = today - pd.Timedelta(days=30)
    elif period == "60D":
        start = today - pd.Timedelta(days=60)
    elif period == "90D":
        start = today - pd.Timedelta(days=90)
    elif period == "Q":
        start = today - pd.DateOffset(months=3)
    elif period == "Y":
        start = today - pd.DateOffset(years=1)
    elif period == "YTD":
        start = pd.Timestamp(year=today.year, month=1, day=1)
    else:
        start = today - pd.DateOffset(years=1)

    end = pd.to_datetime(end_date, errors="coerce") if end_date else today
    if pd.isna(end):
        end = today
    end = end.normalize() + pd.Timedelta(days=1)

    # True return uses the complete portfolio state, including unrealized gains.
    all_time_metrics = compute_metrics(trades.copy(), cash.copy())

    # ✅ invested capital to date (all-time, not just this period) is the correct return denominator
    all_time_cash = cash[cash["date"] <= today]
    all_time_contrib = (
        all_time_cash[all_time_cash["description"].map(is_contribution_description)]["amount"].sum()
        if not all_time_cash.empty else 0
    )
    all_time_contrib += legacy_core_cash_amounts(trades).sum()
    all_time_withdraw = abs(all_time_cash[all_time_cash["description"] == "WITHDRAWAL"]["amount"].sum()) if not all_time_cash.empty else 0
    invested_capital = all_time_contrib - all_time_withdraw

    # Legacy STARTINGCASH entries may be stored as transactions instead of cash flows.
    starting_cash_transactions = (
        trades[trades["type"].map(is_contribution_description)]["price"].sum()
        if not trades.empty else 0
    )
    if starting_cash_transactions > 0:
        invested_capital += starting_cash_transactions

    # Use BUY cost as a final fallback so missing funding rows cannot force 0%.
    if invested_capital <= 0 and not trades.empty:
        invested_capital = trades[trades["type"] == "BUY"]["trade_amount"].sum()

    trades = trades[(trades["date"] >= start) & (trades["date"] < end)]
    cash = cash[(cash["date"] >= start) & (cash["date"] < end)]

    if not dividends.empty:
        dividends = dividends[(dividends["date"] >= start) & (dividends["date"] < end)]
        monthly_div = dividends.groupby(dividends["date"].dt.to_period("M"))["amount"].sum()
        yearly_div = dividends.groupby(dividends["date"].dt.to_period("Y"))["amount"].sum()
        total_dividends = dividends["amount"].sum()
    else:
        monthly_div = pd.Series()
        yearly_div = pd.Series()
        total_dividends = 0

    # ✅ contributions / withdrawals (within selected period, for display)
    contrib = (
        cash[cash["description"].map(is_contribution_description)]["amount"].sum()
        if not cash.empty else 0
    )
    contrib += legacy_core_cash_amounts(trades).sum()
    withdraw = abs(cash[cash["description"] == "WITHDRAWAL"]["amount"].sum()) if not cash.empty else 0

    net_contribution = contrib - withdraw

    # ✅ pnl
    total_pnl = trades["realized_pnl"].sum() if not trades.empty else 0

    # ✅ true return: total realized + unrealized growth vs invested capital
    true_return_pct = (
        all_time_metrics["total_pnl"] / invested_capital * 100
        if invested_capital else 0
    )

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
        "total_dividends": round(total_dividends, 2),
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
    account_details = load_account_details()
        
    # ✅ fallback to All
    if not selected_accounts or "All" in selected_accounts:
        selected_accounts = db_accounts

    # ✅ load data First (Critical Fix)
    trades, cash = load_data()
    dividends = load_dividends()
    
    # ✅ ✅ Filter and EXTRA SAFETY STARTS HERE
    if not trades.empty and "account" in trades.columns:
        trades = trades[trades["account"].isin(selected_accounts)]

    if not cash.empty and "account" in cash.columns:
        cash = cash[cash["account"].isin(selected_accounts)]

    if not dividends.empty and "account" in dividends.columns:
        dividends = dividends[dividends["account"].isin(selected_accounts)]
    
    # ✅ analytic
    trades = enrich_trades(trades)
    metrics = compute_metrics(trades, cash)
    activity = build_activity(trades, cash, dividends)
    account_bal = account_balances(activity)
    period = request.args.get("period", "1Y").upper()
    if period not in {"30D", "60D", "90D", "Q", "Y", "YTD", "CUSTOM"}:
        period = "1Y"
    start_date = request.args.get("start_date", "")
    end_date = request.args.get("end_date", "")
    chart = equity_chart(trades, cash, period=period, start_date=start_date, end_date=end_date)
    account_positions = compute_positions(trades, cash)
    account_perf = account_performance(trades, cash) # ✅ Account Performance update
    for performance in account_perf:
        performance["account_label"] = account_label(performance["account"], account_details)
    
    # analytics function
    analytics = performance_analytics(
        trades, cash, period, dividends, start_date, end_date
    )


    # ✅ allocation chart (with cash)    
    all_positions = []

    for acc in account_positions.values():
        all_positions.extend(acc["positions"])

    alloc_chart = allocation_chart(all_positions, metrics["total_cash"])


    return render_template(
        "index.html",
        transactions=trades.to_dict("records"),
        cash_flows=cash.to_dict("records"),
        dividends=dividends.to_dict("records"),
        account_positions=account_positions,
        activity=activity,
        account_bal = account_balances(activity),
        allocation_chart=alloc_chart,
        #  ✅ dropdown list
        accounts=db_accounts,
        #  ✅ pass selected accounts
        selected_account=selected_accounts,
        start_date=start_date,
        end_date=end_date,
        account_performance=account_perf,
        equity_chart=chart,
        analytics=analytics,
        selected_period=period,
        account_details=account_details,
        account_label=account_label,
        **metrics
    )

@app.route("/lots/<account>/<symbol>")
def lots(account, symbol):
    trades, _ = load_data()
    trades = enrich_trades(trades)

    lots = get_open_lots(trades, account, symbol)

    return {"lots": lots}

@app.route("/position/<account>/<symbol>")
def position_detail(account, symbol):
    trades, cash = load_data()
    trades = enrich_trades(trades)
    symbol = symbol.strip().upper()
    account_trades = trades[
        (trades["account"] == account) &
        (trades["symbol"].str.upper() == symbol)
    ] if not trades.empty else trades
    position = next(
        (item for item in compute_positions(trades, cash).get(account, {}).get("positions", [])
         if item["symbol"].upper() == symbol),
        None
    )
    return {
        "account": account,
        "symbol": symbol,
        "position": position,
        "lots": get_open_lots(trades, account, symbol),
        "history": [
            {key: (value.isoformat() if hasattr(value, "isoformat") else value)
             for key, value in row.items()}
            for row in account_trades.to_dict("records")
        ]
    }

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

@app.route("/download_dataset/<file_format>")
def download_dataset(file_format):
    if file_format not in {"csv", "json"}:
        return {"error": "Supported formats are csv and json"}, 400

    trades, cash = load_data()
    dividends = load_dividends()
    dataset = {
        "transactions": trades.to_dict("records"),
        "cash_flows": cash.to_dict("records"),
        "dividends": dividends.to_dict("records")
    }

    if file_format == "json":
        content = json.dumps(dataset, default=str, indent=2)
        return Response(
            content,
            mimetype="application/json",
            headers={"Content-Disposition": "attachment; filename=stock-tracking-dataset.json"}
        )

    trade_rows = trades.assign(dataset="transactions")
    cash_rows = cash.assign(dataset="cash_flows")
    dividend_rows = dividends.assign(dataset="dividends")
    columns = sorted(set(trade_rows.columns) | set(cash_rows.columns) | set(dividend_rows.columns))
    csv_data = pd.concat([
        trade_rows.reindex(columns=columns),
        cash_rows.reindex(columns=columns),
        dividend_rows.reindex(columns=columns),
    ], ignore_index=True)
    return Response(
        csv_data.to_csv(index=False),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=stock-tracking-dataset.csv"}
    )


@app.route("/delete_account/<name>", methods=["POST"])
def delete_account(name):
    conn = get_db_connection()

    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM transactions WHERE account=?", (name,))
        conn.execute("DELETE FROM cash_flows WHERE account=?", (name,))
        conn.execute("DELETE FROM dividends WHERE account=?", (name,))
        conn.execute("DELETE FROM accounts WHERE name=?", (name,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    conn.close()
    return redirect("/")

@app.route("/add_account", methods=["POST"])
def add_account():
    conn = get_db_connection()

    try:
        conn.execute(
            "INSERT INTO accounts(name, account_number) VALUES (?, ?)",
            (request.form["account_name"], request.form.get("account_number") or None)
        )
        conn.commit()
    except:
        pass  # avoid duplicate crash

    conn.close()
    return redirect("/")

@app.route("/update_account_number", methods=["POST"])
def update_account_number():
    conn = get_db_connection()
    conn.execute(
        "UPDATE accounts SET account_number=? WHERE name=?",
        (request.form.get("account_number") or None, request.form["account_name"])
    )
    conn.commit()
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


@app.route("/upload_fidelity", methods=["POST"])
def upload_fidelity():
    file = request.files.get("fidelity_file")
    if file and file.filename.lower().endswith(".csv"):
        try:
            import_fidelity_activity(file)
        except Exception as e:
            print("Fidelity upload error:", e)
    return redirect("/")
# ---------------------------
if __name__ == "__main__":
    init_db()
    app.run(debug=True)
