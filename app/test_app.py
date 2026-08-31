"""Pytest suite validating the StockTrackingApp Flask application.

Covers DB init, account management, trade/cash CRUD routes, position/lot
lookups, and the index dashboard rendering. Network calls to yfinance are
mocked so tests run fully offline and deterministically.
"""
import os
import re
import tempfile
from io import BytesIO
from html.parser import HTMLParser

import pytest

import main


class PriceCellParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.symbols = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = attributes.get("class", "").split()
        if tag == "td" and "price" in classes:
            self.symbols.append(attributes.get("data-symbol"))


@pytest.fixture(autouse=True)
def _mock_price(monkeypatch):
    """Avoid real network calls to yfinance; return a fixed price/prev-price."""
    monkeypatch.setattr(main, "get_price_cached", lambda symbol: (100.0, 95.0))
    main.price_cache.clear()
    yield
    main.price_cache.clear()


@pytest.fixture
def client(monkeypatch):
    """Point the app at a fresh temp SQLite DB for each test and return a test client."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setattr(main, "db_file", path)
    main.init_db()

    main.app.config.update(TESTING=True)
    with main.app.test_client() as test_client:
        yield test_client

    os.remove(path)


def add_account(client, name):
    return client.post("/add_account", data={"account_name": name}, follow_redirects=True)


def add_trade(client, account="Brokerage", date="2026-01-05", symbol="AAPL",
              action="BUY", shares="10", price="150", fees="1"):
    return client.post("/add_trade", data={
        "account": account,
        "date": date,
        "stock": symbol,
        "action": action,
        "shares": shares,
        "price": price,
        "fees": fees,
    }, follow_redirects=True)


def add_cash(client, account="Brokerage", date="2026-01-01", amount="1000",
             txn_type="CONTRIBUTION"):
    return client.post("/add_cash", data={
        "account": account,
        "date": date,
        "amount": amount,
        "type": txn_type,
    }, follow_redirects=True)


# ---------------------------
# DB / index
# ---------------------------
def test_index_loads_with_empty_db(client):
    resp = client.get("/")
    assert resp.status_code == 200


def test_index_reflects_trade_and_cash(client):
    add_account(client, "Brokerage")
    add_cash(client)
    add_trade(client)

    resp = client.get("/")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "AAPL" in html
    assert "Brokerage" in html


def test_activity_column_menu_matches_current_table_contract(client):
    resp = client.get("/")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "syncActivityColumnCheckboxes" in html
    assert "toggleColumn(control)" in html

    column_inputs = re.findall(r'<input[^>]*data-column="(\d+)"[^>]*>', html)
    expected_optional_columns = {"0", "1", "2", "3", "4", "10", "13", "14", "15", "16", "17", "18", "19", "20"}
    assert set(column_inputs) == expected_optional_columns

    checked_columns = re.findall(r'<input[^>]*data-column="(\d+)"[^>]*checked[^>]*>', html)
    assert set(checked_columns) == {"0", "1", "2", "3", "4", "10"}

    for required_label in [
        "Lot", "Date", "Account", "Symbol", "Action", "P&L",
        "Account #", "Description", "Fidelity Action", "Fidelity Type",
        "Commission", "Accrued Interest", "Source Amount", "Settlement Date",
    ]:
        assert required_label in html


def test_dataset_downloads_include_transactions_and_cash(client):
    add_account(client, "Brokerage")
    add_cash(client)
    add_trade(client)

    json_response = client.get("/download_dataset/json")
    assert json_response.status_code == 200
    assert "attachment" in json_response.headers["Content-Disposition"]
    dataset = json_response.get_json()
    assert dataset["transactions"][0]["symbol"] == "AAPL"
    assert dataset["cash_flows"][0]["description"] == "CONTRIBUTION"

    csv_response = client.get("/download_dataset/csv")
    csv_text = csv_response.get_data(as_text=True)
    assert csv_response.status_code == 200
    assert "AAPL" in csv_text
    assert "CONTRIBUTION" in csv_text


def test_dataset_download_rejects_unknown_format(client):
    response = client.get("/download_dataset/xml")
    assert response.status_code == 400


def test_fidelity_401k_upload_imports_activity(client):
    fidelity_csv = """Run Date,Account,Account Number,Action,Symbol,Description,Type,Price ($),Quantity,Commission,Fees ($),Accrued Interest,Amount ($),Settlement Date
