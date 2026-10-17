"""The API's endpoints, under ``/api/v1``. Each one calls the same functions as the CLI, so
the rules (allowed status changes, rule validation, how answers become checks) are the
same whichever way a person works. Writes commit explicitly at the end of the request."""

import uuid
from collections.abc import Iterator
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from lumida.api.schemas import (
    Answer,
    AnswerOut,
    FindingDetail,
    FindingPage,
    FindingReview,
    HealthOut,
    NewRule,
    QuestionOut,
    RelationshipOut,
    RelationshipReview,
    RuleOut,
    RuleReview,
    RunOut,
    SourceOut,
)
from lumida.api.views import (
    finding_detail,
    finding_out,
    health_out,
    owned,
    recent_runs,
    relationship_out,
    rule_out,
    run_out,
    source_out,
)
from lumida.context.questions import answer, generate
from lumida.context.store import ContextError
from lumida.detection.recording import change_status
from lumida.discovery.catalog import load_catalog
from lumida.discovery.store import current_relationships, review
from lumida.graph.models import (
    ACTIVE_FINDING_STATUSES,
    Finding,
    FindingCategory,
    FindingStatus,
    Relationship,
    RelationshipStatus,
    Rule,
    RuleStatus,
    Severity,
    utcnow,
)
from lumida.ingestion.sources import get_source, list_sources
from lumida.rules.spec import parse_spec
from lumida.rules.store import add_user_rule, review_rule, rules_of


def get_session(request: Request) -> Iterator[Session]:
    with Session(request.app.state.engine) as session:  # closing rolls back uncommitted work
        yield session


DB = Annotated[Session, Depends(get_session)]
router = APIRouter(prefix="/api/v1")

FINDING_ACTIONS = {
    "confirm": FindingStatus.CONFIRMED,
    "reject": FindingStatus.REJECTED,
    "resolve": FindingStatus.RESOLVED,
    "reopen": FindingStatus.OPEN,
}
RULE_ACTIONS = {
    "approve": RuleStatus.ACTIVE,
    "reject": RuleStatus.REJECTED,
    "disable": RuleStatus.DISABLED,
    "enable": RuleStatus.ACTIVE,
    "reconsider": RuleStatus.PROPOSED,
}
# Most severe first, as in `lumida findings`.
SEVERITY_RANK = {s: i for i, s in enumerate([Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM,
                                              Severity.LOW, Severity.INFO])}  # fmt: skip
FindingFilter = Literal["active", "open", "confirmed", "rejected", "resolved", "all"]
MAX_PAGE = 500


# --- sources -------------------------------------------------------------------------------


@router.get("/sources")
def sources(session: DB) -> list[SourceOut]:
    return [source_out(session, s) for s in list_sources(session)]


@router.get("/sources/{name}")
def source(name: str, session: DB) -> SourceOut:
    return source_out(session, get_source(session, name))


@router.get("/sources/{name}/runs")
def runs(name: str, session: DB, limit: Annotated[int, Query(ge=1, le=200)] = 20) -> list[RunOut]:
    return [run_out(r) for r in recent_runs(session, get_source(session, name), limit)]


@router.get("/sources/{name}/health")
def health(name: str, session: DB) -> HealthOut:
    return health_out(session, get_source(session, name))


# --- findings ------------------------------------------------------------------------------


