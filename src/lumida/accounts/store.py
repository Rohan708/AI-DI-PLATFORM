"""People, logins and the audit log: the rules, shared by the API and ``lumida user``.

- Emails are compared lower-case and are unique across the install.
- Logging in returns a random token; only its SHA-256 hash is stored. Sessions expire;
  API keys last until revoked. Disabling a person or resetting their password revokes
  every token they have.
- After ``max_failures`` wrong passwords in a row an account is locked for a while.
  The answer to a failed login never says whether the email exists.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lumida.accounts.passwords import dummy_hash, hash_password, verify_password
from lumida.graph.models import AuditEvent, AuthToken, Role, TokenKind, User, utcnow

TOKEN_PREFIX = "lmd_"  # noqa: S105 - not a secret: recognisable in logs and secret scanners
# Writing "last used" on every request would turn every read into a write.
LAST_USED_RESOLUTION = timedelta(minutes=5)
ROLE_RANK = {Role.VIEWER: 0, Role.REVIEWER: 1, Role.ADMIN: 2}


@dataclass(frozen=True)
class AuthPolicy:
    session_hours: float = 12.0
    max_failures: int = 5
    lockout_minutes: float = 15.0


class AccountError(ValueError):
    """Something a person asked for that can't be done (shown to them as is)."""


class AccountNotFoundError(LookupError):
    pass


class LoginFailedError(Exception):
    """Wrong email or password, a disabled account or a locked one: one message for all."""


@dataclass(frozen=True)
class Principal:
    """Who is making a request."""

    user_id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
    role: Role
    token_id: uuid.UUID

    def can(self, needed: Role) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[needed]


def normalize_email(email: str) -> str:
    cleaned = email.strip().lower()
    if "@" not in cleaned or len(cleaned) > 320 or " " in cleaned:
        raise AccountError(f"{email!r} is not an email address")
    return cleaned


# --- people --------------------------------------------------------------------------------


def find_user(session: Session, email: str, tenant_id: uuid.UUID | None = None) -> User:
    query = select(User).where(User.email == normalize_email(email))
    if tenant_id is not None:
        query = query.where(User.tenant_id == tenant_id)
    user = session.scalar(query)
    if user is None:
        raise AccountError(f"no person with email {email.strip()!r}")
    return user


def list_users(session: Session, tenant_id: uuid.UUID) -> list[User]:
    return list(
        session.scalars(select(User).where(User.tenant_id == tenant_id).order_by(User.email))
    )


def create_user(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    email: str,
    name: str,
    role: Role,
    password: str,
    by: str,
) -> User:
    address = normalize_email(email)
    if session.scalar(select(User.id).where(User.email == address)) is not None:
        raise AccountError(f"{address} already has an account")
    user = User(
        tenant_id=tenant_id, email=address, name=name.strip() or address, role=role,
        password_hash=hash_password(password), created_by=by,
    )  # fmt: skip
    session.add(user)
    session.flush()
    audit(session, tenant_id, by, "user.create", user, {"email": address, "role": role.value})
    return user


def set_role(session: Session, user: User, role: Role, *, by: str) -> None:
    if user.role is Role.ADMIN and role is not Role.ADMIN:
        _keep_an_admin(session, user)
    before = user.role
    user.role = role
    audit(session, user.tenant_id, by, "user.role", user, {"from": before.value, "to": role.value})
    session.flush()


def set_disabled(session: Session, user: User, disabled: bool, *, by: str) -> None:
    if disabled and user.role is Role.ADMIN:
        _keep_an_admin(session, user)
    now = utcnow()
    user.disabled_at = now if disabled else None
    if disabled:
        revoke_all(session, user, now)
    else:
        user.failed_logins, user.locked_until = 0, None
    audit(session, user.tenant_id, by, "user.disable" if disabled else "user.enable", user)
    session.flush()


def set_password(session: Session, user: User, password: str, *, by: str) -> None:
    """A new password (by the person or an admin): unlocks, and ends every session."""
    now = utcnow()
    user.password_hash = hash_password(password)
    user.failed_logins, user.locked_until = 0, None
    revoke_all(session, user, now, keep=TokenKind.API_KEY if by == user.email else None)
    audit(session, user.tenant_id, by, "user.password", user)
    session.flush()


def change_own_password(session: Session, principal: Principal, current: str, new: str) -> None:
    user = session.get_one(User, principal.user_id)
    if not verify_password(current, user.password_hash):
        raise AccountError("the current password is wrong")
    set_password(session, user, new, by=user.email)


def _keep_an_admin(session: Session, user: User) -> None:
    admins = session.scalars(
        select(User.id).where(
            User.tenant_id == user.tenant_id,
            User.role == Role.ADMIN,
            User.disabled_at.is_(None),
            User.id != user.id,
        )
    ).all()
    if not admins:
        raise AccountError("this is the last admin; make someone else admin first")


# --- logging in ----------------------------------------------------------------------------


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _new_token(
    session: Session,
    user: User,
    kind: TokenKind,
    expires_at: datetime | None,
    label: str | None = None,
) -> tuple[str, AuthToken]:
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = AuthToken(
        tenant_id=user.tenant_id, user_id=user.id, kind=kind, label=label,
        token_hash=_hash(raw), expires_at=expires_at,
    )  # fmt: skip
    session.add(row)
    session.flush()
    return raw, row


