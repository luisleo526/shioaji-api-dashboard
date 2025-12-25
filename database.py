import os
import logging
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import sessionmaker, declarative_base

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/shioaji"
)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Initialize database tables with race condition handling for multiple workers."""
    try:
        Base.metadata.create_all(bind=engine)
    except (IntegrityError, ProgrammingError) as e:
        # Handle race condition when multiple workers try to create tables simultaneously
        # The table/sequence already exists, which is fine
        if "already exists" in str(e) or "duplicate key" in str(e).lower():
            logger.info("Database tables already exist, skipping creation")
        else:
            raise

