"""Sort entries into categories (and people) automatically.

In order: rules the household taught by correcting an entry, a built-in list of Indian
merchants, a person's name in the text ("aarav uniform"), and item words ("toys", "petrol").
Payees none of these know can be sent to the assistant in a batch; sure answers become rules,
unsure ones wait in "Help me sort".
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coffernest.enums import CategoryKind, Direction, RuleSource, TxnKind
from coffernest.models import Category, CategoryRule, Person, Transaction

SURE = 0.8  # at or above: applied; below: suggested
SORTABLE_KINDS = (TxnKind.expense, TxnKind.fee)

# ---------- payee keys ----------

_NOISE = {
    "upi", "imps", "neft", "rtgs", "pos", "ach", "nach", "ecom", "vps", "mmt", "bil", "billpay",
    "ib", "inb", "mb", "tpt", "dr", "cr", "to", "by", "from", "for", "payment", "paid", "pay",
    "txn", "ref", "no", "the", "pvt", "ltd", "private", "limited", "india", "in", "com", "www",
    "co", "p2a", "p2m", "collect", "request", "autopay", "si", "debit", "credit", "card", "via",
    "on", "at", "and", "of", "rs", "inr", "transfer", "trf", "online", "bank", "a", "c", "ac",
}  # fmt: skip
_HANDLE = re.compile(r"@[a-z0-9.]+")  # the bank part of a UPI id: name@ybl
_NOT_WORD = re.compile(r"[^a-z0-9]+")


def payee_key(text: str | None) -> str:
    """'UPI-SWIGGY-8834551@ybl-Food order' -> 'swiggy food order'. Up to three plain words."""
    raw = _HANDLE.sub(" ", (text or "").lower())
    words = []
    for w in _NOT_WORD.sub(" ", raw).split():
        if w in _NOISE or len(w) < 2 or sum(ch.isdigit() for ch in w) >= 3:
            continue
        words.append(w)
    return " ".join(words[:3])[:60]


def _covers(match: str, key: str) -> bool:
    return key == match or key.startswith(match + " ")


# ---------- built-in knowledge ----------

# (word in the payee, "Parent / Child"). Longer phrases are tried first.
MERCHANTS: tuple[tuple[str, str], ...] = (
    # food delivery and dining
    ("swiggy instamart", "Household / Groceries"),
    ("instamart", "Household / Groceries"),
    ("swiggy", "Lifestyle / Food delivery"),
    ("zomato", "Lifestyle / Food delivery"),
    ("eatsure", "Lifestyle / Food delivery"),
    ("faasos", "Lifestyle / Food delivery"),
    ("box8", "Lifestyle / Food delivery"),
    ("dominos", "Lifestyle / Food delivery"),
    ("pizza hut", "Lifestyle / Dining out"),
    ("mcdonalds", "Lifestyle / Dining out"),
    ("kfc", "Lifestyle / Dining out"),
    ("starbucks", "Lifestyle / Dining out"),
    ("cafe coffee day", "Lifestyle / Dining out"),
    ("chaayos", "Lifestyle / Dining out"),
    ("haldiram", "Lifestyle / Dining out"),
    # groceries
    ("blinkit", "Household / Groceries"),
    ("grofers", "Household / Groceries"),
    ("zepto", "Household / Groceries"),
    ("bigbasket", "Household / Groceries"),
    ("bbnow", "Household / Groceries"),
    ("dmart", "Household / Groceries"),
    ("avenue supermarts", "Household / Groceries"),
    ("jiomart", "Household / Groceries"),
    ("reliance fresh", "Household / Groceries"),
    ("reliance smart", "Household / Groceries"),
    ("more retail", "Household / Groceries"),
    ("spencers", "Household / Groceries"),
    ("ratnadeep", "Household / Groceries"),
    ("nature basket", "Household / Groceries"),
    ("country delight", "Household / Milk & daily needs"),
    ("milkbasket", "Household / Milk & daily needs"),
    ("nandini", "Household / Milk & daily needs"),
    ("akshayakalpa", "Household / Milk & daily needs"),
    # transport
    ("uber", "Transport / Cab & auto"),
    ("ola", "Transport / Cab & auto"),
    ("olacabs", "Transport / Cab & auto"),
    ("rapido", "Transport / Cab & auto"),
    ("namma yatri", "Transport / Cab & auto"),
    ("bmrcl", "Transport / Metro & bus"),
    ("metro", "Transport / Metro & bus"),
    ("bmtc", "Transport / Metro & bus"),
    ("indian oil", "Transport / Fuel"),
    ("iocl", "Transport / Fuel"),
    ("hpcl", "Transport / Fuel"),
    ("hindustan petroleum", "Transport / Fuel"),
    ("bpcl", "Transport / Fuel"),
    ("bharat petroleum", "Transport / Fuel"),
    ("shell", "Transport / Fuel"),
    ("nayara", "Transport / Fuel"),
    ("fastag", "Transport / Parking & tolls"),
    # shopping
    ("amazon", "Lifestyle / Shopping"),
    ("amzn", "Lifestyle / Shopping"),
    ("flipkart", "Lifestyle / Shopping"),
    ("myntra", "Lifestyle / Shopping"),
    ("ajio", "Lifestyle / Shopping"),
    ("meesho", "Lifestyle / Shopping"),
    ("tata cliq", "Lifestyle / Shopping"),
    ("croma", "Lifestyle / Shopping"),
    ("reliance digital", "Lifestyle / Shopping"),
    ("ikea", "Lifestyle / Shopping"),
    ("decathlon", "Lifestyle / Shopping"),
    ("lifestyle stores", "Lifestyle / Shopping"),
    ("westside", "Lifestyle / Shopping"),
    ("max fashion", "Lifestyle / Shopping"),
    ("firstcry", "Kids / Clothes & shoes"),
    ("hamleys", "Kids / Toys & fun"),
    ("nykaa", "Lifestyle / Personal care"),
    ("urban company", "Lifestyle / Personal care"),
    # entertainment and subscriptions
    ("netflix", "Utilities / DTH / OTT"),
    ("hotstar", "Utilities / DTH / OTT"),
    ("disney", "Utilities / DTH / OTT"),
    ("prime video", "Utilities / DTH / OTT"),
    ("spotify", "Utilities / DTH / OTT"),
    ("youtube", "Utilities / DTH / OTT"),
    ("sonyliv", "Utilities / DTH / OTT"),
    ("zee5", "Utilities / DTH / OTT"),
    ("jiocinema", "Utilities / DTH / OTT"),
    ("tata play", "Utilities / DTH / OTT"),
    ("tatasky", "Utilities / DTH / OTT"),
    ("bookmyshow", "Lifestyle / Entertainment"),
    ("pvr", "Lifestyle / Entertainment"),
    ("inox", "Lifestyle / Entertainment"),
    # utilities
    ("jio", "Utilities / Mobile"),
    ("airtel", "Utilities / Mobile"),
    ("vodafone", "Utilities / Mobile"),
    ("bsnl", "Utilities / Mobile"),
    ("act fibernet", "Utilities / Broadband"),
    ("hathway", "Utilities / Broadband"),
    ("bescom", "Utilities / Electricity"),
    ("tneb", "Utilities / Electricity"),
    ("tangedco", "Utilities / Electricity"),
    ("msedcl", "Utilities / Electricity"),
    ("tata power", "Utilities / Electricity"),
    ("adani electricity", "Utilities / Electricity"),
    ("apspdcl", "Utilities / Electricity"),
    ("apepdcl", "Utilities / Electricity"),
    ("tsspdcl", "Utilities / Electricity"),
    ("bwssb", "Utilities / Water"),
    ("indane", "Household / Gas cylinder"),
    ("bharatgas", "Household / Gas cylinder"),
    ("hp gas", "Household / Gas cylinder"),
    # health
    ("apollo", "Health / Medicines"),
    ("pharmeasy", "Health / Medicines"),
    ("1mg", "Health / Medicines"),
    ("netmeds", "Health / Medicines"),
    ("medplus", "Health / Medicines"),
    ("practo", "Health / Doctor"),
    ("thyrocare", "Health / Lab tests"),
    ("dr lal", "Health / Lab tests"),
    # travel
    ("irctc", "Lifestyle / Travel"),
    ("makemytrip", "Lifestyle / Travel"),
    ("goibibo", "Lifestyle / Travel"),
    ("cleartrip", "Lifestyle / Travel"),
    ("ixigo", "Lifestyle / Travel"),
    ("redbus", "Lifestyle / Travel"),
    ("indigo", "Lifestyle / Travel"),
    ("air india", "Lifestyle / Travel"),
    ("akasa", "Lifestyle / Travel"),
    ("oyo", "Lifestyle / Travel"),
    ("airbnb", "Lifestyle / Travel"),
    # insurance and investing
    ("lic", "Insurance / Term life"),
    ("max life", "Insurance / Term life"),
    ("hdfc life", "Insurance / Term life"),
    ("icici pru", "Insurance / Term life"),
    ("hdfc ergo", "Insurance / Health insurance"),
    ("star health", "Insurance / Health insurance"),
    ("niva bupa", "Insurance / Health insurance"),
    ("care health", "Insurance / Health insurance"),
    ("acko", "Insurance / Vehicle"),
    ("zerodha", "Savings & investments / SIP / mutual funds"),
    ("groww", "Savings & investments / SIP / mutual funds"),
    ("kuvera", "Savings & investments / SIP / mutual funds"),
    ("bse star", "Savings & investments / SIP / mutual funds"),
    # education
    ("byjus", "Education / Tuition"),
    ("vedantu", "Education / Tuition"),
    ("unacademy", "Education / Tuition"),
)

# Item words in a description. A child named in the text sends kid-ish items to Kids.
KID_ITEMS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("uniform", "shoes", "shoe", "clothes", "dress", "shirt", "jeans", "frock"),
        "Clothes & shoes",
    ),
    (("toy", "toys", "lego", "game", "games", "cycle", "bicycle", "doll"), "Toys & fun"),
    (
        ("fee", "fees", "school", "tuition", "books", "book", "stationery", "exam", "bag"),
        "School & tuition",
    ),
    (
        (
            "class", "classes", "football", "cricket", "swimming", "dance", "music", "karate",
            "coaching", "skating", "chess", "art", "drawing", "camp", "sports",
        ),
        "Classes & sports",
    ),
    (("doctor", "vaccine", "vaccination", "medicine", "medicines", "paediatric"), "Health"),
    (("pocket", "allowance"), "Pocket money"),
)  # fmt: skip
ITEMS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("grocery", "groceries", "vegetables", "veggies", "fruits", "kirana"),
        "Household / Groceries",
    ),
    (("milk", "curd", "bread", "eggs"), "Household / Milk & daily needs"),
    (("maid", "cook", "nanny", "driver salary"), "Household / Domestic help"),
    (("petrol", "diesel", "fuel", "cng"), "Transport / Fuel"),
    (("cab", "auto", "taxi"), "Transport / Cab & auto"),
    (("toll", "parking"), "Transport / Parking & tolls"),
    (("service", "servicing", "tyre", "tyres"), "Transport / Car service"),
    (("doctor", "clinic", "hospital", "consultation"), "Health / Doctor"),
    (("medicine", "medicines", "pharmacy", "chemist"), "Health / Medicines"),
    (
        ("lunch", "dinner", "breakfast", "restaurant", "cafe", "coffee", "snacks"),
        "Lifestyle / Dining out",
    ),
    (("movie", "movies", "concert"), "Lifestyle / Entertainment"),
    (("haircut", "salon", "parlour", "spa"), "Lifestyle / Personal care"),
    (("trip", "hotel", "flight", "train", "bus ticket"), "Lifestyle / Travel"),
    (("gift", "gifts"), "Family & festivals / Gifts"),
    (("diwali", "pooja", "puja", "festival", "rakhi"), "Family & festivals / Festivals"),
    (("donation", "temple"), "Family & festivals / Donations"),
    (("rent",), "Housing / Rent"),
    (("maintenance",), "Housing / Maintenance"),
    (("electricity", "power bill"), "Utilities / Electricity"),
    (("recharge", "mobile bill"), "Utilities / Mobile"),
    (("broadband", "wifi", "internet"), "Utilities / Broadband"),
    (("uniform", "school"), "Education / Books & uniform"),
    (("fee", "fees"), "Education / School fees"),
    (("tuition",), "Education / Tuition"),
    (("toy", "toys", "lego"), "Kids / Toys & fun"),
)  # fmt: skip


# ---------- the household's categories and people ----------


@dataclass
class Book:
    """Category paths and people names for one household, loaded once per request."""

    paths: dict[str, uuid.UUID] = field(default_factory=dict)  # "parent / child" -> id
    names: dict[str, uuid.UUID] = field(default_factory=dict)  # "child" or "parent" -> id
    labels: dict[uuid.UUID, str] = field(default_factory=dict)
    kinds: dict[uuid.UUID, CategoryKind] = field(default_factory=dict)
    people: dict[str, uuid.UUID] = field(default_factory=dict)  # lower name -> id
    children: set[uuid.UUID] = field(default_factory=set)  # people who are kids
    rules: list[CategoryRule] = field(default_factory=list)
    other_id: uuid.UUID | None = None

    def path(self, label: str) -> uuid.UUID | None:
        want = label.strip().lower()
        if want in self.paths:
            return self.paths[want]
        if "/" in want:
            want = want.split("/")[-1].strip()
        return self.names.get(want)


async def load_book(session: AsyncSession, household_id: uuid.UUID) -> Book:
    book = Book()
    cats = (
        await session.scalars(
            select(Category).where(
                Category.household_id == household_id, Category.archived.is_(False)
            )
        )
    ).all()
    by_id = {c.id: c for c in cats}
    for c in cats:
        parent = by_id.get(c.parent_id) if c.parent_id else None
        label = f"{parent.name} / {c.name}" if parent else c.name
        book.paths[label.lower()] = c.id
        book.labels[c.id] = label
        book.kinds[c.id] = c.kind
        book.names.setdefault(c.name.lower(), c.id)
        if parent is None and c.name == "Other":
            book.other_id = c.id
    for p in (
        await session.scalars(
            select(Person).where(Person.household_id == household_id, Person.archived.is_(False))
        )
    ).all():
        book.people[p.name.lower()] = p.id
        if p.relation.value == "child":
            book.children.add(p.id)
    book.rules = sorted(
        (
            await session.scalars(
                select(CategoryRule).where(CategoryRule.household_id == household_id)
            )
        ).all(),
        key=lambda r: -len(r.match),
    )
    return book


# ---------- guessing ----------


@dataclass
class Guess:
    category_id: uuid.UUID | None = None
    person_id: uuid.UUID | None = None
    confidence: float = 0.0
    source: str = ""  # learned | ai | builtin | words | ""
    key: str = ""

    @property
    def sure(self) -> bool:
        return self.category_id is not None and self.confidence >= SURE


def _words(text: str) -> set[str]:
    return set(_NOT_WORD.sub(" ", text.lower()).split())


def _has(text_words: set[str], text: str, phrase: str) -> bool:
    return phrase in text_words if " " not in phrase else f" {phrase} " in f" {text} "


def guess(
    book: Book, payee: str | None, note: str | None = "", person_id: uuid.UUID | None = None
) -> Guess:
    """Best category and person for an entry from its payee and note (and the person already
    chosen for it, if any). Never touches the DB."""
    key = payee_key(payee)
    text = " ".join(_NOT_WORD.sub(" ", f"{payee or ''} {note or ''}".lower()).split())
    words = _words(text)
    out = Guess(key=key)
    person = person_id or next((pid for name, pid in book.people.items() if name in words), None)
    out.person_id = person

    for rule in book.rules:
        if rule.source is RuleSource.suggested or not key or not _covers(rule.match, key):
            continue
        if rule.category_id is None and rule.person_id is None:
            continue
        out.category_id = rule.category_id
        out.person_id = person or rule.person_id
        out.confidence = 0.95 if rule.source is RuleSource.learned else 0.85
        out.source = rule.source.value
        return out

    if person is not None and person in book.children:
        for item_words, child in KID_ITEMS:
            if any(w in words for w in item_words):
                cat = book.path(f"Kids / {child}")
                if cat:
                    out.category_id, out.confidence, out.source = cat, 0.9, "words"
                    return out

    for phrase, label in MERCHANTS:
        if _has(words, text, phrase):
            cat = book.path(label)
            if cat:
                out.category_id, out.confidence, out.source = cat, 0.9, "builtin"
                return out

    for item_words, label in ITEMS:
        if any(_has(words, text, w) for w in item_words):
            cat = book.path(label)
            if cat:
                out.category_id, out.confidence, out.source = cat, 0.8, "words"
                return out
    if person is not None:
        out.confidence = 0.5
    return out


# ---------- learning ----------


async def learn(
    session: AsyncSession,
    household_id: uuid.UUID,
    payee: str | None,
    category_id: uuid.UUID | None,
    person_id: uuid.UUID | None = None,
    source: RuleSource = RuleSource.learned,
) -> CategoryRule | None:
    """Remember 'this payee goes here'. Does not commit."""
    key = payee_key(payee)
    if not key or (category_id is None and person_id is None):
        return None
    rule = await session.scalar(
        select(CategoryRule).where(
            CategoryRule.household_id == household_id, CategoryRule.match == key
        )
    )
    if rule is None:
        rule = CategoryRule(household_id=household_id, match=key, hits=0)
        session.add(rule)
    elif rule.source is RuleSource.learned and source is not RuleSource.learned:
        return rule  # the household's own correction wins over the assistant's guess
    rule.category_id = category_id
    rule.person_id = person_id
    rule.source = source
    rule.hits = (rule.hits or 0) + 1
    rule.updated_at = datetime.now(UTC)
    await session.flush()
    return rule


async def _candidates(
    session: AsyncSession, household_id: uuid.UUID, only_unsorted: bool = True
) -> list[Transaction]:
    rows = (
        await session.scalars(
            select(Transaction).where(
                Transaction.household_id == household_id,
                Transaction.deleted_at.is_(None),
                Transaction.direction == Direction.debit,
                Transaction.kind.in_(SORTABLE_KINDS),
                Transaction.transfer_group_id.is_(None),
            )
        )
    ).all()
    if not only_unsorted:
        return list(rows)
    other = await session.scalar(
        select(Category.id).where(
            Category.household_id == household_id,
            Category.parent_id.is_(None),
            Category.name == "Other",
        )
    )
    return [r for r in rows if r.category_id is None or r.category_id == other]


async def similar(
    session: AsyncSession, household_id: uuid.UUID, key: str, category_id: uuid.UUID | None
) -> list[Transaction]:
    """Other sortable entries from the same payee that sit in a different category."""
    if not key:
        return []
    return [
        t
        for t in await _candidates(session, household_id, only_unsorted=False)
        if t.category_id != category_id and _covers(key, payee_key(t.payee))
    ]


async def apply_to(
    rows: list[Transaction], category_id: uuid.UUID | None, person_id: uuid.UUID | None
) -> dict[str, Any]:
    """Set category (and person, where none) on ``rows``; returns an undo map."""
    undo: dict[str, Any] = {}
    now = datetime.now(UTC)
    for t in rows:
        undo[str(t.id)] = [
            str(t.category_id) if t.category_id else None,
            str(t.person_id) if t.person_id else None,
        ]
        if category_id is not None:
            t.category_id = category_id
        if person_id is not None and t.person_id is None:
            t.person_id = person_id
        t.updated_at = now
    return undo


async def restore(session: AsyncSession, household_id: uuid.UUID, undo: dict[str, Any]) -> int:
    if not undo:
        return 0
    ids = [uuid.UUID(i) for i in undo]
    count = 0
    for t in (await session.scalars(select(Transaction).where(Transaction.id.in_(ids)))).all():
        if t.household_id != household_id:
            continue
        cat, person = undo[str(t.id)]
        t.category_id = uuid.UUID(cat) if cat else None
        t.person_id = uuid.UUID(person) if person else None
        count += 1
    return count


# ---------- sorting the books ----------

AI_SYSTEM = """You sort an Indian household's bank and card entries into its own categories.
For each payee key, pick the single best category label exactly as listed (or "" if you can't
tell), the person it is for when a listed name is clearly meant (else ""), and your confidence
from 0 to 1. Food apps (Swiggy, Zomato) are Food delivery; quick-commerce and supermarkets are
Groceries. A person's name alone (a UPI transfer to an individual) is usually unknowable: give
low confidence. Never invent categories."""

AI_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "category", "person", "confidence"],
                "properties": {
                    "key": {"type": "string"},
                    "category": {"type": "string"},
                    "person": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        }
    },
}

AI_BATCH = 40
AI_MAX_KEYS = 200


@dataclass
class RunResult:
    by_rules: int = 0
    by_ai: int = 0
    suggested: int = 0
    left: int = 0
    ai_used: bool = False
    undo: dict[str, Any] = field(default_factory=dict)


async def run(session: AsyncSession, household_id: uuid.UUID, llm: Any | None) -> RunResult:
    """Sort every uncategorised (or 'Other') spend entry. Does not commit."""
    book = await load_book(session, household_id)
    rows = await _candidates(session, household_id)
    out = RunResult()
    unknown: dict[str, list[Transaction]] = defaultdict(list)
    for t in rows:
        g = guess(book, t.payee, t.note)
        if g.sure:
            out.undo |= await apply_to([t], g.category_id, g.person_id)
            out.by_rules += 1
        elif g.key:
            unknown[g.key].append(t)

    if llm is not None and unknown:
        keys = sorted(unknown, key=lambda k: -sum(t.amount for t in unknown[k]))[:AI_MAX_KEYS]
        labels = [
            label
            for cid, label in sorted(book.labels.items(), key=lambda kv: kv[1])
            if book.kinds[cid] in (CategoryKind.expense, CategoryKind.debt)
        ]
        people = [name.title() for name in book.people]
        for start in range(0, len(keys), AI_BATCH):
            batch = keys[start : start + AI_BATCH]
            prompt = (
                "Categories:\n"
                + "\n".join(f"- {label}" for label in labels)
                + "\n\nPeople: "
                + (", ".join(people) or "(none)")
                + "\n\nPayee keys (with a sample description and how much was spent):\n"
                + "\n".join(
                    f"- {k} | {unknown[k][0].payee[:60]} | {len(unknown[k])} entries, "
                    f"{sum(t.amount for t in unknown[k]):,.0f} spent"
                    for k in batch
                )
            )
            result = await llm.extract(AI_SYSTEM, prompt, [], AI_SCHEMA)
            out.ai_used = True
            for item in result.get("items") or []:
                key = str(item.get("key") or "")
                if key not in unknown:
                    continue
                cat = book.path(str(item.get("category") or "")) if item.get("category") else None
                person = book.people.get(str(item.get("person") or "").lower())
                try:
                    conf = float(item.get("confidence") or 0)
                except (TypeError, ValueError):
                    conf = 0.0
                if cat is None:
                    continue
                if conf >= SURE:
                    await learn(session, household_id, key, cat, person, RuleSource.ai)
                    out.undo |= await apply_to(unknown.pop(key), cat, person)
                    out.by_ai += 1
                else:
                    await learn(session, household_id, key, cat, person, RuleSource.suggested)
                    out.suggested += 1
    out.left = sum(len(v) for v in unknown.values())
    return out


async def review(session: AsyncSession, household_id: uuid.UUID) -> list[dict[str, Any]]:
    """Payees still waiting for a category, biggest first, with any suggestion."""
    book = await load_book(session, household_id)
    groups: dict[str, list[Transaction]] = defaultdict(list)
    for t in await _candidates(session, household_id):
        key = payee_key(t.payee)
        if key:
            groups[key].append(t)
    suggestions = {r.match: r for r in book.rules if r.source is RuleSource.suggested}
    out: list[dict[str, Any]] = []
    for key, rows in groups.items():
        hint = next((r for m, r in suggestions.items() if _covers(m, key)), None)
        g = guess(book, rows[0].payee, rows[0].note)
        out.append(
            {
                "key": key,
                "label": max(
                    {t.payee for t in rows}, key=lambda p: sum(t.payee == p for t in rows)
                ),
                "count": len(rows),
                "total": sum((t.amount for t in rows), Decimal("0")),
                "last_date": max(t.date for t in rows),
                "suggested_category_id": hint.category_id if hint else g.category_id,
                "suggested_person_id": hint.person_id if hint else g.person_id,
            }
        )
    out.sort(key=lambda g: -g["total"])
    return out[:100]
