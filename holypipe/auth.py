"""Authentication and dynamic role-based permissions.

Sessions are opaque server-side tokens in a `UserSession` row (not
self-contained JWTs) so a logout or a deactivated user actually revokes
access immediately instead of waiting out a token's expiry. Passwords are
bcrypt-hashed, never stored or logged in plain text.

The set of permission *codes* the app understands (`PERMISSIONS` below) is
fixed — enforcement has to check something specific. What's dynamic is
everything built on top of that fixed catalog: which codes make up a role,
which roles exist, and which roles a user holds are all created/edited
through the API by anyone holding `roles.manage`/`users.manage`.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import secrets
import threading
from dataclasses import dataclass, field

import bcrypt
from fastapi import Cookie, Depends, Header, HTTPException
from sqlalchemy import select

from .db import session_scope
from .logging_util import log
from .models import ApiToken, Role, User, UserSession
from .timeutil import utcnow

SESSION_COOKIE = "hp_session"
SESSION_TTL_DAYS = 7
#: Prefix on every issued personal access token, so one glance at a string
#: tells you it's a HolyPipe API token (same idea as GitHub's `ghp_`).
API_TOKEN_PREFIX = "hp_pat_"
# Only send the cookie over HTTPS. Off by default so the documented plain-
# HTTP localhost/Docker setup keeps working; set to 1 once HolyPipe sits
# behind real TLS (a reverse proxy terminating HTTPS in front of it).
COOKIE_SECURE = os.getenv("HOLYPIPE_COOKIE_SECURE", "0").strip().lower() in {"1", "true", "yes", "on"}

#: code -> human description. "*" (used by the built-in Administrator role)
#: matches every one of these, including any added later.
PERMISSIONS: dict[str, str] = {
    "sources.view": "View sources",
    "sources.manage": "Create, edit, delete, and test sources",
    "destinations.view": "View destinations",
    "destinations.manage": "Create, edit, delete, and test destinations",
    "connections.view": "View connections and their run history",
    "connections.manage": "Create, edit, and delete connections",
    "connections.operate": "Run, resync, start/stop, and activate/deactivate connections",
    "logs.view": "View live and historical logs",
    "users.manage": "Create, edit, deactivate, and delete users",
    "roles.manage": "Create, edit, and delete roles",
}


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


def create_session(user_id: str) -> tuple[str, dt.datetime]:
    token = secrets.token_urlsafe(32)
    expires = utcnow() + dt.timedelta(days=SESSION_TTL_DAYS)
    with session_scope() as s:
        s.add(UserSession(token=token, user_id=user_id, expires_at=expires))
    return token, expires


def destroy_session(token: str) -> None:
    with session_scope() as s:
        row = s.get(UserSession, token)
        if row:
            s.delete(row)


def destroy_all_sessions_for(user_id: str) -> None:
    with session_scope() as s:
        for row in s.query(UserSession).filter(UserSession.user_id == user_id).all():
            s.delete(row)


@dataclass(frozen=True)
class AuthUser:
    id: str
    username: str
    must_change_password: bool
    permissions: frozenset = field(default_factory=frozenset)

    def has(self, code: str) -> bool:
        return "*" in self.permissions or code in self.permissions


def _auth_user_from(user: User) -> AuthUser:
    perms: set[str] = set()
    for role in user.roles:
        perms.update(role.permissions or [])
    return AuthUser(id=user.id, username=user.username,
                    must_change_password=user.must_change_password,
                    permissions=frozenset(perms))


def _load_auth_user(token: str) -> AuthUser | None:
    with session_scope() as s:
        row = s.get(UserSession, token)
        if not row or row.expires_at < utcnow():
            if row:
                s.delete(row)
            return None
        user = s.get(User, row.user_id)
        if not user or not user.is_active:
            return None
        return _auth_user_from(user)


def hash_api_token(plain: str) -> str:
    """Plain sha256, not bcrypt — the token itself is 256 bits of random
    entropy (unlike a user-chosen password), so a fast lookup hash is both
    sufficient against brute force and lets the DB index on it directly."""
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def create_api_token(user_id: str, name: str, expires_at: dt.datetime | None = None) -> tuple[str, ApiToken]:
    """Returns the plaintext token (shown to the caller exactly once) and the
    stored row (hash only, safe to keep around/return afterward)."""
    plain = API_TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = ApiToken(user_id=user_id, name=name, token_hash=hash_api_token(plain),
                   token_prefix=plain[: len(API_TOKEN_PREFIX) + 6], expires_at=expires_at)
    with session_scope() as s:
        s.add(row)
        s.flush()
        s.refresh(row)
        s.expunge(row)
    return plain, row


def revoke_api_token(token_id: str) -> bool:
    with session_scope() as s:
        row = s.get(ApiToken, token_id)
        if not row:
            return False
        s.delete(row)
        return True


def _load_auth_user_from_api_token(token: str) -> AuthUser | None:
    with session_scope() as s:
        row = s.execute(
            select(ApiToken).where(ApiToken.token_hash == hash_api_token(token))
        ).scalar_one_or_none()
        if not row or (row.expires_at and row.expires_at < utcnow()):
            return None
        user = s.get(User, row.user_id)
        if not user or not user.is_active:
            return None
        row.last_used_at = utcnow()
        return _auth_user_from(user)


def get_current_user(hp_session: str | None = Cookie(default=None),
                     authorization: str | None = Header(default=None)) -> AuthUser:
    if authorization and authorization.lower().startswith("bearer "):
        user = _load_auth_user_from_api_token(authorization[7:].strip())
        if not user:
            raise HTTPException(401, "Invalid, expired, or revoked API token")
        return user
    if not hp_session:
        raise HTTPException(401, "Not authenticated")
    user = _load_auth_user(hp_session)
    if not user:
        raise HTTPException(401, "Session expired or invalid")
    return user


def require_permission(code: str):
    """A FastAPI dependency: 403s unless the caller holds `code` (or `*`)."""
    def _dep(user: AuthUser = Depends(get_current_user)) -> AuthUser:
        if not user.has(code):
            raise HTTPException(403, f"Missing permission: {code}")
        return user
    return _dep


async def get_ws_user(hp_session: str | None = Cookie(default=None)) -> AuthUser | None:
    """Non-raising variant for the websocket endpoint, which needs to close
    the socket itself rather than let FastAPI turn a 401 into an HTTP error
    after the handshake has already been accepted."""
    if not hp_session:
        return None
    return _load_auth_user(hp_session)


class LoginThrottle:
    """Locks out repeated failed logins for one username, independent of
    which IP they come from — a small, dependency-free brute-force guard."""

    MAX_ATTEMPTS = 5
    WINDOW = dt.timedelta(minutes=15)

    def __init__(self) -> None:
        self._failures: dict[str, list[dt.datetime]] = {}
        self._lock = threading.Lock()

    def check(self, username: str) -> None:
        now = utcnow()
        with self._lock:
            attempts = [t for t in self._failures.get(username, []) if now - t < self.WINDOW]
            self._failures[username] = attempts
            if len(attempts) >= self.MAX_ATTEMPTS:
                raise HTTPException(429, "Too many failed login attempts — try again in a few minutes")

    def record_failure(self, username: str) -> None:
        with self._lock:
            self._failures.setdefault(username, []).append(utcnow())

    def clear(self, username: str) -> None:
        with self._lock:
            self._failures.pop(username, None)


login_throttle = LoginThrottle()


def seed_default_admin() -> None:
    """Creates the Administrator role and an admin/admin user the first time
    HolyPipe starts with no users at all. Forces a password change on first
    login — the default credentials are meant to get you in the door, not
    to be left in place."""
    with session_scope() as s:
        if s.query(User).count() > 0:
            return
        admin_role = Role(name="Administrator", description="Full access to everything, including user/role management.",
                          permissions=["*"], is_builtin=True)
        s.add(admin_role)
        s.flush()
        admin_user = User(username="admin", password_hash=hash_password("admin"), must_change_password=True)
        admin_user.roles.append(admin_role)
        s.add(admin_user)
    log("Seeded default admin user — username 'admin', password 'admin'. "
       "Log in and change this immediately; it is forced on first login.", level="WARNING")
