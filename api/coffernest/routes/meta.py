"""/api/v1/meta — what this server offers, readable before sign-in (apps check it first)."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from coffernest.deps import Session, SettingsDep
from coffernest.routes.auth import has_real_users

router = APIRouter(tags=["meta"])

VERSION = "0.2.0"
PASSWORD_MIN_LENGTH = 10


class MetaOut(BaseModel):
    name: str
    version: str
    registration_open: bool
    # Nobody has signed up yet (the server's own demo account doesn't count): the next account
    # becomes this server's admin.
    first_user: bool
    ai_available: bool
    ai_provider: str
    password_min_length: int
    account_deletion: bool
    privacy_url: str
    terms_url: str
    source_url: str


@router.get("/meta", response_model=MetaOut)
async def meta(settings: SettingsDep, session: Session) -> MetaOut:
    origin = settings.public_url.rstrip("/")
    return MetaOut(
        name="Coffernest",
        version=VERSION,
        registration_open=settings.allow_registration,
        first_user=not await has_real_users(session, settings),
        ai_available=settings.ai_provider != "none",
        ai_provider=settings.ai_provider,
        password_min_length=PASSWORD_MIN_LENGTH,
        account_deletion=True,
        privacy_url=settings.privacy_url or f"{origin}/privacy",
        terms_url=settings.terms_url or f"{origin}/terms",
        source_url="https://github.com/vkfolio/coffernest",
    )
