from __future__ import annotations

import hashlib
import secrets
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from fastapi import HTTPException, Request, status

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
        payload = jwt.decode(token, cfg.jwt_secret, algorithms=["HS256"])
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


def current_user(request: Request, repo: HubRepository, cfg: HubConfig) -> dict[str, Any]:
    token = request.cookies.get(ACCESS_COOKIE)
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail={"code": "auth_required", "message": "Authentication required"})
    payload = _decode(token, cfg, "access")
    user = repo.user_by_id(int(payload["sub"]))
    if user is None:
        raise HTTPException(status_code=401, detail={"code": "user_disabled", "message": "User is disabled"})
    return user


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


def set_auth_cookies(response, access: str, refresh: str, csrf: str, *, secure: bool = False) -> None:
    response.set_cookie(ACCESS_COOKIE, access, httponly=True, samesite="lax", secure=secure, max_age=900)
    response.set_cookie(REFRESH_COOKIE, refresh, httponly=True, samesite="lax", secure=secure, max_age=604800)
    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, samesite="lax", secure=secure, max_age=604800)
