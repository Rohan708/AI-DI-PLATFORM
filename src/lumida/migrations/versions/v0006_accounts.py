"""Accounts: people, login tokens, audit log (Stage 4.3).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)


def _tenant(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"], ["tenant.id"], name=op.f(f"fk_{table}_tenant_id_tenant")
    )


def upgrade() -> None:
    op.create_table(
        "app_user",
        sa.Column("id", UUID, nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("created_by", sa.String(320), nullable=False),
        sa.Column("disabled_at", TS, nullable=True),
        sa.Column("last_login_at", TS, nullable=True),
        sa.Column("failed_logins", sa.Integer(), nullable=False),
        sa.Column("locked_until", TS, nullable=True),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_app_user")),
        sa.UniqueConstraint("email", name=op.f("uq_app_user_email")),
        _tenant("app_user"),
    )
    op.create_table(
        "auth_token",
        sa.Column("id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("label", sa.String(200), nullable=True),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=True),
        sa.Column("last_used_at", TS, nullable=True),
        sa.Column("revoked_at", TS, nullable=True),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_token")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_auth_token_token_hash")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name=op.f("fk_auth_token_user_id_app_user")
        ),
        _tenant("auth_token"),
    )
    op.create_index("ix_auth_token_user", "auth_token", ["user_id"])
    op.create_table(
        "audit_event",
        sa.Column("id", UUID, nullable=False),
        sa.Column("at", TS, nullable=False),
        sa.Column("actor", sa.String(320), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(64), nullable=True),
        sa.Column("target_id", sa.String(100), nullable=True),
        sa.Column("details", postgresql.JSONB, nullable=False),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_event")),
        _tenant("audit_event"),
    )
    op.create_index("ix_audit_event_tenant_at", "audit_event", ["tenant_id", "at"])


def downgrade() -> None:
    op.drop_table("audit_event")
    op.drop_index("ix_auth_token_user", table_name="auth_token")
    op.drop_table("auth_token")
    op.drop_table("app_user")
