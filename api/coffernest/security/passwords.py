"""Argon2id password hashing."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# ~100 ms on a small ARM board; 32 MiB keeps concurrent logins cheap on RAM.
_hasher = PasswordHasher(time_cost=3, memory_cost=32 * 1024, parallelism=2)

# Verified against when the e-mail is unknown, so timing doesn't reveal which accounts exist.
_DUMMY = _hasher.hash("not-a-real-password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored: str | None, password: str) -> bool:
    try:
        return _hasher.verify(stored or _DUMMY, password) and stored is not None
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(stored: str) -> bool:
    return _hasher.check_needs_rehash(stored)
