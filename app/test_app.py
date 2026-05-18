from main import init_db, get_db_connection, app

# Initialize DB and insert a sample transaction
init_db()
conn = get_db_connection()
c = conn.cursor()
# Insert a sample BUY transaction
c.execute("INSERT INTO transactions (account, lot, date, settlement_date, stock, action, shares, price_share, fees, deposits, trade_amount, net_cash_flow, profit_loss) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
          ("401K-B", 1, "2026-05-18", "2026-05-20", "AAPL", "BUY", 10, 150.0, 1.5, 0, 1500.0, -(1500.0+1.5), 0))
conn.commit()

# Use Flask test client to GET the index page and verify output
with app.test_client() as client:
    resp = client.get('/')
    print('STATUS:', resp.status_code)
    html = resp.get_data(as_text=True)
    print('Contains AAPL:', 'AAPL' in html)
    print('Contains 401K-B:', '401K-B' in html)
    # Print a short excerpt around the AAPL if present
    if 'AAPL' in html:
        start = html.find('AAPL')
        print(html[start-80:start+80])

print('Test script completed.')
