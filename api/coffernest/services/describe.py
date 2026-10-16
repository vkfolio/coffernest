"""Turn 'spent 1200 on aarav uniform yesterday' into a draft entry (never saved here)."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from coffernest.services import categorize

_MONTHS = {
    m: i + 1
    for i, names in enumerate(
        (
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        )
    )
    for m in names
}
_FILLER = {
    "spent", "spend", "paid", "pay", "bought", "buy", "for", "on", "rs", "inr", "rupees",
    "usd", "eur", "gbp", "dollars", "euros", "pounds",
    "rupee", "of", "the", "a", "an", "to", "at", "today", "yesterday", "and", "my", "in",
}  # fmt: skip
_AMOUNT = re.compile(
    r"(?:₹|rs\.?|inr|\$|€|£|usd|eur|gbp|aud|cad|sgd)?\s*(\d[\d,]*(?:\.\d{1,2})?)"
    r"\s*(k|thousand|l|lakh|lakhs)?\b",
    re.I,
)
_DAY_MONTH = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3,9})\b", re.I)
_MONTH_DAY = re.compile(r"\b([a-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?\b", re.I)
_SLASH = re.compile(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b")


@dataclass
class Draft:
    amount: Decimal | None
    payee: str
    note: str
    date: date
    category_id: uuid.UUID | None
    person_id: uuid.UUID | None
    confidence: float
    source: str


def _date(text: str, today: date) -> tuple[date, str]:
    low = text.lower()
    if "day before yesterday" in low:
        return today - timedelta(days=2), low.replace("day before yesterday", " ")
    if "yesterday" in low:
        return today - timedelta(days=1), low.replace("yesterday", " ")
    for rx, order in ((_DAY_MONTH, "dm"), (_MONTH_DAY, "md")):
        m = rx.search(low)
        if m:
            day_s, mon_s = (m.group(1), m.group(2)) if order == "dm" else (m.group(2), m.group(1))
            month = _MONTHS.get(mon_s.lower())
            if month:
                try:
                    d = date(today.year, month, int(day_s))
                except ValueError:
                    continue
                if d > today:
                    d = d.replace(year=today.year - 1)
                return d, low[: m.start()] + " " + low[m.end() :]
    m = _SLASH.search(low)
    if m:
        try:
            year = int(m.group(3)) if m.group(3) else today.year
            year = year + 2000 if year < 100 else year
            d = date(year, int(m.group(2)), int(m.group(1)))
            if d <= today:
                return d, low[: m.start()] + " " + low[m.end() :]
        except ValueError:
            pass
    return today, low.replace("today", " ")


def _amount(text: str) -> tuple[Decimal | None, str]:
    for m in _AMOUNT.finditer(text):
        try:
            value = Decimal(m.group(1).replace(",", ""))
        except InvalidOperation:
            continue
        unit = (m.group(2) or "").lower()
        if unit in ("k", "thousand"):
            value *= 1000
        elif unit in ("l", "lakh", "lakhs"):
            value *= 100000
        if value <= 0:
            continue
        return value.quantize(Decimal("0.01")), text[: m.start()] + " " + text[m.end() :]
    return None, text


def parse(book: categorize.Book, text: str, today: date | None = None) -> Draft:
    """Rules only: amount, date, person and category from a short description."""
    today = today or date.today()
    when, rest = _date(text, today)
    amount, rest = _amount(rest)
    words = [w for w in re.split(r"[^a-z0-9&']+", rest.lower()) if w and w not in _FILLER]
    payee = " ".join(words).strip()
    payee = payee[:1].upper() + payee[1:] if payee else ""
    g = categorize.guess(book, payee, "")
    return Draft(
        amount=amount,
        payee=payee[:120],
        note=text.strip()[:2000],
        date=when,
        category_id=g.category_id if g.sure else None,
        person_id=g.person_id,
        confidence=g.confidence,
        source=g.source,
    )


AI_SYSTEM = """You read one short note an Indian household typed about money they spent, and
pick the best category label exactly as listed (or "" if unclear) and the person it was for when
a listed name is meant (else ""). Also return a short payee or item name (2-4 words). Give your
confidence from 0 to 1. Never invent categories."""

AI_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["category", "person", "payee", "confidence"],
    "properties": {
        "category": {"type": "string"},
        "person": {"type": "string"},
        "payee": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}


async def with_ai(book: categorize.Book, draft: Draft, llm: Any) -> Draft:
    """Ask the assistant when the rules found no category."""
    labels = [
        label
        for cid, label in sorted(book.labels.items(), key=lambda kv: kv[1])
        if book.kinds[cid].value in ("expense", "debt")
    ]
    prompt = (
        "Categories:\n"
        + "\n".join(f"- {label}" for label in labels)
        + "\n\nPeople: "
        + (", ".join(n.title() for n in book.people) or "(none)")
        + f"\n\nNote: {draft.note}"
    )
    result = await llm.extract(AI_SYSTEM, prompt, [], AI_SCHEMA)
    cat = book.path(str(result.get("category") or "")) if result.get("category") else None
    try:
        conf = float(result.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    person = book.people.get(str(result.get("person") or "").lower())
    if cat is not None and conf >= categorize.SURE:
        draft.category_id = cat
        draft.confidence = conf
        draft.source = "ai"
    if draft.person_id is None and person is not None:
        draft.person_id = person
    if not draft.payee and result.get("payee"):
        draft.payee = str(result["payee"])[:120]
    return draft
