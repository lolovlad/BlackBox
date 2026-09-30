from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import jwt
from fastapi import HTTPException, Request, status
from starlette.responses import Response

from .config import HubConfig
from .db import HubRepository


ACCESS_COOKIE = "bb_access"
REFRESH_COOKIE = "bb_refresh"
CSRF_COOKIE = "bb_csrf"


def _encode(user: dict[str, Any], cfg: HubConfig, *, kind: str, ttl: int, sid: str | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": str(user["id"]), "username": user["username"], "role": user["role"], "kind": kind, "iat": now, "exp": now + timedelta(seconds=ttl)}
    if sid:
        payload["sid"] = sid
    return jwt.encode(payload, cfg.jwt_secret, algorithm="HS256")


def _decode(token: str, cfg: HubConfig, expected_kind: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, cfg.jwt_secret, algorithms=["HS256"], leeway=15)
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "invalid_token", "message": "Invalid token"}) from exc
    if payload.get("kind") != expected_kind:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "invalid_token", "message": "Invalid token kind"})
    return payload


def issue_tokens(repo: HubRepository, cfg: HubConfig, user: dict[str, Any]) -> tuple[str, str, str]:
    sid = str(uuid4())
    refresh = _encode(user, cfg, kind="refresh", ttl=cfg.refresh_ttl_seconds, sid=sid)
    repo.create_refresh_session(user["id"], hashlib.sha256(refresh.encode()).hexdigest(), (datetime.now(timezone.utc) + timedelta(seconds=cfg.refresh_ttl_seconds)).isoformat(), sid=sid)
    access = _encode(user, cfg, kind="access", ttl=cfg.access_ttl_seconds)
    return access, refresh, sid


def _bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def current_user(request: Request, repo: HubRepository, cfg: HubConfig) -> dict[str, Any]:
    payload = None
    cookie = request.cookies.get(ACCESS_COOKIE)
    if cookie:
        try:
            payload = _decode(cookie, cfg, "access")
        except HTTPException:
            payload = None
    if payload is None:
        bearer = _bearer_token(request)
        if bearer:
            payload = _decode(bearer, cfg, "access")
    if payload is None:
        raise HTTPException(status_code=401, detail={"code": "auth_required", "message": "Authentication required"})
    user = repo.user_by_id(int(payload["sub"]))
    if user is None:
        raise HTTPException(status_code=401, detail={"code": "user_disabled", "message": "User is disabled"})
    return user


def access_token_ok(request: Request, cfg: HubConfig) -> bool:
    cookie = request.cookies.get(ACCESS_COOKIE)
    if cookie:
        try:
            _decode(cookie, cfg, "access")
            return True
        except HTTPException:
            pass
    bearer = _bearer_token(request)
    if not bearer:
        return False
    try:
        _decode(bearer, cfg, "access")
    except HTTPException:
        return False
    return True


def mint_access_from_refresh_token(token: str, repo: HubRepository, cfg: HubConfig) -> str | None:
    """Issue a new access token from a live refresh session without rotating it.

    Rotation stays on the explicit refresh endpoint. Page loads and parallel
    API calls can all renew the short-lived access cookie from the same refresh
    session, so one expired access token does not log the operator out.
    """
    if not token:
        return None
    try:
        payload = _decode(token, cfg, "refresh")
    except HTTPException:
        return None
    sid = str(payload.get("sid") or "")
    if not sid or not repo.refresh_token_matches(sid, hashlib.sha256(token.encode()).hexdigest()):
        return None
    session = repo.refresh_session(sid)
    if session is None or session.get("disabled"):
        return None
    return _encode(session, cfg, kind="access", ttl=cfg.access_ttl_seconds)


def mint_access_from_refresh(request: Request, repo: HubRepository, cfg: HubConfig) -> str | None:
    return mint_access_from_refresh_token(request.cookies.get(REFRESH_COOKIE, ""), repo, cfg)


def install_request_cookie(request: Request, name: str, value: str) -> None:
    cookies = dict(request.cookies)
    cookies[name] = value
    header = "; ".join(f"{key}={item}" for key, item in cookies.items())
    updated = [(key, item) for key, item in request.scope["headers"] if key.lower() != b"cookie"]
    updated.append((b"cookie", header.encode("latin-1")))
    request.scope["headers"] = updated
    for attr in ("_headers", "_cookies"):
        if hasattr(request, attr):
            delattr(request, attr)


