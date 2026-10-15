"""Short-lived JWT access tokens and opaque refresh tokens."""

from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass

import jwt

from coffernest.config import Settings
from coffernest.enums import Role

_ALG = "HS256"


@dataclass(frozen=True)
class AccessClaims:
    user_id: uuid.UUID
    member_id: uuid.UUID
    household_id: uuid.UUID
    role: Role
    token_version: int


def issue_access(settings: Settings, claims: AccessClaims) -> str:
    now = int(time.time())
    payload = {
        "sub": str(claims.user_id),
        "mid": str(claims.member_id),
        "hid": str(claims.household_id),
        "role": claims.role.value,
        "ver": claims.token_version,
        "typ": "access",
        "iat": now,
        "exp": now + settings.access_ttl_s,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALG)


def decode_access(settings: Settings, token: str) -> AccessClaims | None:
    try:
        data = jwt.decode(token, settings.jwt_secret, algorithms=[_ALG])
        if data.get("typ") != "access":
            return None
        return AccessClaims(
            user_id=uuid.UUID(data["sub"]),
            member_id=uuid.UUID(data["mid"]),
            household_id=uuid.UUID(data["hid"]),
            role=Role(data["role"]),
            token_version=int(data["ver"]),
        )
    except (jwt.PyJWTError, KeyError, ValueError):
        return None


PAT_PREFIX = "cn_pat_"
# Tokens made before the rename still work.
PAT_PREFIXES = (PAT_PREFIX, "vkf_pat_")
SCOPE_READ = "finance.read"
SCOPE_WRITE = "finance.write"


@dataclass(frozen=True)
class AppClaims:
    """Who an MCP/OAuth token speaks for. Never accepted by the web API."""

    user_id: uuid.UUID
    member_id: uuid.UUID
    household_id: uuid.UUID
    role: Role
    token_version: int
    client_id: str
    scopes: tuple[str, ...]
    resource: str
    expires_at: int


def issue_app_token(settings: Settings, claims: AppClaims) -> str:
    now = int(time.time())
    payload = {
        "sub": str(claims.user_id),
        "mid": str(claims.member_id),
        "hid": str(claims.household_id),
        "role": claims.role.value,
        "ver": claims.token_version,
        "typ": "mcp",
        "cid": claims.client_id,
        "scope": " ".join(claims.scopes),
        "aud": claims.resource,
        "iat": now,
        "exp": claims.expires_at,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALG)


def decode_app_token(settings: Settings, token: str, resource: str) -> AppClaims | None:
    try:
        data = jwt.decode(token, settings.jwt_secret, algorithms=[_ALG], audience=resource)
        if data.get("typ") != "mcp":
            return None
        return AppClaims(
            user_id=uuid.UUID(data["sub"]),
            member_id=uuid.UUID(data["mid"]),
            household_id=uuid.UUID(data["hid"]),
            role=Role(data["role"]),
            token_version=int(data["ver"]),
            client_id=str(data["cid"]),
            scopes=tuple(str(data.get("scope", "")).split()),
            resource=str(data["aud"]),
            expires_at=int(data["exp"]),
        )
    except (jwt.PyJWTError, KeyError, ValueError):
        return None


def new_opaque() -> str:
    return secrets.token_urlsafe(32)


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
