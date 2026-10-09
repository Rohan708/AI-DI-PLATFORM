"""Stage 2.5 against the real lab: no row outliers in clean data, the planted fat-finger
quantities are found (with their primary keys), and the finding resolves once fixed."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ai_data_engineer.db import create_db_engine
from ai_data_engineer.detection.rows import ROW_OUTLIER, check_row_outliers
from ai_data_engineer.graph.models import DataSource, Finding, FindingStatus
from ai_data_engineer.ingestion.postgres import PostgresAdapter
from ai_data_engineer.ingestion.scan import scan_source
from ai_data_engineer.ingestion.settings import ScanSettings
from ai_data_engineer.lab.runner import build_lab, inject

SETTINGS = ScanSettings(exclude_schemas=("aide_lab",))
T1 = datetime(2026, 9, 1, 3, tzinfo=UTC)


def _factory(url: str) -> Callable[[DataSource], Any]:
    return lambda _source: PostgresAdapter(url, SETTINGS)


def test_fat_finger_rows_are_found_in_the_database_and_resolve(
    session: Session, source: DataSource, make_database: Callable[[], str]
) -> None:
    url = make_database()
    lab = create_db_engine(url)
    build_lab(lab, seed=71, size="tiny")
    scan_source(session, source, observed_at=T1, adapter_factory=_factory(url))

    clean = check_row_outliers(session, source, adapter_factory=_factory(url), now=T1)
    assert clean.columns >= 5, clean.errors  # amounts, prices, quantities across schemas
    assert (clean.opened, clean.errors) == (0, {})  # no false alarms on clean data

    (planted,) = inject(lab, ["fat_finger_quantity"])
    found = check_row_outliers(session, source, adapter_factory=_factory(url), now=T1)
    assert found.opened == 1
    finding = session.scalars(select(Finding).where(Finding.check_name == ROW_OUTLIER)).one()
    assert finding.title.startswith(f"{planted.details['rows']} rows in shop.order_items.quantity")
    assert finding.evidence["outlier_rows"] == planted.details["rows"]
    assert finding.evidence["max_ratio"] >= 100
    assert len(finding.evidence["sample_rows"]) == planted.details["rows"]
    assert set(finding.evidence["sample_rows"][0]) == {"order_id", "line_no"}  # keys only

    with lab.begin() as conn:  # someone fixes the typos
        conn.execute(text("UPDATE shop.order_items SET quantity = 1 WHERE quantity = 500"))
    lab.dispose()
    fixed = check_row_outliers(session, source, adapter_factory=_factory(url), now=T1)
    assert fixed.resolved == 1
    assert finding.status is FindingStatus.RESOLVED
