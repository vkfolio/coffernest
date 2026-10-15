# ruff: noqa: E501
"""OAuth 2.1 authorization server for MCP clients (claude.ai / Claude Desktop connectors, Codex).

Just enough of the spec for a single household app:
- RFC 9728 protected-resource metadata and RFC 8414 server metadata at /.well-known/…
- RFC 7591 dynamic client registration, redirect URIs limited to Claude's callback and loopback
- authorization code + S256 PKCE only, single-use 60-second codes bound to client/redirect/resource
- rotating refresh tokens; replaying a spent one revokes the whole grant

The sign-in page reuses the app's password, 2FA and lockout rules. Access tokens are JWTs with
``typ=mcp`` and the MCP URL as audience, so they never work against the web API.
"""

from __future__ import annotations

import base64
import hashlib
import html
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from coffernest.deps import Session, SettingsDep
from coffernest.enums import Role
from coffernest.mcp_server import resource_url
from coffernest.models import Member, OAuthClient, OAuthCode, OAuthGrant, User
from coffernest.routes.auth import _audit, _primary_member
from coffernest.security import throttle, totp
from coffernest.security.passwords import verify_password
from coffernest.security.tokens import (
    SCOPE_READ,
    SCOPE_WRITE,
    AppClaims,
    issue_app_token,
    new_opaque,
    sha256,
)

router = APIRouter(tags=["oauth"], include_in_schema=False)

ACCESS_TTL = 3600
REFRESH_TTL = timedelta(days=60)
CODE_TTL = timedelta(seconds=90)
SCOPES = [SCOPE_READ, SCOPE_WRITE, "offline_access"]
_ALLOWED_REDIRECTS = (
    re.compile(r"^https://claude\.(ai|com)/api/mcp/auth_callback$"),
    re.compile(r"^http://(localhost|127\.0\.0\.1)(:\d+)?/[^\s]*$"),
)


def _now() -> datetime:
    return datetime.now(UTC)


def _issuer(settings: SettingsDep) -> str:
    return settings.public_url.rstrip("/")


def _resource_meta(settings: SettingsDep) -> dict[str, Any]:
    return {
        "resource": resource_url(settings),
        "authorization_servers": [_issuer(settings)],
        "scopes_supported": SCOPES,
        "bearer_methods_supported": ["header"],
        "resource_name": "Coffernest",
    }


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
async def protected_resource(settings: SettingsDep) -> JSONResponse:
    return JSONResponse(_resource_meta(settings))


@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/oauth-authorization-server/mcp")
@router.get("/.well-known/openid-configuration")
async def server_meta(settings: SettingsDep) -> JSONResponse:
    base = _issuer(settings)
    return JSONResponse(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "revocation_endpoint": f"{base}/oauth/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "revocation_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": SCOPES,
        }
    )


class RegisterIn(BaseModel):
    redirect_uris: list[str] = Field(min_length=1, max_length=10)
    client_name: str = Field(default="", max_length=120)
    token_endpoint_auth_method: str = "none"
    grant_types: list[str] | None = None
    response_types: list[str] | None = None
    scope: str | None = None


def _redirect_ok(uri: str) -> bool:
    return any(p.match(uri) for p in _ALLOWED_REDIRECTS)


def _oauth_error(code: str, detail: str, http: int = 400) -> JSONResponse:
    return JSONResponse({"error": code, "error_description": detail}, status_code=http)


@router.post("/oauth/register")
async def register(body: RegisterIn, session: Session) -> JSONResponse:
    bad = [u for u in body.redirect_uris if not _redirect_ok(u)]
    if bad:
        return _oauth_error(
            "invalid_redirect_uri", "Only Claude's callback or a localhost redirect is allowed"
        )
    client = OAuthClient(
        client_id=f"cn_{uuid.uuid4().hex}",
        client_name=body.client_name or "MCP client",
        redirect_uris=body.redirect_uris,
    )
    session.add(client)
    await session.commit()
    return JSONResponse(
        {
            "client_id": client.client_id,
            "client_name": client.client_name,
            "redirect_uris": client.redirect_uris,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "client_id_issued_at": int(time.time()),
        },
        status_code=201,
    )


