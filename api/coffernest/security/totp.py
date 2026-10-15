"""TOTP (RFC 6238) second factor with one-time recovery codes.

Adapted from VClaw's gateway/twofactor.py, but state lives on the ``User`` row. The caller commits.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

import pyotp

from coffernest.models import User

ISSUER = "Coffernest"
RECOVERY_COUNT = 10
_STEP_S = 30
_clock = time.time  # patched in tests


def _norm(code: str) -> str:
    return code.replace("-", "").replace(" ", "").strip().lower()


def _hash(code: str) -> str:
    return hashlib.sha256(_norm(code).encode()).hexdigest()


def _new_recovery_codes() -> tuple[list[str], list[str]]:
    plain = [secrets.token_hex(5) for _ in range(RECOVERY_COUNT)]
    return [f"{c[:5]}-{c[5:]}" for c in plain], [_hash(c) for c in plain]


def _match_step(secret: str, code: str, after: int) -> int | None:
    """The 30 s step (now ±1) whose code equals ``code`` and is newer than ``after``."""
    code = code.strip()
    if not (code.isdigit() and len(code) == 6):
        return None
    totp = pyotp.TOTP(secret)
    now = int(_clock()) // _STEP_S
    for step in (now - 1, now, now + 1):
        if step > after and hmac.compare_digest(totp.at(step * _STEP_S), code):
            return step
    return None


def begin_setup(user: User) -> dict[str, str]:
    secret = pyotp.random_base32()
    user.totp_pending = secret
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name=ISSUER)
    return {"secret": secret, "uri": uri}


def confirm_setup(user: User, code: str) -> list[str] | None:
    if not user.totp_pending:
        return None
    step = _match_step(user.totp_pending, code, -1)
    if step is None:
        return None
    shown, hashed = _new_recovery_codes()
    user.totp_secret, user.totp_pending = user.totp_pending, None
    user.totp_last_step = step
    user.recovery_codes = hashed
    return shown


def verify(user: User, code: str) -> bool:
    """A fresh TOTP code, or an unused recovery code (consumed)."""
    if not user.totp_secret:
        return False
    step = _match_step(user.totp_secret, code, user.totp_last_step)
    if step is not None:
        user.totp_last_step = step
        return True
    wanted = _hash(code)
    for stored in user.recovery_codes:
        if hmac.compare_digest(stored, wanted):
            user.recovery_codes = [c for c in user.recovery_codes if c != stored]
            return True
    return False


def disable(user: User) -> None:
    user.totp_secret = user.totp_pending = None
    user.totp_last_step = -1
    user.recovery_codes = []
