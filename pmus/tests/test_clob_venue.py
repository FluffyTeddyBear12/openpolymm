from decimal import Decimal
from unittest.mock import MagicMock, patch
import pytest

try:
    from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams, OrderArgs
    from py_clob_client_v2.order_builder.constants import BUY, SELL
except ImportError:
    from py_clob_client.clob_types import AssetType, BalanceAllowanceParams, OrderArgs
    from py_clob_client.order_builder.constants import BUY, SELL

from pmus.config import Settings
from pmus.clob_venue import ClobVenueClient
from pmus.models import Level, Position, Quote

def test_clob_venue_fetches_and_parses_sampling_markets():
    mock_clob = MagicMock()
    mock_clob.get_sampling_markets.return_value = {
        "data": [
            {
                "condition_id": "0xcond1",
                "market_slug": "market-one",
                "question": "Will X happen in 2026?",
                "minimum_tick_size": "0.001",
                "minimum_order_size": "5",
                "rewards": {
                    "rates": [{"rewards_daily_rate": 100}],
                    "min_size": 200,
                    "max_spread": 3.5,
                },
                "tokens": [
                    {"token_id": "tok-yes-1", "outcome": "Yes"},
                    {"token_id": "tok-no-1", "outcome": "No"},
                ],
            },
            {
                "condition_id": "0xcond2",
                "market_slug": "zero-reward",
                "question": "Zero reward market?",
                "minimum_tick_size": "0.001",
                "minimum_order_size": "5",
                "rewards": {
                    "rates": [{"rewards_daily_rate": 0}],
                    "min_size": 100,
                    "max_spread": 2.0,
                },
                "tokens": [{"token_id": "tok-zero-1", "outcome": "Yes"}],
            },
            {
                "condition_id": "0xcond3",
                "market_slug": "no-rewards",
                "question": "No rewards market?",
                "tokens": [{"token_id": "tok-none-1", "outcome": "Yes"}],
            },
        ]
    }

    settings = Settings(venue="global")
    client = ClobVenueClient(settings, client=mock_clob)

    pairs = client.fetch_reward_markets(limit=20)
    assert len(pairs) == 2

    m_yes, w_yes = pairs[0]
    assert m_yes.slug == "tok-yes-1"
    assert m_yes.event_id == "market-one"
    assert m_yes.question == "Will X happen in 2026?"
    assert m_yes.tick == Decimal("0.001")
    assert m_yes.min_qty == Decimal("5")
    assert m_yes.category == "global"
    assert m_yes.active is True

    assert w_yes.slug == "tok-yes-1"
    assert w_yes.program_id == "0xcond1"
    assert w_yes.pool_usd == Decimal("100")
    assert w_yes.discount == Decimal("0.30")
    assert w_yes.target_size == Decimal("200")
    assert w_yes.max_spread == Decimal("0.035")
    assert w_yes.status == "active"

    m_no, w_no = pairs[1]
    assert m_no.slug == "tok-no-1"
    assert w_no.slug == "tok-no-1"

def test_clob_venue_fetches_and_sorts_book():
    mock_clob = MagicMock()
    settings = Settings(venue="global", clob_host="https://clob.polymarket.com")
    client = ClobVenueClient(settings, client=mock_clob)

    fake_book_resp = {
        "market": "0xcond1",
        "asset_id": "tok-1",
        "timestamp": 1700000000000,
        "bids": [
            {"price": "0.40", "size": "100"},
            {"price": "0.45", "size": "50"},
            {"price": "0.35", "size": "200"},
        ],
        "asks": [
            {"price": "0.60", "size": "80"},
            {"price": "0.55", "size": "60"},
            {"price": "0.65", "size": "120"},
        ],
    }

    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.json.return_value = fake_book_resp
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        book = client.fetch_book("tok-1")

        mock_get.assert_called_once_with("https://clob.polymarket.com/book?token_id=tok-1", timeout=10.0)
        assert book.slug == "tok-1"
        assert book.ts_ms == 1700000000000

        # Bids must be sorted descending
        assert len(book.bids) == 3
        assert book.bids[0].price == Decimal("0.45")
        assert book.bids[0].qty == Decimal("50")
        assert book.bids[1].price == Decimal("0.40")
        assert book.bids[2].price == Decimal("0.35")

        # Asks must be sorted ascending
        assert len(book.asks) == 3
        assert book.asks[0].price == Decimal("0.55")
        assert book.asks[0].qty == Decimal("60")
        assert book.asks[1].price == Decimal("0.60")
        assert book.asks[2].price == Decimal("0.65")

