"""Pytest suite validating the StockTrackingApp Flask application.

Covers DB init, account management, trade/cash CRUD routes, position/lot
lookups, and the index dashboard rendering. Network calls to yfinance are
mocked so tests run fully offline and deterministically.
"""
import os
import tempfile
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


def test_dashboard_shows_total_portfolio_value_and_return_label(client):
    add_account(client, "Brokerage")
    add_cash(client, amount="5000")
    add_trade(client, shares="10", price="150", fees="1")

    resp = client.get("/")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "$4499.00" in html
    assert "Return: $-500.00" in html


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

    client.get("/delete_account/TestAcct", follow_redirects=True)
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


def test_delete_account_blocked_when_in_use(client):
    add_account(client, "Brokerage")
    add_trade(client, account="Brokerage")

    client.get("/delete_account/Brokerage", follow_redirects=True)

    assert "Brokerage" in main.load_accounts()


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
