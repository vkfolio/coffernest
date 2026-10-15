"""Symmetric encryption for the few secrets we keep (remembered statement passwords)."""

from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from coffernest.config import get_settings


@lru_cache
def _fernet() -> Fernet:
    settings = get_settings()
    secret = settings.secret_enc_key or settings.jwt_secret
    # Any string works as the env value: derive a proper 32-byte Fernet key from it.
    # "vkf-box:" is the original salt; changing it would make every stored secret unreadable.
    key = base64.urlsafe_b64encode(hashlib.sha256(f"vkf-box:{secret}".encode()).digest())
    return Fernet(key)


def seal(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def unseal(token: str) -> str | None:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return None
