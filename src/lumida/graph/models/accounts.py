"""People, their login tokens, and the audit log (Stage 4.3).

- ``User``: one person, in one tenant, with one role. Passwords are stored only as
  scrypt hashes (``accounts/passwords.py``).
- ``AuthToken``: a login session or a personal API key. Only a SHA-256 hash of the token
  is stored, so a copy of the database can't be used to log in.
- ``AuditEvent``: who did what, when: logins (and failed ones), reviews, rule and user
  changes. Never passwords, tokens or row values.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from lumida.graph.models.base import Base, StrEnumType, TenantScoped, utcnow
from lumida.graph.models.enums import Role, TokenKind


class User(TenantScoped, Base):
    __tablename__ = "app_user"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True)  # stored lower-case
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[Role] = mapped_column(StrEnumType(Role))
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    created_by: Mapped[str] = mapped_column(String(320))
    disabled_at: Mapped[datetime | None]
    last_login_at: Mapped[datetime | None]
    failed_logins: Mapped[int] = mapped_column(default=0)
    locked_until: Mapped[datetime | None]


class AuthToken(TenantScoped, Base):
    __tablename__ = "auth_token"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id"))
    kind: Mapped[TokenKind] = mapped_column(StrEnumType(TokenKind))
    label: Mapped[str | None] = mapped_column(String(200))  # e.g. "jira sync" for a key
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime | None]  # None = until revoked (API keys)
    last_used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]

    __table_args__ = (Index("ix_auth_token_user", "user_id"),)


class AuditEvent(TenantScoped, Base):
    __tablename__ = "audit_event"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    at: Mapped[datetime] = mapped_column(default=utcnow)
    actor: Mapped[str] = mapped_column(String(320))  # an email, or "cli:<os user>"
    action: Mapped[str] = mapped_column(String(64))  # e.g. "finding.review", "auth.login"
    target_type: Mapped[str | None] = mapped_column(String(64))
    target_id: Mapped[str | None] = mapped_column(String(100))
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)

    __table_args__ = (Index("ix_audit_event_tenant_at", "tenant_id", "at"),)