def login(
    session: Session, email: str, password: str, policy: AuthPolicy
) -> tuple[str, AuthToken, User]:
    """Check a password and start a session. Every failure is recorded and raises the same
    ``LoginFailedError``; the caller must commit either way (failures count)."""
    now = utcnow()
    try:
        address = normalize_email(email)
    except AccountError:
        address = email.strip().lower()[:320]
    user = session.scalar(select(User).where(User.email == address))
    if user is None:
        verify_password(password, dummy_hash())  # same time as a wrong password
        raise LoginFailedError
    if user.locked_until is not None and user.locked_until > now:
        audit(session, user.tenant_id, address, "auth.login_failed", user, {"reason": "locked"})
        raise LoginFailedError
    if not verify_password(password, user.password_hash) or user.disabled_at is not None:
        reason = "disabled" if user.disabled_at is not None else "wrong password"
        user.failed_logins += 1
        if user.failed_logins >= policy.max_failures:
            user.locked_until = now + timedelta(minutes=policy.lockout_minutes)
            reason += f"; locked for {policy.lockout_minutes:g} minutes"
        audit(session, user.tenant_id, address, "auth.login_failed", user, {"reason": reason})
        raise LoginFailedError
    user.failed_logins, user.locked_until, user.last_login_at = 0, None, now
    expires = now + timedelta(hours=policy.session_hours)
    raw, row = _new_token(session, user, TokenKind.SESSION, expires)
    audit(session, user.tenant_id, address, "auth.login", user)
    return raw, row, user


def resolve(session: Session, raw: str) -> Principal | None:
    """The person behind a token, or None if it's unknown, expired, revoked or disabled."""
    if not raw.startswith(TOKEN_PREFIX):
        return None
    now = utcnow()
    row = session.scalar(select(AuthToken).where(AuthToken.token_hash == _hash(raw)))
    if row is None or row.revoked_at is not None:
        return None
    if row.expires_at is not None and row.expires_at <= now:
        return None
    user = session.get_one(User, row.user_id)
    if user.disabled_at is not None:
        return None
    if row.last_used_at is None or now - row.last_used_at > LAST_USED_RESOLUTION:
        row.last_used_at = now
        session.commit()
    return Principal(user.id, user.tenant_id, user.email, user.role, row.id)


def logout(session: Session, principal: Principal) -> None:
    row = session.get_one(AuthToken, principal.token_id)
    row.revoked_at = utcnow()
    audit(session, principal.tenant_id, principal.email, "auth.logout", None)
    session.flush()


def revoke_all(session: Session, user: User, now: datetime, keep: TokenKind | None = None) -> None:
    statement = update(AuthToken).where(
        AuthToken.user_id == user.id, AuthToken.revoked_at.is_(None)
    )
    if keep is not None:
        statement = statement.where(AuthToken.kind != keep)
    session.execute(
        statement.values(revoked_at=now), execution_options={"synchronize_session": False}
    )


# --- API keys ------------------------------------------------------------------------------


def create_api_key(session: Session, principal: Principal, label: str) -> tuple[str, AuthToken]:
    user = session.get_one(User, principal.user_id)
    raw, row = _new_token(session, user, TokenKind.API_KEY, None, label.strip() or "api key")
    details = {"key": str(row.id), "label": row.label}
    audit(session, user.tenant_id, user.email, "key.create", None, details)
    return raw, row


def api_keys(session: Session, principal: Principal) -> list[AuthToken]:
    query = (
        select(AuthToken)
        .where(
            AuthToken.user_id == principal.user_id,
            AuthToken.kind == TokenKind.API_KEY,
            AuthToken.revoked_at.is_(None),
        )
        .order_by(AuthToken.created_at)
    )
    return list(session.scalars(query))


def revoke_api_key(session: Session, principal: Principal, key_id: uuid.UUID) -> None:
    row = session.get(AuthToken, key_id)
    if row is None or row.user_id != principal.user_id or row.kind is not TokenKind.API_KEY:
        raise AccountNotFoundError(f"no API key with id {key_id}")
    row.revoked_at = row.revoked_at or utcnow()
    audit(session, principal.tenant_id, principal.email, "key.revoke", None, {"key": str(key_id)})
    session.flush()


# --- audit log -----------------------------------------------------------------------------


def audit(
    session: Session,
    tenant_id: uuid.UUID,
    actor: str,
    action: str,
    target: Any = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    """Record one action. ``target`` is any row with an ``id`` (or None)."""
    event = AuditEvent(
        tenant_id=tenant_id, at=utcnow(), actor=actor, action=action,
        target_type=getattr(target, "__tablename__", None) if target is not None else None,
        target_id=str(target.id) if target is not None else None, details=details or {},
    )  # fmt: skip
    session.add(event)
    return event


def audit_events(
    session: Session,
    tenant_id: uuid.UUID,
    *,
    action: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[AuditEvent]:
    query = select(AuditEvent).where(AuditEvent.tenant_id == tenant_id)
    if action:
        query = query.where(AuditEvent.action.startswith(action))
    query = query.order_by(AuditEvent.at.desc(), AuditEvent.id).limit(limit).offset(offset)
    return list(session.scalars(query))
