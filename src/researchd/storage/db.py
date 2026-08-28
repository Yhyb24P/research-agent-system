from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker


def sqlite_url(path: Path) -> URL:
    return URL.create("sqlite", database=str(path.resolve()))


def create_sqlite_engine(path: Path) -> Engine:
    engine = create_engine(sqlite_url(path), future=True)

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection: object, connection_record: object) -> None:
        del connection_record
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
