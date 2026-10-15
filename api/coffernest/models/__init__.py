"""ORM models. Import from here so every table is registered on ``Base.metadata``."""

from coffernest.models.ai import AiSuggestion
from coffernest.models.auth import (
    AuditLog,
    Household,
    Invite,
    LoginThrottle,
    Member,
    RefreshToken,
    User,
)
from coffernest.models.base import Base
from coffernest.models.debt import (
    CardEmi,
    CardStatement,
    CreditCard,
    Loan,
    LoanPrepayment,
    LoanRateChange,
)
from coffernest.models.files import Attachment, Notification, PushSubscription
from coffernest.models.ledger import Account, Category, RecurringRule, Transaction
from coffernest.models.people import CategoryRule, Person
from coffernest.models.v2 import (
    AccessToken,
    ChatMessage,
    ChatThread,
    DocPassword,
    Extraction,
    ExtractionItem,
    OAuthClient,
    OAuthCode,
    OAuthGrant,
    SavedPlan,
    SkillSetting,
)

__all__ = [
    "AccessToken",
    "Account",
    "AiSuggestion",
    "Attachment",
    "AuditLog",
    "Base",
    "CardEmi",
    "CardStatement",
    "Category",
    "CategoryRule",
    "ChatMessage",
    "ChatThread",
    "CreditCard",
    "DocPassword",
    "Extraction",
    "ExtractionItem",
    "Household",
    "Invite",
    "Loan",
    "LoanPrepayment",
    "LoanRateChange",
    "LoginThrottle",
    "Member",
    "Notification",
    "OAuthClient",
    "OAuthCode",
    "OAuthGrant",
    "Person",
    "PushSubscription",
    "RecurringRule",
    "RefreshToken",
    "SavedPlan",
    "SkillSetting",
    "Transaction",
    "User",
]