@router.get("/sources/{name}/findings")
def findings(
    name: str,
    session: DB,
    status: FindingFilter = "active",
    severity: Severity | None = None,
    category: FindingCategory | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> FindingPage:
    """``active`` = open + confirmed (what still needs attention)."""
    src = get_source(session, name)
    where = [Finding.data_source_id == src.id]
    if status == "active":
        where.append(Finding.status.in_(ACTIVE_FINDING_STATUSES))
    elif status != "all":
        where.append(Finding.status == FindingStatus(status))
    if severity is not None:
        where.append(Finding.severity == severity)
    if category is not None:
        where.append(Finding.category == category)
    total = session.scalar(select(func.count()).select_from(Finding).where(*where)) or 0
    rank = case(SEVERITY_RANK, value=Finding.severity)
    page = session.scalars(
        select(Finding)
        .where(*where)
        .order_by(rank, Finding.check_name, Finding.title, Finding.id)
        .limit(limit)
        .offset(offset)
    )
    return FindingPage(total=total, items=[finding_out(session, f, src.name) for f in page])


@router.get("/findings/{finding_id}")
def finding(finding_id: uuid.UUID, session: DB) -> FindingDetail:
    return finding_detail(session, owned(session, Finding, finding_id))


@router.post("/findings/{finding_id}/review")
def review_finding(finding_id: uuid.UUID, body: FindingReview, session: DB) -> FindingDetail:
    f = owned(session, Finding, finding_id)
    change_status(session, f, FINDING_ACTIONS[body.action], actor=body.by, now=utcnow(),
                  note=body.note)  # fmt: skip
    session.commit()
    return finding_detail(session, f)


# --- rules ---------------------------------------------------------------------------------


@router.get("/sources/{name}/rules")
def rules(name: str, session: DB, status: RuleStatus | None = None) -> list[RuleOut]:
    return [rule_out(session, r) for r in rules_of(session, get_source(session, name), status)]


@router.post("/sources/{name}/rules", status_code=201)
def add_rule(name: str, body: NewRule, session: DB) -> RuleOut:
    """A rule written by a person: validated against the database's structure, active at once."""
    src = get_source(session, name)
    rule = add_user_rule(
        session, src, load_catalog(session, src), parse_spec(body.spec), body.by, utcnow()
    )
    session.commit()
    return rule_out(session, rule)


@router.get("/rules/{rule_id}")
def rule(rule_id: uuid.UUID, session: DB) -> RuleOut:
    return rule_out(session, owned(session, Rule, rule_id))


@router.post("/rules/{rule_id}/review")
def review_a_rule(rule_id: uuid.UUID, body: RuleReview, session: DB) -> RuleOut:
    r = owned(session, Rule, rule_id)
    review_rule(session, r, RULE_ACTIONS[body.action], actor=body.by, now=utcnow(),
                note=body.note)  # fmt: skip
    session.commit()
    return rule_out(session, r)


# --- relationships -------------------------------------------------------------------------


@router.get("/sources/{name}/relationships")
def relationships(name: str, session: DB, include_rejected: bool = False) -> list[RelationshipOut]:
    catalog = load_catalog(session, get_source(session, name))
    found = [
        relationship_out(session, rel)
        for rel in current_relationships(session, catalog)
        if include_rejected or rel.status is not RelationshipStatus.REJECTED
    ]
    return sorted(found, key=lambda r: (r.from_table, r.from_columns, r.to_table))


@router.post("/relationships/{relationship_id}/review")
def review_relationship(
    relationship_id: uuid.UUID, body: RelationshipReview, session: DB
) -> RelationshipOut:
    rel = owned(session, Relationship, relationship_id)
    if rel.valid_to is not None:
        raise ContextError("this relationship has been replaced by a newer version")
    confirmed = body.action == "confirm"
    status = RelationshipStatus.CONFIRMED if confirmed else RelationshipStatus.REJECTED
    review(session, rel, status, body.by, utcnow())
    session.commit()
    return relationship_out(session, rel)


# --- questions -----------------------------------------------------------------------------


@router.get("/sources/{name}/questions")
def questions(
    name: str, session: DB, limit: Annotated[int, Query(ge=1, le=100)] = 10
) -> list[QuestionOut]:
    """The most useful unanswered questions about this database, best first."""
    asked = generate(session, get_source(session, name), now=utcnow(), limit=limit)
    return [QuestionOut(key=q.key, kind=q.kind, prompt=q.prompt, hint=q.hint,
                        suggested=q.default) for q in asked]  # fmt: skip


@router.post("/sources/{name}/answers")
def answer_question(name: str, body: Answer, session: DB) -> AnswerOut:
    src = get_source(session, name)
    found = {q.key: q for q in generate(session, src, now=utcnow(), limit=10_000)}
    if body.key not in found:
        raise ContextError(f"no open question {body.key!r} (already answered, or never asked)")
    outcome = answer(session, src, found[body.key], body.value, by=body.by, now=utcnow())
    session.commit()
    return AnswerOut(outcome=outcome)
