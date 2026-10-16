"""Currency helpers: validation and how amounts read in messages (no database needed)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from coffernest.money import check_currency, check_timezone, format_money, short_money


def test_format_money() -> None:
    assert format_money(1234567, "INR") == "₹12,34,567"
    assert format_money(999, "INR") == "₹999"
    assert format_money(1234567, "USD") == "$1,234,567"
    assert format_money(Decimal("1234.5"), "EUR", 2) == "€1,234.50"
    assert format_money(Decimal("1234.5"), "JPY", 2) == "¥1,234"  # yen has no minor unit
    assert format_money(500, "CHF") == "CHF 500"
    assert format_money(-250, "GBP") == "−£250"


def test_short_money() -> None:
    assert short_money(Decimal(500_000)) == "₹5 L"
    assert short_money(Decimal(25_000_000)) == "₹2.5 Cr"
    assert short_money(Decimal(500_000), "USD") == "$500K"
    assert short_money(Decimal(2_500_000), "USD") == "$2.5M"


def test_validators() -> None:
    assert check_currency("usd") == "USD"
    assert check_timezone("America/New_York") == "America/New_York"
    for bad in ("US", "DOLLARS", "U5D", ""):
        with pytest.raises(ValueError):
            check_currency(bad)
    for bad in ("Mars/Olympus", "", "../etc/passwd"):
        with pytest.raises(ValueError):
            check_timezone(bad)