def test_clob_venue_signature_type_3_normalized_to_2(monkeypatch):
    monkeypatch.setenv("POLYMARKET_SIGNATURE_TYPE", "3")
    monkeypatch.setenv("POLYGON_PRIVATE_KEY", "0x" + "a" * 64)
    monkeypatch.setenv("POLYMARKET_PROXY_ADDRESS", "")
    monkeypatch.setenv("POLYMARKET_ADDRESS", "")
    settings = Settings.from_env()

    assert settings.polymarket_signature_type == 2
    assert settings.venue == "global"

    with patch("pmus.clob_venue.ClobClient") as mock_clob_cls:
        mock_inst = MagicMock()
        mock_clob_cls.return_value = mock_inst
        mock_inst.create_or_derive_api_creds.return_value = MagicMock()

        client = ClobVenueClient(settings)
        mock_clob_cls.assert_called_once()
        _, kwargs = mock_clob_cls.call_args
        assert kwargs["signature_type"] == 2


def test_clob_venue_signature_type_deposit_wallet_v2(monkeypatch):
    monkeypatch.setenv("POLYMARKET_SIGNATURE_TYPE", "2")
    monkeypatch.setenv("POLYGON_PRIVATE_KEY", "0x" + "a" * 64)
    monkeypatch.setenv("POLYMARKET_PROXY_ADDRESS", "0x1234567890abcdef1234567890abcdef12345678")
    settings = Settings.from_env()

    with patch("pmus.clob_venue.ClobClient") as mock_clob_cls:
        mock_inst = MagicMock()
        mock_clob_cls.return_value = mock_inst
        mock_inst.create_or_derive_api_creds.return_value = MagicMock()

        client = ClobVenueClient(settings)
        mock_clob_cls.assert_called_once()
        _, kwargs = mock_clob_cls.call_args
        from pmus.clob_venue import IS_V2
        expected_sig = 3 if IS_V2 else 2
        assert kwargs["signature_type"] == expected_sig

def test_clob_venue_balance_allowance_conversion():
    mock_clob = MagicMock()
    mock_clob.get_balance_allowance.return_value = {"balance": "125500000"}
    settings = Settings(venue="global", polygon_private_key="dummy")
    client = ClobVenueClient(settings, client=mock_clob)
    client.is_authenticated = True

    buying_power = client.get_buying_power()
    assert buying_power == Decimal("125.5")

    mock_clob.get_balance_allowance.assert_called_once()
    args, kwargs = mock_clob.get_balance_allowance.call_args
    param = args[0] if args else kwargs.get("params")
    assert isinstance(param, BalanceAllowanceParams)
    assert param.asset_type == AssetType.COLLATERAL

def test_clob_venue_order_creation_args():
    mock_clob = MagicMock()
    mock_clob.create_and_post_order.return_value = {"orderID": "clob-order-999"}
    settings = Settings(venue="global", polygon_private_key="dummy")
    client = ClobVenueClient(settings, client=mock_clob)
    client.is_authenticated = True

    quotes = [
        Quote(slug="tok-target", side="BUY_LONG", price=Decimal("0.485"), qty=Decimal("250")),
        Quote(slug="tok-target", side="SELL_LONG", price=Decimal("0.515"), qty=Decimal("250")),
    ]

    live_orders = client.create_orders_batch(quotes)
    assert len(live_orders) == 2

    assert mock_clob.create_and_post_order.call_count == 2
    call_args_1 = mock_clob.create_and_post_order.call_args_list[0][0][0]
    call_args_2 = mock_clob.create_and_post_order.call_args_list[1][0][0]

    assert isinstance(call_args_1, OrderArgs)
    assert call_args_1.token_id == "tok-target"
    assert call_args_1.price == 0.485
    assert call_args_1.size == 250.0
    assert call_args_1.side == BUY

    assert isinstance(call_args_2, OrderArgs)
    assert call_args_2.token_id == "tok-target"
    assert call_args_2.price == 0.515
    assert call_args_2.size == 250.0
    assert call_args_2.side == SELL

    assert live_orders[0].order_id == "clob-order-999"
    assert live_orders[0].slug == "tok-target"
    assert live_orders[0].side == "BUY_LONG"
    assert live_orders[0].price == Decimal("0.485")
    assert live_orders[0].qty_open == Decimal("250")

def test_clob_venue_cancel_and_open_orders():
    mock_clob = MagicMock()
    mock_clob.cancel_orders.return_value = True
    orders_data = [
        {
            "id": "ord-1",
            "asset_id": "tok-a",
            "side": "BUY",
            "price": "0.45",
            "size": "100",
        },
        {
            "id": "ord-2",
            "asset_id": "tok-b",
            "side": "SELL",
            "price": "0.55",
            "size": "200",
        },
    ]
    mock_clob.get_orders.return_value = orders_data
    mock_clob.get_open_orders.return_value = orders_data

    settings = Settings(venue="global", polygon_private_key="dummy")
    client = ClobVenueClient(settings, client=mock_clob)
    client.is_authenticated = True

    orders = client.get_open_orders()
    assert len(orders) == 2
    assert orders[0].order_id == "ord-1"
    assert orders[0].slug == "tok-a"
    assert orders[0].side == "BUY_LONG"
    assert orders[0].price == Decimal("0.45")
    assert orders[0].qty_open == Decimal("100")

    scoped_cancel = client.cancel_all_scoped(["tok-a"])
    assert scoped_cancel == ["ord-1"]
    mock_clob.cancel_orders.assert_called_once_with(["ord-1"])

