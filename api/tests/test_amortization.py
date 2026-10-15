from datetime import date
from decimal import Decimal as D

from coffernest.enums import PrepayMode
from coffernest.services import amortization as am

HOME = am.LoanTerms(D("5000000"), D("8.5"), 240, date(2026, 11, 5))


def test_emi_matches_emicalculator() -> None:
    # emicalculator.net: ₹50 L, 8.5 %, 20 years → ₹43,391
    assert am.emi(D("5000000"), D("8.5"), 240) == D("43391")
    # ₹10 L, 10.5 %, 5 years → ₹21,494
    assert am.emi(D("1000000"), D("10.5"), 60) == D("21494")
    assert am.emi(D("120000"), D("0"), 12) == D("10000")


def test_schedule_repays_exactly_in_tenure() -> None:
    sched = am.build_schedule(HOME)
    assert len(sched.rows) == 240
    assert sched.rows[-1].closing == 0
    assert sum(r.principal for r in sched.rows) == D("5000000")
    first = sched.rows[0]
    assert first.interest == D("35416.67")  # 50 L × 8.5 % / 12
    assert first.principal == D("43391") - D("35416.67")
    # within ₹100 of emicalculator's total interest (54,13,879, which uses the unrounded EMI)
    assert abs(sched.total_interest - D("5413879")) < 100


def test_prepayment_reduce_tenure_vs_reduce_emi() -> None:
    lump = am.PrepayEvent(date(2028, 1, 1), D("500000"), PrepayMode.reduce_tenure)
    tenure = am.build_schedule(am.LoanTerms(**{**HOME.__dict__, "prepayments": [lump]}))
    emi_mode = am.build_schedule(
        am.LoanTerms(
            **{
                **HOME.__dict__,
                "prepayments": [am.PrepayEvent(lump.date, lump.amount, PrepayMode.reduce_emi)],
            }
        )
    )
    base = am.build_schedule(HOME)
    assert len(tenure.rows) < 240  # finishes years earlier
    assert len(emi_mode.rows) == 240  # same end date …
    assert emi_mode.rows[30].emi < base.rows[30].emi  # … lower EMI
    # Cutting tenure saves more interest than cutting the EMI.
    assert tenure.total_interest < emi_mode.total_interest < base.total_interest
    applied = next(r for r in tenure.rows if r.prepayment > 0)
    assert applied.due_date == date(2028, 1, 5)


def test_rate_hike_keep_emi_extends_tenure() -> None:
    hike = am.RateEvent(date(2027, 6, 1), D("9.5"), keep_emi=True)
    sched = am.build_schedule(am.LoanTerms(**{**HOME.__dict__, "rate_events": [hike]}))
    assert len(sched.rows) > 240
    assert {r.emi for r in sched.rows[:-1]} == {D("43391")}
    assert sched.rows[-1].closing == 0

    reprice = am.RateEvent(date(2027, 6, 1), D("9.5"), keep_emi=False)
    sched2 = am.build_schedule(am.LoanTerms(**{**HOME.__dict__, "rate_events": [reprice]}))
    assert len(sched2.rows) == 240
    assert sched2.rows[-2].emi > D("43391")


def test_outstanding_on() -> None:
    sched = am.build_schedule(HOME)
    assert sched.outstanding_on(date(2026, 10, 1)) == D("5000000")
    assert sched.outstanding_on(date(2026, 11, 5)) == sched.rows[0].closing
    assert sched.outstanding_on(date(2060, 1, 1)) == 0


def test_with_extra_monthly_and_compare() -> None:
    base = am.build_schedule(HOME)
    extra = am.build_schedule(am.with_extra(HOME, extra_monthly=D("5000")))
    cmp = am.compare(base, extra)
    assert cmp.interest_saved > D("1000000")
    assert cmp.months_saved > 50


def test_payoff_avalanche_beats_snowball_on_interest() -> None:
    debts = [
        am.Debt("card", "Card", D("80000"), D("42"), D("4000")),
        am.Debt("personal", "Personal", D("300000"), D("13"), D("10000")),
        am.Debt("car", "Car", D("50000"), D("9"), D("5000")),
    ]
    base = am.plan_payoff(debts, D("0"), "none")
    aval = am.plan_payoff(debts, D("10000"), "avalanche")
    snow = am.plan_payoff(debts, D("10000"), "snowball")
    assert aval.total_interest <= snow.total_interest < base.total_interest
    assert aval.months < base.months
    assert aval.order[0].key == "card"  # highest rate cleared first
    assert snow.order[0].key == "car"  # smallest balance cleared first