8/17/2026,BrokerageLink,123,YOU BOUGHT TEST CORP,TEST,Test Corp,Stocks,10.00,5,,,,-50.00,8/19/2026
7/31/2026,BrokerageLink,123,DIVIDEND RECEIVED FUND,FUND,Fund,Cash,1,0,,,,12.50,
7/31/2026,BrokerageLink Roth,456,REINVESTMENT FUND,FUND,Fund,Cash,1,-3,,,,-3.00,
"""

    response = client.post(
        "/upload_fidelity",
        data={"fidelity_file": (BytesIO(fidelity_csv.encode()), "activity.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200
    conn = main.get_db_connection()
    transactions = conn.execute(
        "SELECT account, symbol, type, shares FROM transactions ORDER BY id"
    ).fetchall()
    transaction_metadata = conn.execute(
        """SELECT account_number, fidelity_action, description, fidelity_type,
                  commission, accrued_interest, source_amount, settlement_date
           FROM transactions ORDER BY id"""
    ).fetchall()
    cash_flows = conn.execute(
        "SELECT account, description, amount FROM cash_flows ORDER BY id"
    ).fetchall()
    dividends = conn.execute(
        "SELECT account, account_number, symbol, quantity, amount FROM dividends ORDER BY id"
    ).fetchall()
    account_details = {
        row[0]: row[1]
        for row in conn.execute("SELECT name, account_number FROM accounts ORDER BY name")
    }
    accounts = [row[0] for row in conn.execute("SELECT name FROM accounts ORDER BY name")]
    conn.close()

    assert [(row[0], row[1], row[2], row[3]) for row in transactions] == [
        ("BrokerageLink", "TEST", "BUY", 5.0),
        ("BrokerageLink Roth", "FUND", "BUY", 3.0),
    ]
    assert list(cash_flows) == []
    assert [(row[0], row[1], row[2], row[3], row[4]) for row in dividends] == [
        ("BrokerageLink", "123", "FUND", 0.0, 12.5),
    ]
    assert accounts == ["BrokerageLink", "BrokerageLink Roth"]
    assert account_details == {"BrokerageLink": "123", "BrokerageLink Roth": "456"}
    assert [tuple(row) for row in transaction_metadata] == [
        ("123", "YOU BOUGHT TEST CORP", "Test Corp", "Stocks", 0.0, 0.0, -50.0, "2026-08-19"),
        ("456", "REINVESTMENT FUND", "Fund", "Cash", 0.0, 0.0, -3.0, ""),
    ]

    html = response.get_data(as_text=True)
    for column_label in [
        "Account #", "Description", "Fidelity Action", "Fidelity Type",
        "Commission", "Accrued Interest", "Source Amount", "Settlement Date",
    ]:
        assert column_label in html


def test_fidelity_import_requires_dividend_zero_quantity_and_purchase_negative_amount(client):
    fidelity_csv = """Run Date,Account,Account Number,Action,Symbol,Description,Type,Price ($),Quantity,Commission,Fees ($),Accrued Interest,Amount ($),Settlement Date