def _match_redirect(client: OAuthClient, uri: str) -> bool:
    if uri in client.redirect_uris:
        return True
    # Loopback clients may use any port (RFC 8252 §7.3).
    parsed = urlparse(uri)
    if parsed.hostname in ("localhost", "127.0.0.1"):
        for known in client.redirect_uris:
            k = urlparse(known)
            if k.hostname == parsed.hostname and k.path == parsed.path:
                return True
    return False


_PAGE = """<!doctype html><html lang="en-IN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connect to Coffernest</title><style>
:root{{--page:#F2F3EE;--block:#E7EAE3;--ink:#18202A;--ink2:#4A5560;--rule:#D3D8CF;
--cover:#16443A;--cover-ink:#EEF3EE;--neg:#B42318;--accent:#1B6A52}}
@media (prefers-color-scheme:dark){{:root{{--page:#0F1916;--block:#16231F;--ink:#E6ECE7;
--ink2:#A8B5AE;--rule:#2A3934;--cover:#1F4D41;--cover-ink:#E8F0EA;--neg:#F2897B;
--accent:#7CC4A4}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--page);color:var(--ink);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;display:grid;
place-items:center;min-height:100dvh;padding:16px}}
main{{width:100%;max-width:400px}}.band{{background:var(--cover);color:var(--cover-ink);
border-radius:20px 20px 0 0;padding:20px 24px}}.band h1{{margin:0;font-size:22px}}
.band p{{margin:4px 0 0;opacity:.85}}form{{background:var(--block);border-radius:0 0 20px 20px;
padding:20px 24px;display:grid;gap:14px}}label{{display:grid;gap:4px;font-size:13px;
color:var(--ink2)}}input[type=email],input[type=password],input[type=text]{{font:inherit;
padding:10px 12px;border:1px solid var(--rule);border-radius:8px;background:var(--page);
color:var(--ink)}}fieldset{{border:0;padding:0;margin:0;display:grid;gap:8px}}
.opt{{display:flex;gap:10px;align-items:flex-start;color:var(--ink);font-size:15px}}
button{{font:inherit;font-weight:600;padding:12px;border:0;border-radius:8px;
background:var(--cover);color:var(--cover-ink);cursor:pointer}}
.err{{color:var(--neg);margin:0}}.small{{font-size:13px;color:var(--ink2);margin:0}}
</style></head><body><main>
<div class="band"><h1>Connect {client}</h1><p>It will be able to read your household's money
in Coffernest{write_note}.</p></div>
<form method="post" action="/oauth/authorize">
{hidden}
{error}
<label>E-mail<input type="email" name="email" value="{email}" required autocomplete="username"></label>
<label>Password<input type="password" name="password" required autocomplete="current-password"></label>
{totp}
<fieldset><legend class="small">What it may do</legend>
<label class="opt"><input type="radio" name="access" value="read" {read_checked}>
<span>Read only: answer questions about balances, loans, cards and plans</span></label>
<label class="opt"><input type="radio" name="access" value="write" {write_checked}>
<span>Read and add: also record entries and repeating bills when you ask</span></label>
</fieldset>
<button type="submit">Allow access</button>
<p class="small">Redirects to {host}. You can disconnect it any time in Settings.</p>
</form></main></body></html>"""


def _page(
    params: dict[str, str],
    client: OAuthClient,
    error: str = "",
    email: str = "",
    need_totp: bool = False,
) -> HTMLResponse:
    hidden = "".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
        for k, v in params.items()
    )
    wants_write = SCOPE_WRITE in params.get("scope", "")
    return HTMLResponse(
        _PAGE.format(
            client=html.escape(client.client_name or "this app"),
            write_note=", and add entries if you allow it" if wants_write else "",
            hidden=hidden,
            error=f'<p class="err">{html.escape(error)}</p>' if error else "",
            email=html.escape(email),
            totp=(
                '<label>Code from your authenticator app<input type="text" name="totp_code" '
                'inputmode="numeric" autocomplete="one-time-code" required></label>'
                if need_totp
                else ""
            ),
            read_checked="" if wants_write else "checked",
            write_checked="checked" if wants_write else "",
            host=html.escape(urlparse(params.get("redirect_uri", "")).netloc or "the app"),
        ),
        headers={"X-Frame-Options": "DENY", "Cache-Control": "no-store"},
    )


