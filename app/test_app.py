"""Pytest suite validating the StockTrackingApp Flask application.

Covers DB init, account management, trade/cash CRUD routes, position/lot
lookups, and the index dashboard rendering. Network calls to yfinance are
mocked so tests run fully offline and deterministically.
"""
import os
import tempfile

import pytest

import main


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
    assert 'data-symbol="AAPL"' in html


# ---------------------------
# Accounts
# ---------------------------
def test_add_and_delete_account(client):
    add_account(client, "TestAcct")

    resp = client.get("/")
    assert "TestAcct" in resp.get_data(as_text=True)

    client.get("/delete_account/TestAcct", follow_redirects=True)
    assert "TestAcct" not in main.load_accounts()


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


def test_price_endpoint_returns_mocked_price(client):
    resp = client.get("/price/AAPL")
    data = resp.get_json()

    assert resp.status_code == 200
    assert data["price"] == 100.0


def test_price_endpoint_rejects_undefined_symbol(client):
    resp = client.get("/price/undefined")

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "A valid symbol is required"


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
