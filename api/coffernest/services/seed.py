"""Default categories for a new Indian household. Colour slots follow the PWA chart palette."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from coffernest.enums import CategoryKind
from coffernest.models import Category

KIDS_CHILDREN = [
    "School & tuition",
    "Classes & sports",
    "Toys & fun",
    "Clothes & shoes",
    "Health",
    "Pocket money",
]

# (name, kind, is_fixed, colour slot, children)
_TREE: list[tuple[str, CategoryKind, bool, int, list[str]]] = [
    ("Housing", CategoryKind.expense, True, 1, ["Rent", "Maintenance", "Property tax", "Repairs"]),
    (
        "Household",
        CategoryKind.expense,
        False,
        2,
        ["Groceries", "Milk & daily needs", "Domestic help", "Gas cylinder", "Household supplies"],
    ),
    (
        "Utilities",
        CategoryKind.expense,
        True,
        7,
        ["Electricity", "Water", "Broadband", "Mobile", "DTH / OTT"],
    ),
    (
        "Education",
        CategoryKind.expense,
        True,
        4,
        ["School fees", "Tuition", "Books & uniform", "School transport"],
    ),
    (
        "Transport",
        CategoryKind.expense,
        False,
        6,
        ["Fuel", "Car service", "Parking & tolls", "Cab & auto", "Metro & bus"],
    ),
    ("Health", CategoryKind.expense, False, 5, ["Doctor", "Medicines", "Lab tests"]),
    ("Insurance", CategoryKind.expense, True, 7, ["Health insurance", "Term life", "Vehicle"]),
    (
        "Lifestyle",
        CategoryKind.expense,
        False,
        5,
        ["Dining out", "Food delivery", "Shopping", "Entertainment", "Travel", "Personal care"],
    ),
    (
        "Kids",
        CategoryKind.expense,
        False,
        4,
        KIDS_CHILDREN,
    ),
    ("Family & festivals", CategoryKind.expense, False, 3, ["Gifts", "Festivals", "Donations"]),
    (
        "Fees & charges",
        CategoryKind.expense,
        False,
        3,
        ["Bank charges", "Card fees", "Interest paid", "Late fees"],
    ),
    (
        "Loan EMI",
        CategoryKind.debt,
        True,
        1,
        ["Home loan EMI", "Personal loan EMI", "Car loan EMI", "Card EMI"],
    ),
    ("Card bill", CategoryKind.transfer, False, 3, []),
    (
        "Savings & investments",
        CategoryKind.transfer,
        True,
        2,
        ["SIP / mutual funds", "PPF / EPF", "Fixed deposit", "Gold"],
    ),
    ("Transfer", CategoryKind.transfer, False, 8, []),
    ("Salary", CategoryKind.income, True, 2, []),
    (
        "Other income",
        CategoryKind.income,
        False,
        2,
        ["Interest earned", "Rental income", "Refunds & cashback", "Bonus"],
    ),
    ("Other", CategoryKind.expense, False, 8, []),
]


async def seed_categories(session: AsyncSession, household_id: uuid.UUID) -> None:
    for name, kind, fixed, slot, children in _TREE:
        parent = Category(
            household_id=household_id, name=name, kind=kind, is_fixed=fixed, color_slot=slot
        )
        session.add(parent)
        await session.flush()
        for child in children:
            session.add(
                Category(
                    household_id=household_id,
                    parent_id=parent.id,
                    name=child,
                    kind=kind,
                    is_fixed=fixed,
                    color_slot=slot,
                )
            )