_PARAMS = (
    "response_type",
    "client_id",
    "redirect_uri",
    "code_challenge",
    "code_challenge_method",
    "state",
    "scope",
    "resource",
)


async def _check_request(
    session: Session, settings: SettingsDep, params: dict[str, str]
) -> OAuthClient:
    client = await session.get(OAuthClient, params.get("client_id", ""))
    if client is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown app. Add the connector again.")
    if not _match_redirect(client, params.get("redirect_uri", "")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That redirect isn't registered")
    if params.get("response_type") != "code":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only response_type=code is supported")
    if params.get("code_challenge_method") != "S256" or not params.get("code_challenge"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "PKCE with S256 is required")
    resource = params.get("resource") or resource_url(settings)
    if resource.rstrip("/") != resource_url(settings):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown resource")
    return client


@router.get("/oauth/authorize")
async def authorize_page(request: Request, session: Session, settings: SettingsDep) -> HTMLResponse:
    params = {k: request.query_params.get(k, "") for k in _PARAMS}
    client = await _check_request(session, settings, params)
    return _page(params, client)


@router.post("/oauth/authorize", response_model=None)
async def authorize(
    request: Request,
    session: Session,
    settings: SettingsDep,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    access: Annotated[str, Form()] = "read",
    totp_code: Annotated[str, Form()] = "",
) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    params = {k: str(form.get(k, "")) for k in _PARAMS}
    client = await _check_request(session, settings, params)
    email = email.strip().lower()
    keys = [f"ip:{throttle.client_ip(request)}", f"acct:{email}"]
    if await throttle.retry_after(session, keys) > 0:
        return _page(params, client, "Too many attempts. Try again later.", email)
    user = await session.scalar(select(User).where(User.email == email))
    ok = verify_password(user.password_hash if user else None, password)
    if not ok or user is None or not user.is_active:
        await throttle.record_failure(session, keys[0], throttle.PER_IP)
        await throttle.record_failure(session, keys[1], throttle.PER_ACCOUNT)
        _audit(session, request, "oauth_login_failed", user, email=email)
        await session.commit()
        return _page(params, client, "Wrong e-mail or password", email)
    if user.totp_secret:
        if not totp_code:
            return _page(params, client, "", email, need_totp=True)
        if not totp.verify(user, totp_code):
            await throttle.record_failure(session, keys[1], throttle.PER_ACCOUNT)
            await session.commit()
            return _page(params, client, "Wrong verification code", email, need_totp=True)
    await throttle.record_success(session, keys)
    member = await _primary_member(session, user.id)
    scopes = [SCOPE_READ]
    if access == "write" and member.role in (Role.owner, Role.adult):
        scopes.append(SCOPE_WRITE)
    code = new_opaque()
    session.add(
        OAuthCode(
            code_hash=sha256(code),
            client_id=client.client_id,
            user_id=user.id,
            member_id=member.id,
            household_id=member.household_id,
            redirect_uri=params["redirect_uri"],
            code_challenge=params["code_challenge"],
            scopes=scopes,
            resource=resource_url(settings),
            expires_at=_now() + CODE_TTL,
        )
    )
    _audit(
        session,
        request,
        "oauth_authorized",
        user,
        member.household_id,
        client=client.client_name[:60],
        scopes=" ".join(scopes),
    )
    await session.commit()
    query = {"code": code, **({"state": params["state"]} if params.get("state") else {})}
    sep = "&" if "?" in params["redirect_uri"] else "?"
    return RedirectResponse(f"{params['redirect_uri']}{sep}{urlencode(query)}", status_code=302)


def _pkce_ok(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == challenge


async def _tokens(
    session: Session,
    settings: SettingsDep,
    *,
    client_id: str,
    user: User,
    member: Member,
    scopes: list[str],
    family: uuid.UUID,
) -> JSONResponse:
    refresh = new_opaque()
    session.add(
        OAuthGrant(
            client_id=client_id,
            user_id=user.id,
            member_id=member.id,
            household_id=member.household_id,
            family_id=family,
            token_hash=sha256(refresh),
            scopes=scopes,
            resource=resource_url(settings),
            expires_at=_now() + REFRESH_TTL,
        )
    )
    await session.commit()
    access = issue_app_token(
        settings,
        AppClaims(
            user_id=user.id,
            member_id=member.id,
            household_id=member.household_id,
            role=member.role,
            token_version=user.token_version,
            client_id=client_id,
            scopes=tuple(scopes),
            resource=resource_url(settings),
            expires_at=int(time.time()) + ACCESS_TTL,
        ),
    )
    return JSONResponse(
        {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ACCESS_TTL,
            "refresh_token": refresh,
            "scope": " ".join(scopes),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/oauth/token")
async def token(request: Request, session: Session, settings: SettingsDep) -> JSONResponse:
    form = await request.form()
    grant = str(form.get("grant_type", ""))
    client_id = str(form.get("client_id", ""))
    if grant == "authorization_code":
        row = await session.scalar(
            select(OAuthCode)
            .where(OAuthCode.code_hash == sha256(str(form.get("code", ""))))
            .with_for_update()
        )
        if row is None or row.used_at is not None or row.expires_at < _now():
            return _oauth_error("invalid_grant", "The code is invalid or expired")
        row.used_at = _now()
        await session.commit()
        if client_id and client_id != row.client_id:
            return _oauth_error("invalid_grant", "Code was issued to another client")
        if str(form.get("redirect_uri", row.redirect_uri)) != row.redirect_uri:
            return _oauth_error("invalid_grant", "redirect_uri doesn't match")
        if not _pkce_ok(str(form.get("code_verifier", "")), row.code_challenge):
            return _oauth_error("invalid_grant", "PKCE check failed")
        user = await session.get(User, row.user_id)
        member = await session.get(Member, row.member_id)
        if user is None or member is None or not user.is_active:
            return _oauth_error("invalid_grant", "The account is no longer active")
        return await _tokens(
            session,
            settings,
            client_id=row.client_id,
            user=user,
            member=member,
            scopes=list(row.scopes),
            family=uuid.uuid4(),
        )
    if grant == "refresh_token":
        old = await session.scalar(
            select(OAuthGrant)
            .where(OAuthGrant.token_hash == sha256(str(form.get("refresh_token", ""))))
            .with_for_update()
        )
        if old is None or old.expires_at < _now():
            return _oauth_error("invalid_grant", "Unknown or expired refresh token")
        if client_id and client_id != old.client_id:
            return _oauth_error("invalid_grant", "Token was issued to another client")
        if old.revoked_at is not None or old.rotated_at is not None:
            # A spent refresh token came back: assume it leaked and end the whole grant.
            await session.execute(
                update(OAuthGrant)
                .where(OAuthGrant.family_id == old.family_id, OAuthGrant.revoked_at.is_(None))
                .values(revoked_at=_now())
            )
            await session.commit()
            return _oauth_error("invalid_grant", "Refresh token was already used")
        old.rotated_at = _now()
        old.last_used_at = _now()
        user = await session.get(User, old.user_id)
        member = await session.get(Member, old.member_id)
        if user is None or member is None or not user.is_active:
            await session.commit()
            return _oauth_error("invalid_grant", "The account is no longer active")
        return await _tokens(
            session,
            settings,
            client_id=old.client_id,
            user=user,
            member=member,
            scopes=list(old.scopes),
            family=old.family_id,
        )
    return _oauth_error("unsupported_grant_type", "Use authorization_code or refresh_token")


@router.post("/oauth/revoke")
async def revoke(request: Request, session: Session) -> JSONResponse:
    form = await request.form()
    row = await session.scalar(
        select(OAuthGrant).where(OAuthGrant.token_hash == sha256(str(form.get("token", ""))))
    )
    if row is not None:
        await session.execute(
            update(OAuthGrant)
            .where(OAuthGrant.family_id == row.family_id, OAuthGrant.revoked_at.is_(None))
            .values(revoked_at=_now())
        )
        await session.commit()
    return JSONResponse({})
