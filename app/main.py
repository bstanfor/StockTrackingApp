# Python

from flask import Flask, render_template, request, redirect
import sqlite3
from datetime import datetime

app = Flask(__name__)
db_file = "finance.db"
ACCOUNTS = ["401K-B", "401K-R", "B-Vanguard-R", "K-Vanguard-R"]

# Initialize database
def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS transactions
                 (id INTEGER PRIMARY KEY, account TEXT, lot INTEGER, date TEXT, settlement_date TEXT, stock TEXT,
                  action TEXT, shares REAL, price_share REAL, fees REAL, deposits REAL,
                  trade_amount REAL, net_cash_flow REAL, profit_loss REAL)''')
    # Add account column if not exists
    try:
        c.execute("ALTER TABLE transactions ADD COLUMN account TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.commit()
    conn.close()

def get_db_connection():
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    return conn

@app.route("/")
def index():
    selected_account = request.args.get('account', ACCOUNTS[0])
    if selected_account not in ACCOUNTS and selected_account != 'All':
        selected_account = ACCOUNTS[0]

    conn = get_db_connection()
    c = conn.cursor()
    if selected_account == 'All':
        c.execute("SELECT id, account, lot, date, settlement_date, stock, action, shares, price_share, fees, deposits, trade_amount, net_cash_flow, profit_loss FROM transactions ORDER BY date DESC")
    else:
        c.execute("SELECT id, account, lot, date, settlement_date, stock, action, shares, price_share, fees, deposits, trade_amount, net_cash_flow, profit_loss FROM transactions WHERE account = ? ORDER BY date DESC", (selected_account,))
    transactions = c.fetchall()
    conn.close()

    total_deposits = sum(safe_float(t['deposits']) for t in transactions if t['action'] in ['Deposit/Withdrawal', 'Dividend', 'Starting Cash'])
    total_profits = sum(safe_float(t['profit_loss']) for t in transactions)
    total_cash = total_deposits + total_profits

    # Supply display configuration expected by the template
    columns = [
        ('id', 'ID'), ('account', 'Account'), ('lot', 'Lot #'), ('date', 'Date'),
        ('settlement_date', 'Settlement Date'), ('stock', 'Stock'), ('action', 'Action'),
        ('shares', 'Shares'), ('price_share', 'Price per Share'), ('fees', 'Fees'),
        ('deposits', 'Deposits'), ('trade_amount', 'Trade Amount'), ('net_cash_flow', 'Net Cash Flow'),
        ('profit_loss', 'Profit / Loss'), ('actions', 'Actions'), ('profit_percent', 'Profit %')
    ]
    visible_columns = [k for k, _ in columns]
    sort_options = [('date', 'Date'), ('account', 'Account'), ('stock', 'Stock')]
    sort_by = 'date'
    sort_order = 'DESC'

    # Simple performance metric placeholder (avoid division by zero)
    try:
        performance = (total_profits / abs(total_deposits)) * 100 if total_deposits != 0 else 0.0
    except Exception:
        performance = 0.0

    return render_template("index.html", transactions=transactions, total_cash=total_cash,
                           total_profits=total_profits, performance=performance,
                           columns=columns, visible_columns=visible_columns,
                           sort_options=sort_options, sort_by=sort_by, sort_order=sort_order,
                           accounts=['All'] + ACCOUNTS, selected_account=selected_account)

def get_buy_price(lot, stock):
    """Retrieve the purchase price for a stock using the lot number"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT price_share FROM transactions WHERE lot = ? AND action = 'BUY' AND stock = ? ORDER BY date ASC LIMIT 1", 
                  (lot, stock))
        result = c.fetchone()
        conn.close()
        return result['price_share'] if result else None
    except:
        return None

def safe_float(value, default=0):
    """Safely convert a value to float, return default if empty or invalid"""
    if value == '' or value is None:
        return default
    try:
        return float(value)
    except:
        return default

@app.route("/add", methods=["POST"])
def add_transaction():
    from datetime import timedelta
    
    account = request.form["account"]
    lot = int(request.form.get("lot", 0))
    date_str = request.form["date"]
    stock = request.form["stock"]
    action = request.form["action"]
    shares = safe_float(request.form.get("shares"))
    price_share = safe_float(request.form.get("price_share"))
    fees = safe_float(request.form.get("fees"))
    deposits = safe_float(request.form.get("deposits"))
    withdrawal = safe_float(request.form.get("withdrawal"))
    starting_cash = safe_float(request.form.get("starting_cash"))
    
    # Calculate Settlement Date (2 calendar days after Date)
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    settlement_date_obj = date_obj + timedelta(days=2)
    settlement_date = settlement_date_obj.strftime("%Y-%m-%d")
    
    # Calculate Trade Amount (only for BUY and SELL)
    trade_amount = 0
    if action in ['BUY', 'SELL']:
        trade_amount = shares * price_share
    
    # Calculate Net Cash Flow and Profit/Loss based on action
    net_cash_flow = 0
    profit_loss = 0
    
    if action == "BUY":
        net_cash_flow = -(trade_amount + fees)
        profit_loss = 0
    elif action == "SELL":
        net_cash_flow = trade_amount - fees
        # Look up the purchase price using lot number
        buy_price = get_buy_price(lot, stock)
        if buy_price is not None:
            profit_loss = (price_share - buy_price) * shares - fees
        else:
            profit_loss = 0  # No matching buy transaction found
    elif action == "Deposit/Withdrawal":
        net_cash_flow = 0
        profit_loss = 0
    elif action == "Dividend":
        net_cash_flow = 0
        profit_loss = 0
    else:
        net_cash_flow = 0
        profit_loss = 0

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""INSERT INTO transactions (
        account, lot, date, settlement_date, stock, action, shares, price_share,
        fees, deposits, trade_amount, net_cash_flow, profit_loss
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
              (account, lot, date_str, settlement_date, stock, action, shares, price_share,
               fees, deposits, trade_amount, net_cash_flow, profit_loss))
    
    # Handle Deposit/Withdrawal: create separate transactions for each
    if action == "Deposit/Withdrawal":
        if deposits > 0:
            c.execute("""INSERT INTO transactions (
                account, lot, date, settlement_date, stock, action, shares, price_share,
                fees, deposits, trade_amount, net_cash_flow, profit_loss
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                      (account, 0, date_str, settlement_date, '', 'Deposit', 0, 0,
                       0, deposits, 0, deposits, 0))
        if withdrawal > 0:
            c.execute("""INSERT INTO transactions (
                account, lot, date, settlement_date, stock, action, shares, price_share,
                fees, deposits, trade_amount, net_cash_flow, profit_loss
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                      (account, 0, date_str, settlement_date, '', 'Withdrawal', 0, 0,
                       0, 0, 0, -withdrawal, 0))
        if starting_cash > 0:
            c.execute("""INSERT INTO transactions (
                account, lot, date, settlement_date, stock, action, shares, price_share,
                fees, deposits, trade_amount, net_cash_flow, profit_loss
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                      (account, 0, date_str, settlement_date, '', 'Starting Cash', 0, 0,
                       0, starting_cash, 0, starting_cash, 0))
    
    conn.commit()
    conn.close()
    return redirect("/")

@app.route("/edit/<int:transaction_id>")
def edit(transaction_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, account, lot, date, settlement_date, stock, action, shares, price_share, fees, deposits, trade_amount, net_cash_flow, profit_loss FROM transactions WHERE id = ?", (transaction_id,))
    transaction = c.fetchone()
    conn.close()
    
    if transaction is None:
        return redirect("/")
    
    return render_template("edit.html", transaction=transaction, accounts=ACCOUNTS)

@app.route("/update/<int:transaction_id>", methods=["POST"])
def update(transaction_id):
    from datetime import timedelta
    
    account = request.form["account"]
    lot = int(request.form.get("lot", 0))
    date_str = request.form["date"]
    stock = request.form["stock"]
    action = request.form["action"]
    shares = safe_float(request.form.get("shares"))
    price_share = safe_float(request.form.get("price_share"))
    fees = safe_float(request.form.get("fees"))
    deposits = safe_float(request.form.get("deposits"))
    
    # Calculate Settlement Date (2 calendar days after Date)
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    settlement_date_obj = date_obj + timedelta(days=2)
    settlement_date = settlement_date_obj.strftime("%Y-%m-%d")
    
    # Calculate Trade Amount (only for BUY and SELL)
    trade_amount = 0
    if action in ['BUY', 'SELL']:
        trade_amount = shares * price_share
    
    # Calculate Net Cash Flow and Profit/Loss based on action
    net_cash_flow = 0
    profit_loss = 0
    
    if action == "BUY":
        net_cash_flow = -(trade_amount + fees)
        profit_loss = 0
    elif action == "SELL":
        net_cash_flow = trade_amount - fees
        # Look up the purchase price using lot number
        buy_price = get_buy_price(lot, stock)
        if buy_price is not None:
            profit_loss = (price_share - buy_price) * shares - fees
        else:
            profit_loss = 0  # No matching buy transaction found
    elif action in ["Deposit/Withdrawal", "Dividend"]:
        net_cash_flow = 0
        profit_loss = 0
    elif action == "Withdrawal":
        net_cash_flow = -deposits
        profit_loss = 0
    else:
        net_cash_flow = 0
        profit_loss = 0
    
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""UPDATE transactions SET
        account = ?, lot = ?, date = ?, settlement_date = ?, stock = ?, action = ?,
        shares = ?, price_share = ?, fees = ?, deposits = ?,
        trade_amount = ?, net_cash_flow = ?, profit_loss = ?
        WHERE id = ?""",
              (account, lot, date_str, settlement_date, stock, action, shares, price_share,
               fees, deposits, trade_amount, net_cash_flow, profit_loss, transaction_id))
    conn.commit()
    conn.close()
    return redirect("/")

@app.route("/delete/<int:transaction_id>")
def delete(transaction_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM transactions WHERE id = ?", (transaction_id,))
    conn.commit()
    conn.close()
    return redirect("/")

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)