from decimal import Decimal

from backend.collectors.universal_extractor import UniversalExtractor, _normalize_currency


def test_custom_price_selector_detects_symbol_in_nearby_markup() -> None:
    html = """
    <html><body>
      <h1>Sony WH-1000XM5</h1>
      <div class="price-wrap">
        <span class="currency">₹</span>
        <span data-price="22990">22,990</span>
      </div>
    </body></html>
    """

    result = UniversalExtractor().extract(
        html,
        "https://shopatsc.com/products/wh1000xm5-pmin",
        custom_selectors={"price_selector": "[data-price]"},
    )

    assert result.price == Decimal("22990")
    assert result.currency == "INR"


def test_custom_currency_selector_accepts_formatted_price() -> None:
    html = """
    <html><body>
      <div class="price">₹ 22,990</div>
    </body></html>
    """

    result = UniversalExtractor().extract(
        html,
        "https://shopatsc.com/products/wh1000xm5-pmin",
        custom_selectors={
            "price_selector": ".price",
            "currency_selector": ".price",
        },
    )

    assert result.price == Decimal("22990")
    assert result.currency == "INR"


def test_currency_normalizer_can_read_a_formatted_currency_symbol() -> None:
    assert _normalize_currency("₹ 22,990") == "INR"
    assert _normalize_currency("C$ 120.00") == "CAD"
    assert _normalize_currency("USD") == "USD"
