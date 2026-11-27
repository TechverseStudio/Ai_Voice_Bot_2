"""Database Schema Migration & Initialization CLI for Margadarshak AI Platform."""

import argparse
import logging

from backend.config import DATABASE_URL
from backend.crm import Base, create_tables
from backend.db_migration import get_engine_for_url

logger = logging.getLogger("db_migrate")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def init_database(db_url: str = DATABASE_URL) -> None:
    """Initialize all schema tables and indexes on target database."""
    logger.info(f"Initializing database schema on: {db_url}")
    engine = get_engine_for_url(db_url)
    Base.metadata.create_all(engine)
    logger.info("All database tables and constraints successfully created.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Initialize or migrate database schema.")
    parser.add_argument("--url", default=DATABASE_URL, help="Target Database URL (PostgreSQL or SQLite)")
    args = parser.parse_args()
    init_database(args.url)
