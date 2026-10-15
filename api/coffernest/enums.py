"""Domain enumerations shared by models and API schemas."""

from enum import StrEnum


class Role(StrEnum):
    owner = "owner"  # full control incl. members and deletion
    adult = "adult"  # read/write money data
    viewer = "viewer"  # read only


class AccountKind(StrEnum):
    bank = "bank"
    cash = "cash"
    wallet = "wallet"
    credit_card = "credit_card"
    loan = "loan"
    investment = "investment"


LIABILITY_KINDS = frozenset({AccountKind.credit_card, AccountKind.loan})


class CategoryKind(StrEnum):
    expense = "expense"
    income = "income"
    transfer = "transfer"
    debt = "debt"


class Direction(StrEnum):
    debit = "debit"  # money leaves the account (spend; for a liability: payment received lowers it)
    credit = "credit"  # money enters the account


class TxnKind(StrEnum):
    expense = "expense"
    income = "income"
    transfer = "transfer"
    emi = "emi"
    card_payment = "card_payment"
    prepayment = "prepayment"
    refund = "refund"
    fee = "fee"
    interest = "interest"
    adjustment = "adjustment"


class TxnStatus(StrEnum):
    scheduled = "scheduled"  # materialised from a recurring rule, not yet confirmed
    cleared = "cleared"
    reconciled = "reconciled"  # matched against a statement


class TxnSource(StrEnum):
    manual = "manual"
    recurring = "recurring"
    import_ = "import"


class LoanKind(StrEnum):
    home = "home"
    personal = "personal"
    car = "car"
    education = "education"
    gold = "gold"
    other = "other"


class RateType(StrEnum):
    fixed = "fixed"
    floating = "floating"


class PrepayMode(StrEnum):
    reduce_tenure = "reduce_tenure"
    reduce_emi = "reduce_emi"


class CardEmiKind(StrEnum):
    emi_conversion = "emi_conversion"  # a purchase converted to EMI on the card
    loan_on_card = "loan_on_card"  # pre-approved loan disbursed to a bank account


class StatementStatus(StrEnum):
    unpaid = "unpaid"
    partial = "partial"
    min_paid = "min_paid"
    paid = "paid"


class AttachmentKind(StrEnum):
    bill = "bill"
    statement = "statement"
    receipt = "receipt"
    other = "other"


class UploadStatus(StrEnum):
    pending = "pending"  # presigned URL issued, object not confirmed yet
    stored = "stored"


class Relation(StrEnum):
    self_ = "self"
    spouse = "spouse"
    child = "child"
    parent = "parent"
    other = "other"


class RuleSource(StrEnum):
    learned = "learned"  # the household corrected an entry
    ai = "ai"  # the assistant sorted an unknown payee
    suggested = "suggested"  # the assistant's guess, not sure enough: waits in "Help me sort"