def test_clob_venue_is_order_scoring():
    mock_clob = MagicMock()
    mock_clob.is_order_scoring.return_value = {"scoring": True}
    settings = Settings(venue="global", polygon_private_key="dummy")
    client = ClobVenueClient(settings, client=mock_clob)
    client.is_authenticated = True

    assert client.is_order_scoring("ord-123") is True
    mock_clob.is_order_scoring.return_value = {"scoring": False}
    assert client.is_order_scoring("ord-123") is False

def test_clob_venue_close_position_safe():
    mock_clob = MagicMock()
    mock_clob.create_and_post_order.return_value = {"orderID": "ord-close-1"}
    settings = Settings(venue="global", polygon_private_key="dummy")
    client = ClobVenueClient(settings, client=mock_clob)
    client.is_authenticated = True

    pos_long = Position(slug="tok-p", net_qty=Decimal("50"), avg_price=Decimal("0.50"))
    bbo_tight = (Level(Decimal("0.49"), Decimal("100")), Level(Decimal("0.51"), Decimal("100")))
    ord_tight = client.close_position_safe("tok-p", pos_long, bbo=bbo_tight, max_loss_cents=Decimal("0.02"))
    assert ord_tight is not None
    assert ord_tight.price == Decimal("0.49")

    bbo_wide = (Level(Decimal("0.40"), Decimal("100")), Level(Decimal("0.51"), Decimal("100")))
    ord_wide = client.close_position_safe("tok-p", pos_long, bbo=bbo_wide, max_loss_cents=Decimal("0.02"))
    assert ord_wide is not None
    assert ord_wide.price == Decimal("0.50")


def test_clob_venue_fetch_books_batch():
    mock_clob = MagicMock()
    settings = Settings(venue="global", clob_host="https://clob.polymarket.com")
    client = ClobVenueClient(settings, client=mock_clob)

    fake_batch_resp = [
        {
            "asset_id": "tok-1",
            "timestamp": 1700000000000,
            "bids": [{"price": "0.45", "size": "100"}],
            "asks": [{"price": "0.55", "size": "100"}],
        }
    ]

    fake_single_resp = {
        "asset_id": "tok-2",
        "timestamp": 1700000000000,
        "bids": [{"price": "0.40", "size": "50"}],
        "asks": [{"price": "0.60", "size": "50"}],
    }

    with patch("requests.post") as mock_post, patch("requests.get") as mock_get:
        mock_post_resp = MagicMock()
        mock_post_resp.json.return_value = fake_batch_resp
        mock_post_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_post_resp

        mock_get_resp = MagicMock()
        mock_get_resp.json.return_value = fake_single_resp
        mock_get_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_get_resp

        books = client.fetch_books_batch(["tok-1", "tok-2"])

        mock_post.assert_called_once_with(
            "https://clob.polymarket.com/books",
            json=[{"token_id": "tok-1"}, {"token_id": "tok-2"}],
            timeout=15.0,
        )
        # tok-2 was missing from batch response, so fetch_book fallback was called
        mock_get.assert_called_once_with("https://clob.polymarket.com/book?token_id=tok-2", timeout=10.0)

        assert "tok-1" in books
        assert "tok-2" in books
        assert books["tok-1"].bids[0].price == Decimal("0.45")
        assert books["tok-2"].bids[0].price == Decimal("0.40")


def test_clob_venue_create_orders_batch_rejections():
    mock_clob = MagicMock()
    settings = Settings(venue="global", polygon_private_key="dummy")
    client = ClobVenueClient(settings, client=mock_clob)
    client.is_authenticated = True

    # 4 quotes: 1 rejected by errorMsg, 1 rejected by success=False, 1 rejected by status=killed, 1 accepted
    quotes = [
        Quote(slug="tok-err", side="BUY_LONG", price=Decimal("0.50"), qty=Decimal("100")),
        Quote(slug="tok-fail", side="BUY_LONG", price=Decimal("0.50"), qty=Decimal("100")),
        Quote(slug="tok-killed", side="BUY_LONG", price=Decimal("0.50"), qty=Decimal("100")),
        Quote(slug="tok-ok", side="BUY_LONG", price=Decimal("0.50"), qty=Decimal("100")),
    ]

    mock_clob.create_and_post_order.side_effect = [
        {"orderID": "0x1", "errorMsg": "balance too low"},
        {"orderID": "0x2", "success": False, "error": "invalid size"},
        {"orderID": "0x3", "status": "killed"},
        {"orderID": "0x4", "status": "open", "success": True},
    ]

    live_orders = client.create_orders_batch(quotes)
    assert len(live_orders) == 1
    assert live_orders[0].order_id == "0x4"
    assert live_orders[0].slug == "tok-ok"

