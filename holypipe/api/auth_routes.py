"""Login/session endpoints, plus user and role management."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import (
    COOKIE_SECURE,
    PERMISSIONS,
    SESSION_COOKIE,
    SESSION_TTL_DAYS,
    AuthUser,
    create_api_token,
    create_session,
    destroy_all_sessions_for,
    destroy_session,
    get_current_user,
    hash_password,
    login_throttle,
    require_permission,
    revoke_api_token,
    verify_password,
)
from ..db import get_session
from ..models import ApiToken, Role, User
from ..timeutil import utcnow
from . import schemas as sch

router = APIRouter(prefix="/api")


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_TTL_DAYS * 24 * 3600,
        httponly=True, secure=COOKIE_SECURE, samesite="strict", path="/",
    )


def _user_permissions(user: User) -> list[str]:
    perms: set[str] = set()
    for role in user.roles:
        perms.update(role.permissions or [])
    return sorted(perms)


# ---------------------------------------------------------------------------
# Login / session
# ---------------------------------------------------------------------------
@router.post("/auth/login", response_model=sch.MeOut)
def login(body: sch.LoginRequest, response: Response, db: Session = Depends(get_session)):
    login_throttle.check(body.username)
    user = db.execute(select(User).where(User.username == body.username)).scalar_one_or_none()
    if not user or not user.is_active or not verify_password(body.password, user.password_hash):
        login_throttle.record_failure(body.username)
        raise HTTPException(401, "Invalid username or password")
    login_throttle.clear(body.username)
    token, _ = create_session(user.id)
    _set_session_cookie(response, token)
    return sch.MeOut(id=user.id, username=user.username, must_change_password=user.must_change_password,
                     permissions=_user_permissions(user))


@router.post("/auth/logout")
def logout(response: Response, hp_session: str | None = Cookie(default=None)):
    if hp_session:
        destroy_session(hp_session)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/auth/me", response_model=sch.MeOut)
def me(user: AuthUser = Depends(get_current_user)):
    return sch.MeOut(id=user.id, username=user.username, must_change_password=user.must_change_password,
                     permissions=sorted(user.permissions))


@router.post("/auth/change-password")
def change_password(body: sch.ChangePasswordRequest, response: Response,
                    user: AuthUser = Depends(get_current_user), db: Session = Depends(get_session)):
    row = db.get(User, user.id)
    if not row or not verify_password(body.old_password, row.password_hash):
        raise HTTPException(400, "Current password is incorrect")
    row.password_hash = hash_password(body.new_password)
    row.must_change_password = False
    db.commit()
    # Force re-login everywhere else — a changed password should invalidate
    # any other session that might have been established with the old one.
    destroy_all_sessions_for(user.id)
    token, _ = create_session(user.id)
    _set_session_cookie(response, token)
    return {"ok": True}


@router.get("/auth/permissions")
def list_permission_catalog(user: AuthUser = Depends(get_current_user)):
    return [{"code": code, "description": desc} for code, desc in PERMISSIONS.items()]


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------
@router.get("/roles", response_model=list[sch.RoleOut], dependencies=[Depends(require_permission("roles.manage"))])
def list_roles(db: Session = Depends(get_session)):
    return db.execute(select(Role).order_by(Role.created_at)).scalars().all()


@router.post("/roles", response_model=sch.RoleOut, dependencies=[Depends(require_permission("roles.manage"))])
def create_role(body: sch.RoleIn, db: Session = Depends(get_session)):
    unknown = set(body.permissions) - set(PERMISSIONS) - {"*"}
    if unknown:
        raise HTTPException(400, f"Unknown permission(s): {', '.join(sorted(unknown))}")
    if db.execute(select(Role).where(Role.name == body.name)).scalar_one_or_none():
        raise HTTPException(409, "A role with this name already exists")
    role = Role(name=body.name, description=body.description, permissions=body.permissions)
    db.add(role)
    db.commit()
    db.refresh(role)
    return role


@router.patch("/roles/{role_id}", response_model=sch.RoleOut, dependencies=[Depends(require_permission("roles.manage"))])
def update_role(role_id: str, body: sch.RoleIn, db: Session = Depends(get_session)):
    role = db.get(Role, role_id)
    if not role:
        raise HTTPException(404, "Role not found")
    if role.is_builtin:
        raise HTTPException(400, "The built-in Administrator role can't be edited")
    unknown = set(body.permissions) - set(PERMISSIONS) - {"*"}
    if unknown:
        raise HTTPException(400, f"Unknown permission(s): {', '.join(sorted(unknown))}")
    role.name, role.description, role.permissions = body.name, body.description, body.permissions
    db.commit()
    db.refresh(role)
    return role


@router.delete("/roles/{role_id}", dependencies=[Depends(require_permission("roles.manage"))])
def delete_role(role_id: str, db: Session = Depends(get_session)):
    role = db.get(Role, role_id)
    if not role:
        raise HTTPException(404, "Role not found")
    if role.is_builtin:
        raise HTTPException(400, "The built-in Administrator role can't be deleted")
    if role.users:
        raise HTTPException(409, f"{len(role.users)} user(s) still hold this role — reassign them first")
    db.delete(role)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
def _user_out(user: User) -> sch.UserOut:
    return sch.UserOut(
        id=user.id, username=user.username, is_active=user.is_active,
        must_change_password=user.must_change_password, created_at=user.created_at,
        role_ids=[r.id for r in user.roles], role_names=[r.name for r in user.roles],
    )


@router.get("/users", response_model=list[sch.UserOut], dependencies=[Depends(require_permission("users.manage"))])
def list_users(db: Session = Depends(get_session)):
    return [_user_out(u) for u in db.execute(select(User).order_by(User.created_at)).scalars().all()]


@router.post("/users", response_model=sch.UserOut, dependencies=[Depends(require_permission("users.manage"))])
def create_user(body: sch.UserIn, db: Session = Depends(get_session)):
    if db.execute(select(User).where(User.username == body.username)).scalar_one_or_none():
        raise HTTPException(409, "A user with this username already exists")
    roles = db.execute(select(Role).where(Role.id.in_(body.role_ids))).scalars().all() if body.role_ids else []
    if len(roles) != len(set(body.role_ids)):
        raise HTTPException(400, "One or more role_ids not found")
    user = User(username=body.username, password_hash=hash_password(body.password), is_active=body.is_active)
    user.roles = list(roles)
    db.add(user)
    db.commit()
    db.refresh(user)
    return _user_out(user)


@router.patch("/users/{user_id}", response_model=sch.UserOut, dependencies=[Depends(require_permission("users.manage"))])
def update_user(user_id: str, body: sch.UserPatch, current: AuthUser = Depends(get_current_user),
                db: Session = Depends(get_session)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if body.is_active is False and user.id == current.id:
        raise HTTPException(400, "You can't deactivate your own account")
    if body.role_ids is not None:
        roles = db.execute(select(Role).where(Role.id.in_(body.role_ids))).scalars().all() if body.role_ids else []
        if len(roles) != len(set(body.role_ids)):
            raise HTTPException(400, "One or more role_ids not found")
        if user.id == current.id and not any(r.is_builtin for r in roles):
            raise HTTPException(400, "You can't remove your own Administrator access")
        user.roles = list(roles)
    if body.is_active is not None:
        user.is_active = body.is_active
        if not body.is_active:
            destroy_all_sessions_for(user.id)
    if body.password:
        user.password_hash = hash_password(body.password)
        user.must_change_password = False
        destroy_all_sessions_for(user.id)
    db.commit()
    db.refresh(user)
    return _user_out(user)


@router.delete("/users/{user_id}", dependencies=[Depends(require_permission("users.manage"))])
def delete_user(user_id: str, current: AuthUser = Depends(get_current_user), db: Session = Depends(get_session)):
    if user_id == current.id:
        raise HTTPException(400, "You can't delete your own account")
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    db.delete(user)
    db.commit()
    destroy_all_sessions_for(user_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# API tokens — self-service personal access tokens for non-interactive
# callers (AI agents via MCP, scripts) that can't do the cookie login flow.
# A token carries the issuing user's own permissions; there's no separate
# scoping, so anyone with users.manage can also see (never the plaintext)
# and revoke another user's tokens the same way they can deactivate the
# account itself.
# ---------------------------------------------------------------------------
@router.get("/tokens", response_model=list[sch.ApiTokenOut])
def list_my_tokens(user: AuthUser = Depends(get_current_user), db: Session = Depends(get_session)):
    q = select(ApiToken).where(ApiToken.user_id == user.id).order_by(ApiToken.created_at.desc())
    return db.execute(q).scalars().all()


@router.post("/tokens", response_model=sch.ApiTokenCreated)
def create_my_token(body: sch.ApiTokenIn, user: AuthUser = Depends(get_current_user)):
    expires_at = (utcnow() + dt.timedelta(days=body.expires_in_days)) if body.expires_in_days else None
    plain, row = create_api_token(user.id, body.name, expires_at)
    return sch.ApiTokenCreated(
        id=row.id, name=row.name, token_prefix=row.token_prefix, created_at=row.created_at,
        last_used_at=row.last_used_at, expires_at=row.expires_at, token=plain,
    )


@router.delete("/tokens/{token_id}")
def delete_my_token(token_id: str, user: AuthUser = Depends(get_current_user), db: Session = Depends(get_session)):
    row = db.get(ApiToken, token_id)
    if not row or (row.user_id != user.id and not user.has("users.manage")):
        raise HTTPException(404, "Token not found")
    revoke_api_token(token_id)
    return {"ok": True}
