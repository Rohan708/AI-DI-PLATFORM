"""Logging in, personal API keys, people (admins) and the audit log, under ``/api/v1``."""

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from lumida.accounts.store import (
    AccountError,
    AccountNotFoundError,
    LoginFailedError,
    api_keys,
    audit_events,
    change_own_password,
    create_api_key,
    create_user,
    list_users,
    login,
    logout,
    revoke_api_key,
    set_disabled,
    set_password,
    set_role,
)
from lumida.api.auth import DB, Admin, Viewer
from lumida.api.schemas import (
    ApiKeyOut,
    AuditOut,
    Login,
    LoginOut,
    NewApiKey,
    NewApiKeyOut,
    NewUser,
    PasswordChange,
    PasswordReset,
    UserChange,
    UserOut,
)
from lumida.graph.models import AuditEvent, AuthToken, Role, User, utcnow

router = APIRouter(prefix="/api/v1")
LOGIN_FAILED = "wrong email or password, or the account is locked or disabled"


def user_out(u: User) -> UserOut:
    locked = u.locked_until is not None and u.locked_until > utcnow()
    return UserOut(
        id=u.id, email=u.email, name=u.name, role=u.role.value, created_at=u.created_at,
        created_by=u.created_by, last_login_at=u.last_login_at,
        disabled=u.disabled_at is not None, locked=locked,
    )  # fmt: skip


def _key_out(k: AuthToken) -> ApiKeyOut:
    return ApiKeyOut(id=k.id, label=k.label, created_at=k.created_at, last_used_at=k.last_used_at)


# --- logging in ----------------------------------------------------------------------------


@router.post("/auth/login")
def log_in(body: Login, session: DB, request: Request) -> LoginOut:
    """The one endpoint that needs no token."""
    try:
        token, row, user = login(session, body.email, body.password, request.app.state.policy)
    except LoginFailedError:
        session.commit()  # failed attempts count towards the lockout and are audited
        raise HTTPException(401, LOGIN_FAILED) from None
    session.commit()
    expires = row.expires_at or utcnow()  # sessions always have one
    return LoginOut(token=token, expires_at=expires, user=user_out(user))


@router.post("/auth/logout", status_code=204)
def log_out(session: DB, me: Viewer) -> None:
    logout(session, me)
    session.commit()


@router.get("/auth/me")
def whoami(session: DB, me: Viewer) -> UserOut:
    return user_out(session.get_one(User, me.user_id))


@router.post("/auth/password", status_code=204)
def change_password(body: PasswordChange, session: DB, me: Viewer) -> None:
    """Change your own password. All your sessions end (log in again); API keys keep working."""
    change_own_password(session, me, body.current_password, body.new_password)
    session.commit()


@router.get("/auth/keys")
def keys(session: DB, me: Viewer) -> list[ApiKeyOut]:
    return [_key_out(k) for k in api_keys(session, me)]


@router.post("/auth/keys", status_code=201)
def new_key(body: NewApiKey, session: DB, me: Viewer) -> NewApiKeyOut:
    """A personal key for scripts: it acts as you, with your role, until revoked."""
    raw, row = create_api_key(session, me, body.label)
    session.commit()
    return NewApiKeyOut(key=raw, **_key_out(row).model_dump())


@router.delete("/auth/keys/{key_id}", status_code=204)
def delete_key(key_id: uuid.UUID, session: DB, me: Viewer) -> None:
    revoke_api_key(session, me, key_id)
    session.commit()


# --- people (admins) -----------------------------------------------------------------------


def _user(session: DB, me: Admin, user_id: uuid.UUID) -> User:
    user = session.get(User, user_id)
    if user is None or user.tenant_id != me.tenant_id:
        raise AccountNotFoundError(f"no person with id {user_id}")
    return user


@router.get("/users")
def users(session: DB, me: Admin) -> list[UserOut]:
    return [user_out(u) for u in list_users(session, me.tenant_id)]


@router.post("/users", status_code=201)
def add_user(body: NewUser, session: DB, me: Admin) -> UserOut:
    user = create_user(
        session, tenant_id=me.tenant_id, email=body.email, name=body.name,
        role=Role(body.role), password=body.password, by=me.email,
    )  # fmt: skip
    session.commit()
    return user_out(user)


@router.patch("/users/{user_id}")
def change_user(user_id: uuid.UUID, body: UserChange, session: DB, me: Admin) -> UserOut:
    user = _user(session, me, user_id)
    if user.id == me.user_id and (body.disabled or (body.role and body.role != "admin")):
        raise AccountError("you can't disable yourself or take away your own admin role")
    if body.role is not None and Role(body.role) is not user.role:
        set_role(session, user, Role(body.role), by=me.email)
    if body.disabled is not None and body.disabled != (user.disabled_at is not None):
        set_disabled(session, user, body.disabled, by=me.email)
    session.commit()
    return user_out(user)


@router.post("/users/{user_id}/password", status_code=204)
def reset_password(user_id: uuid.UUID, body: PasswordReset, session: DB, me: Admin) -> None:
    """A new password for someone: unlocks them and ends all their sessions and keys."""
    set_password(session, _user(session, me, user_id), body.password, by=me.email)
    session.commit()


# --- audit log -----------------------------------------------------------------------------


@router.get("/audit")
def audit_log(
    session: DB,
    me: Admin,
    action: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AuditOut]:
    """Newest first. ``action`` filters by prefix, e.g. ``auth`` or ``finding.review``."""
    events: list[AuditEvent] = audit_events(
        session, me.tenant_id, action=action, limit=limit, offset=offset
    )
    return [
        AuditOut(
            at=e.at,
            actor=e.actor,
            action=e.action,
            target_type=e.target_type,
            target_id=e.target_id,
            details=e.details,
        )
        for e in events
    ]