8/17/2026,401k,123,DIVIDEND RECEIVED FUND,FUND,Fund,Distributions,1,2,,,,12.50,
8/17/2026,401k,123,YOU BOUGHT FDRXX - CASH,FDRXX,Fidelity Cash,Purchase,1,5,,,,5,8/19/2026
8/17/2026,401k,123,YOU BOUGHT FDRXX - CASH,FDRXX,Fidelity Cash,Purchase,1,5,,,,-5,8/19/2026
8/17/2026,401k,123,DIVIDEND RECEIVED FUND,FUND,Fund,Distributions,1,0,,,,12.50,
"""

    response = client.post(
        "/upload_fidelity",
        data={"fidelity_file": (BytesIO(fidelity_csv.encode()), "activity.csv")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 302
    conn = main.get_db_connection()
    transactions = conn.execute(
        "SELECT symbol, type, shares FROM transactions"
    ).fetchall()
    dividends = conn.execute(
        "SELECT symbol, quantity, amount FROM dividends"
    ).fetchall()
    conn.close()

    assert [(row[0], row[1], row[2]) for row in transactions] == [("FDRXX", "BUY", 5.0)]
    assert [(row[0], row[1], row[2]) for row in dividends] == [("FUND", 0.0, 12.5)]


def test_fidelity_core_cash_purchase_is_imported_as_contribution(client):
    fidelity_csv = """Run Date,Account,Account Number,Action,Symbol,Description,Type,Price ($),Quantity,Commission,Fees ($),Accrued Interest,Amount ($),Settlement Date
8/17/2026,401k,123,PURCHASE INTO CORE ACCOUNT FIDELITY GOVERNMENT CASH RESERVES (FDRXX) (Cash),FDRXX,Fidelity Government Cash Reserves,Cash,1,0,,,,2500.00,
"""

    response = client.post(
        "/upload_fidelity",
        data={"fidelity_file": (BytesIO(fidelity_csv.encode()), "activity.csv")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 302
    trades, cash = main.load_data()
    assert trades.empty
    assert [(row["account"], row["description"], row["amount"])
            for _, row in cash.iterrows()] == [("401k", "CONTRIBUTION", 2500.0)]

    metrics = main.compute_metrics(trades, cash)
    assert metrics["total_cash"] == 2500.0


def test_fidelity_core_cash_purchase_is_buy_when_matching_dividend(client):
    fidelity_csv = """Run Date,Account,Account Number,Action,Symbol,Description,Type,Price ($),Quantity,Commission,Fees ($),Accrued Interest,Amount ($),Settlement Date
8/17/2026,401k,123,DIVIDEND RECEIVED FIDELITY GOVERNMENT CASH RESERVES (FDRXX) (Cash),FDRXX,Fidelity Government Cash Reserves,Cash,1,0,,,,125.00,
8/17/2026,401k,123,PURCHASE INTO CORE ACCOUNT FIDELITY GOVERNMENT CASH RESERVES (FDRXX) (Cash),FDRXX,Fidelity Government Cash Reserves,Cash,1,-125,,,,-125.00,
"""

    response = client.post(
        "/upload_fidelity",
        data={"fidelity_file": (BytesIO(fidelity_csv.encode()), "activity.csv")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 302
    trades, cash = main.load_data()
    dividends = main.load_dividends()

    assert [(row["symbol"], row["type"], row["shares"])
            for _, row in trades.iterrows()] == [("FDRXX", "BUY", 125.0)]
    assert cash.empty
    assert len(dividends) == 1


def test_fidelity_core_cash_redemption_is_ignored_when_symbol_is_fdrxx(client):
    fidelity_csv = """Run Date,Account,Account Number,Action,Symbol,Description,Type,Price ($),Quantity,Commission,Fees ($),Accrued Interest,Amount ($),Settlement Date
