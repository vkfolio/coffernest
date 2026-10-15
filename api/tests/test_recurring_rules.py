from datetime import date

import pytest

from coffernest.services.recurring import normalize_rrule, occurrences, validate_rrule


def test_day_31_clamps_to_month_end() -> None:
    days = occurrences(
        "FREQ=MONTHLY;BYMONTHDAY=31", date(2027, 1, 1), date(2027, 1, 1), date(2027, 4, 30)
    )
    assert days == [date(2027, 1, 31), date(2027, 2, 28), date(2027, 3, 31), date(2027, 4, 30)]
    assert normalize_rrule("rrule:freq=monthly;bymonthday=30") == (
        "FREQ=MONTHLY;BYMONTHDAY=30,-1;BYSETPOS=1"
    )


def test_last_day_of_month() -> None:
    days = occurrences(
        "FREQ=MONTHLY;BYMONTHDAY=-1", date(2028, 1, 1), date(2028, 1, 1), date(2028, 3, 31)
    )
    assert days == [date(2028, 1, 31), date(2028, 2, 29), date(2028, 3, 31)]


def test_school_fee_terms_and_skips() -> None:
    rule = "FREQ=YEARLY;BYMONTH=1,4,7,10;BYMONTHDAY=10"
    days = occurrences(
        rule, date(2026, 4, 1), date(2026, 4, 1), date(2027, 3, 31), skipped=[date(2026, 7, 10)]
    )
    assert days == [date(2026, 4, 10), date(2026, 10, 10), date(2027, 1, 10)]


def test_end_date_and_validation() -> None:
    days = occurrences(
        "FREQ=MONTHLY;BYMONTHDAY=5",
        date(2026, 1, 1),
        date(2026, 1, 1),
        date(2026, 12, 31),
        end=date(2026, 3, 5),
    )
    assert days == [date(2026, 1, 5), date(2026, 2, 5), date(2026, 3, 5)]
    with pytest.raises(ValueError):
        validate_rrule("FREQ=SOMETIMES", date(2026, 1, 1))
