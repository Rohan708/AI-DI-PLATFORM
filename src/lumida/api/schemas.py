"""What the API sends and accepts. Names and numbers only: no connection URLs, no secrets.

Evidence is passed through as stored: it holds measurements and offending-row IDs, the
same as ``lumida finding show`` prints (raw values only where the customer opted in).
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class RunOut(BaseModel):
    id: uuid.UUID
    status: str
    started_at: datetime
    finished_at: datetime | None
    error_message: str | None
    stats: dict[str, Any]


class SourceOut(BaseModel):
    name: str
    kind: str
    connection_ref: str | None  # the *name* of the setting holding the URL, never the URL
    run_at: str | None  # local "HH:MM"; None = not scheduled
    timezone: str
    created_at: datetime
    disabled: bool
    baseline_done: bool  # False until the first ("quiet baseline") run finished
    health_score: float | None
    open_findings: dict[str, int]  # severity -> count, open + confirmed
    last_run: RunOut | None


class ScoreOut(BaseModel):
    score: float
    worst_table: float | None = None
    open_findings: dict[str, int]


class TableScoreOut(ScoreOut):
    table: str


class HealthOut(BaseModel):
    computed_at: datetime
    source: ScoreOut
    previous_score: float | None
    schemas: dict[str, ScoreOut]
    tables: list[TableScoreOut]  # worst first


class FindingOut(BaseModel):
    id: uuid.UUID
    source: str
    category: str
    check_name: str
    severity: str
    status: str
    origin: str
    title: str
    table: str | None
    column: str | None
    confidence: float | None
    first_detected_at: datetime
    last_detected_at: datetime


class FindingPage(BaseModel):
    total: int
    items: list[FindingOut]


class EventOut(BaseModel):
    at: datetime
    by: str
    from_status: str | None
    to_status: str
    note: str | None


class FindingDetail(FindingOut):
    description: str
    evidence: dict[str, Any]
    explanation: dict[str, Any] | None  # the AI's plain-language explanation, if any
    resolved_at: datetime | None
    reviewed_by: str | None
    reviewed_at: datetime | None
    review_note: str | None
    history: list[EventOut]


class RuleOut(BaseModel):
    id: uuid.UUID
    source: str
    name: str
    kind: str
    status: str
    origin: str
    confidence: float | None
    spec: dict[str, Any]
    rationale: str | None
    created_at: datetime
    created_by: str | None
    reviewed_by: str | None
    reviewed_at: datetime | None
    history: list[dict[str, Any]]


class RelationshipOut(BaseModel):
    id: uuid.UUID
    from_table: str
    from_columns: list[str]
    to_table: str
    to_columns: list[str]
    kind: str  # declared (a real foreign key) | inferred
    status: str
    confidence: float | None
    signals: list[str]
    ai_review: dict[str, Any] | None
    reviewed_by: str | None


class QuestionOut(BaseModel):
    key: str
    kind: str
    prompt: str
    hint: str
    suggested: str


# --- requests ------------------------------------------------------------------------------
# Who is acting comes from the login, never from the request.

Note = Field(default=None, max_length=2000, description="why (kept in the history)")


class FindingReview(BaseModel):
    action: Literal["confirm", "reject", "resolve", "reopen"]
    note: str | None = Note


class RuleReview(BaseModel):
    action: Literal["approve", "reject", "disable", "enable", "reconsider"]
    note: str | None = Note


class NewRule(BaseModel):
    spec: dict[str, Any] = Field(description='e.g. {"kind": "not_null", "table": ..., ...}')


class RelationshipReview(BaseModel):
    action: Literal["confirm", "reject"]


class Answer(BaseModel):
    key: str = Field(min_length=1, description="the question's key, from GET .../questions")
    value: str = Field(max_length=5000, description='the answer; "" or "?" = not known')


class AnswerOut(BaseModel):
    outcome: str


# --- accounts (Stage 4.3) ------------------------------------------------------------------

Password = Field(min_length=1, max_length=1024)
RoleName = Literal["viewer", "reviewer", "admin"]


class Login(BaseModel):
    email: str = Field(min_length=1, max_length=320)
    password: str = Password


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    role: str
    created_at: datetime
    created_by: str
    last_login_at: datetime | None
    disabled: bool
    locked: bool


class LoginOut(BaseModel):
    token: str = Field(description='send as "Authorization: Bearer <token>"')
    expires_at: datetime
    user: UserOut


class PasswordChange(BaseModel):
    current_password: str = Password
    new_password: str = Password


class NewUser(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    name: str = Field(default="", max_length=200)
    role: RoleName = "viewer"
    password: str = Password


class UserChange(BaseModel):
    role: RoleName | None = None
    disabled: bool | None = None


class PasswordReset(BaseModel):
    password: str = Password


class NewApiKey(BaseModel):
    label: str = Field(min_length=1, max_length=200, description='e.g. "jira sync"')


class ApiKeyOut(BaseModel):
    id: uuid.UUID
    label: str | None
    created_at: datetime
    last_used_at: datetime | None


class NewApiKeyOut(ApiKeyOut):
    key: str = Field(description="shown once; store it now")


class AuditOut(BaseModel):
    at: datetime
    actor: str
    action: str
    target_type: str | None
    target_id: str | None
    details: dict[str, Any]