8/17/2026,401k,123,PURCHASE INTO CORE ACCOUNT FIDELITY GOVERNMENT CASH RESERVES (FDRXX) (Cash),FDRXX,Fidelity Government Cash Reserves,Cash,1,0,,,,2500.00,
8/17/2026,401k,123,REDEMPTION FROM CORE ACCOUNT FIDELITY GOVERNMENT CASH RESERVES (FDRXX) MORNING TRADE (Cash),FDRXX,Fidelity Government Cash Reserves,Cash,1,0,,,,2500.00,
"""

    response = client.post(
        "/upload_fidelity",
        data={"fidelity_file": (BytesIO(fidelity_csv.encode()), "activity.csv")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 302
    trades, cash = main.load_data()
    assert trades.empty
    assert [(row["account"], row["description"], row["amount"]) for _, row in cash.iterrows()] == [
        ("401k", "CONTRIBUTION", 2500.0)
    ]


def test_brokeragelink_fdrxx_shares_are_reported_as_cash(client, monkeypatch):
    add_account(client, "BrokerageLink")
    add_cash(client, account="BrokerageLink", amount="1000")
    add_trade(
        client, account="BrokerageLink", symbol="FDRXX", shares="500",
        price="1", fees="0"
    )

    monkeypatch.setattr(main, "get_price_cached", lambda symbol: (1.0, 1.0))
    trades, cash = main.load_data()
    enriched = main.enrich_trades(trades)

    positions = main.compute_positions(enriched, cash)["BrokerageLink"]
    metrics = main.compute_metrics(enriched, cash)

    assert positions["cash"] == 1000.0
    assert positions["positions"] == []
    assert metrics["total_cash"] == 1000.0


def test_all_accounts_report_fdrxx_shares_as_cash(client, monkeypatch):
    add_account(client, "401K")
    add_cash(client, account="401K", amount="1000")
    add_trade(client, account="401K", symbol="FDRXX", shares="750", price="1", fees="0")

    monkeypatch.setattr(main, "get_price_cached", lambda symbol: (1.0, 1.0))
    trades, cash = main.load_data()
    enriched = main.enrich_trades(trades)

    account = main.compute_positions(enriched, cash)["401K"]
    metrics = main.compute_metrics(enriched, cash)

    assert account["cash"] == 1000.0
    assert account["positions"] == []
    assert account["cash_symbol"] == "FDRXX (Cash)"
    assert metrics["total_cash"] == 1000.0


def test_dashboard_analytics_normalizes_contribution_labels_and_totals_dividends(client):
    add_account(client, "401K")
    add_cash(client, account="401K", date="2026-07-01", amount="1000", txn_type="Starting Cash")
    add_cash(client, account="401K", date="2026-07-02", amount="250", txn_type="starting_cash")

    conn = main.get_db_connection()
    conn.execute(
        """INSERT INTO dividends(account, date, symbol, quantity, amount, description, type)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("401K", "2026-07-03", "FUND", 0, 12.50, "Fund dividend", "Distributions"),
    )
    conn.commit()
    conn.close()

    trades, cash = main.load_data()
    dividends = main.load_dividends()
    analytics = main.performance_analytics(trades, cash, "Y", dividends)

    assert analytics["net_contributions"] == 1250.0
    assert analytics["total_dividends"] == 12.50


def test_dashboard_preserves_period_when_account_filter_changes(client):
    add_account(client, "401K")

    html = client.get("/?account=401K&period=YTD").get_data(as_text=True)

    assert 'name="period" value="YTD"' in html
    assert "Year to Date" in html
    assert 'name="account" value="401K"' in html


def test_dashboard_custom_date_range_limits_contributions_and_dividends(client):
    add_account(client, "401K")
    add_cash(client, account="401K", date="2026-01-15", amount="1000")
    add_cash(client, account="401K", date="2026-06-15", amount="250")

    conn = main.get_db_connection()
    conn.execute(
        """INSERT INTO dividends(account, date, symbol, quantity, amount, description, type)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("401K", "2026-01-20", "FUND", 0, 10, "Included", "Distributions"),
    )
    conn.execute(
        """INSERT INTO dividends(account, date, symbol, quantity, amount, description, type)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("401K", "2026-06-20", "FUND", 0, 20, "Excluded", "Distributions"),
    )
    conn.commit()
    conn.close()

    trades, cash = main.load_data()
    dividends = main.load_dividends()
    analytics = main.performance_analytics(
        trades, cash, "CUSTOM", dividends, "2026-01-01", "2026-03-31"
    )

    assert analytics["net_contributions"] == 1000.0
    assert analytics["total_dividends"] == 10.0


