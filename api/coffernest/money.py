"""The household's currency: validation, symbols and plain-text amounts for messages.

A household has one currency and amounts are never converted; this only decides how they read.
"""

from __future__ import annotations

import re
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Symbols for common currencies; anything else is written as its code ("CHF 1,200").
SYMBOLS = {
    "INR": "₹",
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "JPY": "¥",
    "CNY": "¥",
    "AUD": "A$",
    "CAD": "C$",
    "SGD": "S$",
    "NZD": "NZ$",
    "HKD": "HK$",
    "MXN": "MX$",
    "BRL": "R$",
    "ZAR": "R",
    "KRW": "₩",
    "PHP": "₱",
    "THB": "฿",
    "VND": "₫",
    "ILS": "₪",
    "NGN": "₦",
    "TRY": "₺",
    "PLN": "zł",
    "RUB": "₽",
    "UAH": "₴",
    "BDT": "৳",
    "PKR": "Rs",
    "LKR": "Rs",
    "NPR": "Rs",
}
# Currencies with no minor unit in everyday use.
ZERO_DECIMAL = {"JPY", "KRW", "VND", "CLP", "ISK", "HUF", "TWD", "IDR", "UGX", "PYG"}
_CODE = re.compile(r"^[A-Z]{3}$")


def check_currency(code: str) -> str:
    """Upper-cased ISO 4217-style code; raises ValueError for anything else."""
    code = code.strip().upper()
    if not _CODE.match(code):
        raise ValueError("Currency must be a 3-letter code like INR, USD or EUR")
    return code


def check_timezone(name: str) -> str:
    """An IANA time zone name such as Asia/Kolkata; raises ValueError for unknown ones."""
    name = name.strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown time zone: {name}") from exc
    return name


def currency_symbol(code: str) -> str:
    """The symbol (₹, $), or the code and a space ("CHF ") when there's no common one."""
    return SYMBOLS.get(code.upper(), f"{code.upper()} ")


def _indian(digits: str) -> str:
    head, tail = digits[:-3], digits[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join([*groups, tail]) if groups else tail


def format_money(amount: object, code: str = "INR", decimals: int = 0) -> str:
    """Plain-text amount for messages: ₹1,23,456 for INR, $123,456 or ¥123,456 otherwise."""
    code = code.upper()
    value = Decimal(str(amount))
    places = 0 if code in ZERO_DECIMAL else decimals
    sign = "−" if value < 0 else ""
    text = f"{abs(value):,.{places}f}"
    if code == "INR":
        whole, _, frac = text.replace(",", "").partition(".")
        text = _indian(whole) + (f".{frac}" if frac else "")
    return f"{sign}{currency_symbol(code)}{text}"


def short_money(value: Decimal, code: str = "INR") -> str:
    """Round figures for milestones: ₹5 L, ₹1 Cr for INR; $500K, $1M otherwise."""
    sym = currency_symbol(code)
    if code.upper() == "INR":
        if value >= 10_000_000:
            n = value / 10_000_000
            return f"{sym}{n:.0f} Cr" if value % 10_000_000 == 0 else f"{sym}{n:.1f} Cr"
        return f"{sym}{value / 100_000:.0f} L"
    if value >= 1_000_000:
        n = value / 1_000_000
        return f"{sym}{n:.0f}M" if value % 1_000_000 == 0 else f"{sym}{n:.1f}M"
    return f"{sym}{value / 1_000:.0f}K"