def safe_next_path(value: str | None) -> str | None:
    """Local path to return to after login. Rejects off-site redirects."""
    if not value:
        return None
    raw = value.strip()
    if not raw.startswith("/") or raw.startswith("//") or raw.startswith("/\\"):
        return None
    if any(char in raw for char in ("\\", "\r", "\n")):
        return None
    parts = urlsplit(raw)
    if parts.scheme or parts.netloc:
        return None
    path = parts.path or "/"
    if path in {"/", "/login"} or path.startswith("/login/"):
        return None
    query = f"?{parts.query}" if parts.query else ""
    return path + query


def require_role(role: str):
    def dependency(request: Request):
        repo: HubRepository = request.app.state.repo
        cfg: HubConfig = request.app.state.cfg
        user = current_user(request, repo, cfg)
        if user["role"] != role:
            raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "Insufficient permissions"})
        return user

    return dependency


def csrf_protect(request: Request) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get("x-csrf-token") or request.headers.get("x-csrftoken")
    if not cookie or not header or not secrets.compare_digest(cookie, header):
        raise HTTPException(status_code=403, detail={"code": "csrf_failed", "message": "CSRF validation failed"})


def set_auth_cookies(
    response,
    access: str,
    refresh: str,
    csrf: str,
    *,
    secure: bool = False,
    access_ttl: int = 900,
    refresh_ttl: int = 604800,
) -> None:
    response.set_cookie(ACCESS_COOKIE, access, httponly=True, samesite="lax", secure=secure, max_age=access_ttl, path="/")
    response.set_cookie(REFRESH_COOKIE, refresh, httponly=True, samesite="lax", secure=secure, max_age=refresh_ttl, path="/")
    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, samesite="lax", secure=secure, max_age=refresh_ttl, path="/")


def clear_auth_cookies(response, *, secure: bool = False) -> None:
    response.delete_cookie(ACCESS_COOKIE, path="/", secure=secure, httponly=True, samesite="lax")
    response.delete_cookie(REFRESH_COOKIE, path="/", secure=secure, httponly=True, samesite="lax")
    response.delete_cookie(CSRF_COOKIE, path="/", secure=secure, httponly=False, samesite="lax")


_SKIP_RENEW = {"/healthz", "/api/v1/auth/login", "/api/v1/auth/refresh", "/api/v1/auth/logout"}


def _should_renew(path: str, method: str) -> bool:
    if method == "OPTIONS":
        return False
    if path.startswith("/static") or path.startswith("/api/v1/internal/"):
        return False
    if path in _SKIP_RENEW:
        return False
    if path == "/login" and method != "GET":
        return False
    return True


def _access_set_cookie(value: str, *, secure: bool, max_age: int) -> bytes:
    response = Response()
    response.set_cookie(ACCESS_COOKIE, value, httponly=True, samesite="lax", secure=secure, max_age=max_age, path="/")
    for key, raw in response.raw_headers:
        if key.lower() == b"set-cookie":
            return raw
    return b""


class AccessCookieMiddleware:
    """Renew bb_access from bb_refresh before the route runs.

    HTML pages and API handlers only accept the access cookie. When it expires,
    a still-valid refresh cookie is exchanged for a new access cookie on the
    same response, so the operator is not sent back to login.
    """

    def __init__(self, app: Any, repo: HubRepository, cfg: HubConfig) -> None:
        self.app = app
        self.repo = repo
        self.cfg = cfg

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not _should_renew(str(scope.get("path") or ""), str(scope.get("method") or "GET")):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if access_token_ok(request, self.cfg):
            await self.app(scope, receive, send)
            return
        renewed = mint_access_from_refresh(request, self.repo, self.cfg)
        if not renewed:
            await self.app(scope, receive, send)
            return
        install_request_cookie(request, ACCESS_COOKIE, renewed)
        cookie_line = _access_set_cookie(renewed, secure=self.cfg.cookie_secure, max_age=self.cfg.access_ttl_seconds)

        async def send_with_cookie(message: dict) -> None:
            if message["type"] == "http.response.start" and cookie_line:
                headers = list(message.get("headers") or [])
                if not any(key.lower() == b"set-cookie" and value.startswith(b"bb_access=") for key, value in headers):
                    headers.append((b"set-cookie", cookie_line))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_cookie)