def test_dashboard_counts_legacy_fidelity_core_cash_buys_as_contributions(client):
    add_account(client, "BrokerageLink")
    conn = main.get_db_connection()
    conn.execute(
        """INSERT INTO transactions(
           account, date, symbol, type, shares, price, fees, lot_id,
           fidelity_action, source_amount)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "BrokerageLink", "2026-07-10", "FDRXX", "BUY", 681.04, 1,
            0, 0,
            "PURCHASE INTO CORE ACCOUNT FIDELITY GOVERNMENT CASH RESERVES (FDRXX) (Cash)",
            -681.04,
        ),
    )
    conn.commit()
    conn.close()

    trades, cash = main.load_data()
    trades = main.enrich_trades(trades)
    analytics = main.performance_analytics(trades, cash, "Y")

    assert analytics["net_contributions"] == 681.04

    activity = main.build_activity(trades, cash)
    core_cash_row = activity[0]
    assert core_cash_row["action"] == "CONTRIBUTION"
    assert core_cash_row["trade_amount"] == 681.04
    assert core_cash_row["net_cash_flow"] == 681.04


def test_realized_pnl_uses_same_day_trade_order_and_account_scope(client):
    add_account(client, "BrokerageLink")
    add_account(client, "BrokerageLink Roth")
    add_trade(
        client, account="BrokerageLink", date="2026-03-03", symbol="QBTS",
        action="BUY", shares="8000", price="17.52", fees="0"
    )
    add_trade(
        client, account="BrokerageLink", date="2026-03-03", symbol="QBTS",
        action="SELL", shares="8000", price="18.15", fees="0"
    )
    add_trade(
        client, account="BrokerageLink Roth", date="2026-03-03", symbol="QBTS",
        action="SELL", shares="1", price="18.15", fees="0"
    )

    trades, _ = main.load_data()
    enriched = main.enrich_trades(trades)
    brokerage_sell = enriched[
        (enriched["account"] == "BrokerageLink") &
        (enriched["type"] == "SELL")
    ].iloc[0]
    roth_sell = enriched[
        (enriched["account"] == "BrokerageLink Roth") &
        (enriched["type"] == "SELL")
    ].iloc[0]

    assert round(brokerage_sell["realized_pnl"], 2) == 5040.00
    assert round(brokerage_sell["realized_pct"], 2) == 3.60
    assert roth_sell["realized_pnl"] == 0


def test_dashboard_inline_edit_sanitizes_account_labels_before_fetch(client):
    add_account(client, "BrokerageLink")
    add_trade(client, account="BrokerageLink", shares="10", price="150", fees="1")

    html = client.get("/").get_data(as_text=True)

    assert "normalizeAccountName" in html
    assert 'form.set("account", accountValue)' in html


def test_dashboard_shows_total_portfolio_value_and_return_label(client):
    add_account(client, "Brokerage")
    add_cash(client, amount="5000")
    add_trade(client, shares="10", price="150", fees="1")

    resp = client.get("/")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "$4499.00" in html
    assert "Return: $-500.00" in html


def test_true_return_includes_unrealized_position_pnl(client):
    add_account(client, "B-Vanguard-R")
    add_cash(client, account="B-Vanguard-R", amount="5000")
    add_trade(client, account="B-Vanguard-R", shares="10", price="150", fees="0")

    response = client.get("/")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "-10.0%" in html or "-10.00%" in html


def test_true_return_counts_starting_cash_as_invested_capital(client):
    add_account(client, "B-Vanguard-R")
    add_cash(client, account="B-Vanguard-R", amount="5000")
    conn = main.get_db_connection()
    conn.execute(
        "UPDATE cash_flows SET description='STARTINGCASH' WHERE account=?",
        ("B-Vanguard-R",)
    )
    conn.commit()
    conn.close()
    add_trade(client, account="B-Vanguard-R", shares="10", price="150", fees="0")

    html = client.get("/").get_data(as_text=True)

    assert "-10.0%" in html or "-10.00%" in html


def test_true_return_falls_back_to_buy_cost_when_starting_cash_is_trade(client):
    add_account(client, "B-Vanguard-R")
    add_trade(
        client,
        account="B-Vanguard-R",
        symbol="CASH",
        action="STARTINGCASH",
        shares="0",
        price="5000",
        fees="0"
    )
    add_trade(client, account="B-Vanguard-R", shares="10", price="150", fees="0")

    html = client.get("/").get_data(as_text=True)

    assert "-10.0%" in html or "-10.00%" in html


def test_position_price_cells_include_ticker_metadata(client):
    add_account(client, "Brokerage")
    add_cash(client, amount="5000")
    add_trade(client, symbol="AAPL", shares="10", price="150")
    add_trade(client, symbol="SPCX", shares="5", price="135")

    parser = PriceCellParser()
    parser.feed(client.get("/").get_data(as_text=True))

    assert sorted(parser.symbols) == ["AAPL", "SPCX"]
    assert all(symbol and symbol.lower() != "undefined" for symbol in parser.symbols)


# ---------------------------
# Accounts
# ---------------------------
def test_add_and_delete_account(client):
    add_account(client, "TestAcct")

    resp = client.get("/")
    assert "TestAcct" in resp.get_data(as_text=True)

    client.post("/delete_account/TestAcct", follow_redirects=True)
    assert "TestAcct" not in main.load_accounts()


def test_optional_account_number_is_persisted_and_displayed(client):
    client.post("/add_account", data={
        "account_name": "Brokerage",
        "account_number": "4012"
    })

    assert main.load_account_details()["Brokerage"] == "4012"
    html = client.get("/").get_data(as_text=True)
    assert "Brokerage (Acct # 4012)" in html

    client.post("/update_account_number", data={
        "account_name": "Brokerage",
        "account_number": ""
    })
    assert main.load_account_details()["Brokerage"] is None


def test_delete_account_removes_all_associated_data(client):
    add_account(client, "Brokerage")
    add_trade(client, account="Brokerage")
    add_cash(client, account="Brokerage")
    conn = main.get_db_connection()
    conn.execute(
        """INSERT INTO dividends(account, account_number, date, symbol, quantity, amount,
                   description, type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("Brokerage", "4012", "2026-01-02", "FUND", 0, 10, "Fund dividend", "Distributions"),
    )
    conn.commit()
    conn.close()

    response = client.post("/delete_account/Brokerage", follow_redirects=True)

    assert response.status_code == 200
    assert "Brokerage" not in main.load_accounts()
    conn = main.get_db_connection()
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE account=?", ("Brokerage",)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM cash_flows WHERE account=?", ("Brokerage",)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM dividends WHERE account=?", ("Brokerage",)).fetchone()[0] == 0
    conn.close()


def test_settings_shows_confirmed_account_delete_controls(client):
    add_account(client, "Brokerage")

    html = client.get("/").get_data(as_text=True)

    assert 'action="/delete_account/Brokerage"' in html
    assert "all associated transactions, cash flows, and dividends" in html


def test_rename_account_updates_related_records(client):
    add_account(client, "OldName")
    add_trade(client, account="OldName")
    add_cash(client, account="OldName")

    client.post("/rename_account", data={"old_name": "OldName", "new_name": "NewName"},
                follow_redirects=True)

    trades, cash = main.load_data()
    assert "NewName" in trades["account"].values
    assert "NewName" in cash["account"].values
    assert "OldName" not in main.load_accounts()


# ---------------------------
# Trades
# ---------------------------
def test_add_trade_persists_row(client):
    add_account(client, "Brokerage")
    add_trade(client)

    trades, _ = main.load_data()
    assert len(trades) == 1
    assert trades.iloc[0]["symbol"] == "AAPL"
    assert trades.iloc[0]["type"] == "BUY"


def test_edit_page_shows_transaction(client):
    add_account(client, "Brokerage")
    add_trade(client)

    trades, _ = main.load_data()
    trade_id = int(trades.iloc[0]["id"])

    resp = client.get(f"/edit/{trade_id}")
    assert resp.status_code == 200
    assert "AAPL" in resp.get_data(as_text=True)


def test_update_trade_changes_values(client):
    add_account(client, "Brokerage")
    add_trade(client)

    trades, _ = main.load_data()
    trade_id = int(trades.iloc[0]["id"])

    client.post(f"/update/{trade_id}", data={
        "account": "Brokerage",
        "date": "2026-01-06",
        "stock": "MSFT",
        "action": "BUY",
        "shares": "5",
        "price": "200",
        "fees": "0",
        "lot": "0",
    }, follow_redirects=True)

    trades, _ = main.load_data()
    assert trades.iloc[0]["symbol"] == "MSFT"
    assert trades.iloc[0]["shares"] == 5


def test_account_filter_accepts_labelled_names_and_preserves_multi_select(client):
    add_account(client, "BrokerageLink")
    add_account(client, "BrokerageLink Roth")
    conn = main.get_db_connection()
    conn.execute(
        "UPDATE accounts SET account_number='653206563' WHERE name='BrokerageLink'"
    )
    conn.execute(
        "UPDATE accounts SET account_number='456' WHERE name='BrokerageLink Roth'"
    )
    conn.commit()
    conn.close()

    resp = client.get(
        "/?account=BrokerageLink+%28Acct+%23+653206563%29&account=BrokerageLink+Roth+%28Acct+%23+456%29"
    )

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "BrokerageLink" in html
    assert "BrokerageLink Roth" in html


def test_update_trade_strips_account_label_suffix(client):
    add_account(client, "BrokerageLink")
    conn = main.get_db_connection()
    conn.execute(
        "UPDATE accounts SET account_number=? WHERE name=?",
        ("653206563", "BrokerageLink"),
    )
    conn.execute(
        "INSERT INTO transactions(account,date,symbol,type,shares,price,fees,lot_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("BrokerageLink", "2026-01-05", "AAPL", "BUY", 10, 150, 1, 0),
    )
    conn.commit()
    trade_id = conn.execute("SELECT id FROM transactions WHERE account=? ORDER BY id DESC LIMIT 1", ("BrokerageLink",)).fetchone()[0]
    conn.close()

    response = client.post(f"/update/{trade_id}", data={
        "account": "BrokerageLink (Acct # 653206563)",
        "date": "2026-01-06",
        "stock": "MSFT",
        "action": "BUY",
        "shares": "5",
        "price": "200",
        "fees": "0",
        "lot": "0",
    }, follow_redirects=True)

    assert response.status_code == 200
    trades, _ = main.load_data()
    assert trades.iloc[0]["account"] == "BrokerageLink"
    assert trades.iloc[0]["symbol"] == "MSFT"


def test_inline_editor_browser_flow_updates_shares_and_fees_without_label_suffix(client):
    add_account(client, "BrokerageLink")
    conn = main.get_db_connection()
    conn.execute(
        "UPDATE accounts SET account_number=? WHERE name=?",
        ("653206563", "BrokerageLink"),
    )
    conn.execute(
        "INSERT INTO transactions(account,date,symbol,type,shares,price,fees,lot_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("BrokerageLink", "2026-01-05", "AAPL", "BUY", 10, 150, 1.5, 0),
    )
    conn.commit()
    trade_id = conn.execute(
        "SELECT id FROM transactions WHERE account=? ORDER BY id DESC LIMIT 1",
        ("BrokerageLink",),
    ).fetchone()[0]
    conn.close()

    response = client.post(
        f"/update/{trade_id}",
        data={
            "account": "BrokerageLink (Acct # 653206563)",
            "date": "2026-01-06",
            "stock": "AAPL",
            "action": "BUY",
            "shares": "12.5",
            "price": "155",
            "fees": "2.75",
            "lot": "0",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    trades, _ = main.load_data()
    row = trades.iloc[0]
    assert row["account"] == "BrokerageLink"
    assert row["shares"] == 12.5
    assert row["fees"] == 2.75
    assert "Acct #" not in row["account"]


def test_delete_trade_removes_row(client):
    add_account(client, "Brokerage")
    add_trade(client)

    trades, _ = main.load_data()
    trade_id = int(trades.iloc[0]["id"])

    client.get(f"/delete/{trade_id}", follow_redirects=True)

    trades, _ = main.load_data()
    assert trades.empty


# ---------------------------
# Cash flows
# ---------------------------
def test_add_cash_persists_row(client):
    add_account(client, "Brokerage")
    add_cash(client)

    _, cash = main.load_data()
    assert len(cash) == 1
    assert cash.iloc[0]["amount"] == 1000


def test_withdrawal_is_stored_as_negative(client):
    add_account(client, "Brokerage")
    add_cash(client, amount="200", txn_type="WITHDRAWAL")

    _, cash = main.load_data()
    assert cash.iloc[0]["amount"] == -200


def test_delete_cash_removes_row(client):
    add_account(client, "Brokerage")
    add_cash(client)

    _, cash = main.load_data()
    cash_id = int(cash.iloc[0]["id"])

    client.get(f"/delete_cash/{cash_id}", follow_redirects=True)

    _, cash = main.load_data()
    assert cash.empty


# ---------------------------
# Positions / lots / price
# ---------------------------
def test_lots_endpoint_returns_open_lot(client):
    add_account(client, "Brokerage")
    add_trade(client, account="Brokerage", symbol="AAPL", action="BUY", shares="10", price="150")

    resp = client.get("/lots/Brokerage/AAPL")
    data = resp.get_json()

    assert resp.status_code == 200
    assert len(data["lots"]) == 1
    assert data["lots"][0]["shares_remaining"] == 10


def test_position_detail_returns_lots_and_history(client):
    add_account(client, "Brokerage")
    add_trade(client, account="Brokerage", symbol="AAPL", action="BUY", shares="10", price="150")

    resp = client.get("/position/Brokerage/AAPL")
    data = resp.get_json()

    assert resp.status_code == 200
    assert data["position"]["symbol"] == "AAPL"
    assert data["lots"][0]["shares_remaining"] == 10
    assert data["history"][0]["type"] == "BUY"


def test_price_endpoint_returns_mocked_price(client):
    resp = client.get("/price/AAPL")
    data = resp.get_json()

    assert resp.status_code == 200
    assert data["price"] == 100.0


def test_price_endpoint_rejects_undefined_symbol(client):
    resp = client.get("/price/undefined")

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "A valid symbol is required"


def test_equity_chart_includes_daily_cash_balance(monkeypatch):
    today = main.pd.Timestamp.today().normalize()
    trades = main.pd.DataFrame(columns=["date", "realized_pnl"])
    cash = main.pd.DataFrame([{"date": today, "amount": 1000.13}])
    captured = {}

    class Chart:
        def to_html(self, full_html):
            return "chart"

    def capture_line(frame, **kwargs):
        captured["values"] = frame["value"].copy()
        return Chart()

    monkeypatch.setattr(main.px, "line", capture_line)

    assert main.equity_chart(trades, cash) == "chart"
    assert captured["values"].iloc[-1] == 1000.13


def test_equity_chart_includes_legacy_fdrxx_contribution(monkeypatch):
    today = main.pd.Timestamp.today().normalize()
    trades = main.pd.DataFrame([{
        "id": 1,
        "account": "Fidelity Roth",
        "date": today,
        "symbol": "FDRXX",
        "type": "BUY",
        "shares": 870.22,
        "price": 1.0,
        "fees": 0.0,
        "realized_pnl": 0.0,
        "fidelity_action": "PURCHASE INTO CORE ACCOUNT FIDELITY GOVERNMENT CASH RESERVES (FDRXX) (Cash)",
        "source_amount": -870.22,
    }])
    cash = main.pd.DataFrame(columns=["date", "amount"])
    captured = {}

    class Chart:
        def to_html(self, full_html):
            return "chart"

    def capture_line(frame, **kwargs):
        captured["values"] = frame["value"].copy()
        return Chart()

    monkeypatch.setattr(main.px, "line", capture_line)

    assert main.equity_chart(trades, cash) == "chart"
    assert captured["values"].iloc[-1] == 870.22


def test_compute_positions_after_partial_sell(client):
    add_account(client, "Brokerage")
    add_cash(client)
    add_trade(client, symbol="AAPL", action="BUY", shares="10", price="150", fees="1")
    add_trade(client, symbol="AAPL", action="SELL", shares="4", price="180", fees="1")

    trades, cash = main.load_data()
    trades = main.enrich_trades(trades)
    positions = main.compute_positions(trades, cash)

    aapl = next(p for p in positions["Brokerage"]["positions"] if p["symbol"] == "AAPL")
    assert aapl["shares"] == 6
