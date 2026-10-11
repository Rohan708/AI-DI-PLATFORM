"""Stage 2.6 against the real lab: the clean lab has personal data only where its column
names say so; the planted emails in a legacy name field are found, counted inside the
database, and resolve once cleaned up."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ai_data_engineer.db import create_db_engine
from ai_data_engineer.discovery.catalog import load_catalog
from ai_data_engineer.graph.models import (
    ColumnProfile,
    DataSource,
    Finding,
    FindingCategory,
    FindingStatus,
)
from ai_data_engineer.ingestion.postgres import PostgresAdapter
from ai_data_engineer.ingestion.scan import scan_source
from ai_data_engineer.ingestion.settings import ScanSettings
from ai_data_engineer.lab.runner import build_lab, inject
from ai_data_engineer.semantic.run import PII_UNEXPECTED, check_semantic

SETTINGS = ScanSettings(exclude_schemas=("aide_lab",))
T1 = datetime(2026, 9, 25, 3, tzinfo=UTC)


def _factory(url: str) -> Callable[[DataSource], Any]:
    return lambda _source: PostgresAdapter(url, SETTINGS)


def test_personal_data_map_and_stray_emails(
    session: Session, source: DataSource, make_database: Callable[[], str]
) -> None:
    url = make_database()
    lab = create_db_engine(url)
    build_lab(lab, seed=95, size="tiny")
    scan_source(session, source, observed_at=T1, adapter_factory=_factory(url))

    clean = check_semantic(session, source, adapter_factory=_factory(url))
    assert clean.skipped is None
    assert clean.opened == 0, [(f.table, f.column, f.kind) for f in clean.mapped]
    mapped = {(f.table, f.column, f.kind) for f in clean.mapped}
    assert ("shop.customers", "email", "email") in mapped
    email = load_catalog(session, source).tables["shop.customers"].columns["email"]
    profile = session.scalars(
        select(ColumnProfile).where(ColumnProfile.column_key == email.column_key)
    ).one()
    assert profile.extra["pii"]["email"] >= 0.99  # remembered for docs and the UI

    (planted,) = inject(lab, ["emails_in_names"])
    found = check_semantic(session, source, adapter_factory=_factory(url))
    assert found.opened == 1
    finding = session.scalars(select(Finding).where(Finding.check_name == PII_UNEXPECTED)).one()
    assert finding.category is FindingCategory.SEMANTIC
    assert finding.evidence["kind"] == "email"
    assert finding.evidence["matching_values"] == planted.details["rows"]
    assert "legacy.CUST_MASTER.CUST_NM" in finding.title
    assert "@" not in str(finding.evidence)  # counts only, never a value

    with lab.begin() as conn:  # someone cleans the field up
        cleanup = """UPDATE legacy."CUST_MASTER" SET "CUST_NM" = 'X' WHERE "CUST_NM" LIKE '%@%'"""
        conn.execute(text(cleanup))
    lab.dispose()
    fixed = check_semantic(session, source, adapter_factory=_factory(url))
    assert fixed.resolved == 1
    assert finding.status is FindingStatus.RESOLVED
