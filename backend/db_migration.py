"""Database Migration & Rehearsal Tool for Margadarshak AI CRM.

Supports migrating CRM records from SQLite to PostgreSQL (or SQLite-to-SQLite copy
for verification), validating record counts across leads, campaigns, call logs,
and appointments.
"""

import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

from sqlalchemy import create_engine, select, func, text, inspect
from sqlalchemy.orm import Session, sessionmaker

from backend.config import DATABASE_URL, DB_POOL_SIZE, DB_MAX_OVERFLOW, DB_POOL_RECYCLE, DB_POOL_TIMEOUT
from backend.crm import (
    Base,
    ImportBatch,
    Contact,
    Campaign,
    CampaignContact,
    Lead,
    CallSummary,
    OutboundCall,
    BrowserSession,
    Appointment,
    AuditLog,
    DndEntry,
    FollowUpTask,
    CampaignJob,
    ActiveCallChannel,
)

logger = logging.getLogger("db_migration")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

MODELS_IN_ORDER = [
    ImportBatch,
    Contact,
    Campaign,
    CampaignContact,
    Lead,
    CallSummary,
    OutboundCall,
    BrowserSession,
    Appointment,
    AuditLog,
    DndEntry,
    FollowUpTask,
    CampaignJob,
    ActiveCallChannel,
]


def get_engine_for_url(url: str):
    """Build engine with appropriate options for dialect."""
    opts = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        opts["connect_args"] = {"check_same_thread": False}
    elif url.startswith("postgresql"):
        opts.update({
            "pool_size": DB_POOL_SIZE,
            "max_overflow": DB_MAX_OVERFLOW,
            "pool_recycle": DB_POOL_RECYCLE,
            "pool_timeout": DB_POOL_TIMEOUT,
        })
    return create_engine(url, **opts)


def migrate_database(source_url: str, target_url: str) -> dict[str, Any]:
    """Migrate all CRM data from source database to target database and verify integrity."""
    logger.info(f"Initiating migration from '{source_url}' to '{target_url}'")
    source_engine = get_engine_for_url(source_url)
    target_engine = get_engine_for_url(target_url)

    # 1. Create all schema tables on target
    Base.metadata.create_all(target_engine)

    SourceSession = sessionmaker(bind=source_engine, expire_on_commit=False)
    TargetSession = sessionmaker(bind=target_engine, expire_on_commit=False)

    stats: dict[str, dict[str, int]] = {}

    with SourceSession() as src, TargetSession() as tgt:
        for model in MODELS_IN_ORDER:
            table_name = model.__tablename__
            # Count source rows
            src_count = src.scalar(select(func.count()).select_from(model)) or 0
            stats[table_name] = {"source_rows": src_count, "migrated_rows": 0}

            if src_count == 0:
                continue

            # Read all rows from source
            items = list(src.scalars(select(model).order_by(model.id)))
            for item in items:
                # Create detached copy for target
                data = {c.name: getattr(item, c.name) for c in model.__table__.columns}
                new_item = model(**data)
                tgt.merge(new_item)

            tgt.commit()
            tgt_count = tgt.scalar(select(func.count()).select_from(model)) or 0
            stats[table_name]["migrated_rows"] = tgt_count
            logger.info(f"Table '{table_name}': {tgt_count}/{src_count} rows copied.")

    # 2. Verification checks across core business entities
    with SourceSession() as src, TargetSession() as tgt:
        verification = {
            "leads": {
                "source": src.scalar(select(func.count(Lead.id))) or 0,
                "target": tgt.scalar(select(func.count(Lead.id))) or 0,
            },
            "campaigns": {
                "source": src.scalar(select(func.count(Campaign.id))) or 0,
                "target": tgt.scalar(select(func.count(Campaign.id))) or 0,
            },
            "call_logs": {
                "source": src.scalar(select(func.count(OutboundCall.id))) or 0,
                "target": tgt.scalar(select(func.count(OutboundCall.id))) or 0,
            },
            "appointments": {
                "source": src.scalar(select(func.count(Appointment.id))) or 0,
                "target": tgt.scalar(select(func.count(Appointment.id))) or 0,
            },
        }

    all_matched = all(v["source"] == v["target"] for v in verification.values())

    report = {
        "success": all_matched,
        "source_url": source_url,
        "target_url": target_url,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tables": stats,
        "verification": verification,
    }
    logger.info(f"Migration completed. All core entity counts matched: {all_matched}")
    return report


def rehearse_migration_on_copy(source_db_path: str = "data/voice_agent.db") -> dict[str, Any]:
    """Create a temporary copy of SQLite database and rehearse migration into a fresh database."""
    source_file = Path(source_db_path)
    if not source_file.exists():
        # Create minimal source if missing
        from backend.crm import create_tables
        create_tables()

    temp_dir = tempfile.mkdtemp(prefix="rehearsal_db_")
    copy_source_path = Path(temp_dir) / "source_copy.db"
    target_copy_path = Path(temp_dir) / "target_rehearsal.db"

    try:
        shutil.copy2(str(source_file), str(copy_source_path))
        src_url = f"sqlite:///{copy_source_path}"
        tgt_url = f"sqlite:///{target_copy_path}"

        report = migrate_database(src_url, tgt_url)
        report["rehearsal_mode"] = True
        return report
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Margadarshak Database Migration")
    parser.add_argument("--source", default=DATABASE_URL, help="Source database URL")
    parser.add_argument("--target", default=None, help="Target database URL (e.g. postgresql+psycopg://...)")
    parser.add_argument("--rehearse", action="store_true", help="Rehearse migration on a local copy")

    args = parser.parse_args()
    if args.rehearse or not args.target:
        res = rehearse_migration_on_copy()
        print(json.dumps(res, indent=2))
    else:
        res = migrate_database(args.source, args.target)
        print(json.dumps(res, indent=2))
