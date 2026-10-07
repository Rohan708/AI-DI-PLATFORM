"""Human-readable summary of what the metadata store knows about a source
(``aide source show``). Used for the Stage 1.3 "matches the real database" check, and the
starting point for Stage 1.4's auto-generated data dictionary."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai_data_engineer.graph.models import DataSource, IngestionRun
from ai_data_engineer.graph.queries import (
    get_asset_profile_history,
    get_column_profile_history,
    get_current_structure,
)


def render_source_summary(session: Session, source: DataSource) -> str:
    runs = session.scalar(
        select(func.count())
        .select_from(IngestionRun)
        .where(IngestionRun.data_source_id == source.id)
    )
    last_run = session.scalars(
        select(IngestionRun)
        .where(IngestionRun.data_source_id == source.id)
        .order_by(IngestionRun.started_at.desc())
        .limit(1)
    ).first()
    out = [
        f"# {source.name} ({source.kind.value})",
        "",
        f"Scans: {runs}"
        + (
            f"; last {last_run.started_at.isoformat()} ({last_run.status.value})"
            if last_run
            else ""
        ),
    ]
    for asset in get_current_structure(session, source.id):
        latest = get_asset_profile_history(session, asset.asset_key, limit=1)
        rows = "?"
        if latest and latest[0].row_count is not None:
            rows = f"{'~' if latest[0].row_count_is_estimate else ''}{latest[0].row_count:,}"
        pk = ", ".join(asset.primary_key) or "**none**"
        out += [
            "",
            f"## {asset.schema_name}.{asset.name}  ({asset.kind.value}, {rows} rows, PK: {pk})",
            "",
            "| Column | Type | Family | Nullable | Null % | Distinct |",
            "|---|---|---|---|---|---|",
        ]
        for column in asset.columns:
            profile = get_column_profile_history(session, column.column_key, limit=1)
            null_pct = distinct = "-"
            if profile:
                p = profile[0]
                if p.null_rate is not None:
                    null_pct = f"{p.null_rate:.1%}"
                if p.distinct_count is not None:
                    distinct = f"{'~' if p.distinct_is_approx else ''}{p.distinct_count:,}"
            out.append(
                f"| {column.name} | {column.native_type} | {column.type_family.value} | "
                f"{'yes' if column.is_nullable else 'no'} | {null_pct} | {distinct} |"
            )
    return "\n".join(out) + "\n"
