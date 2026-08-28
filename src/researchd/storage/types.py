from datetime import UTC, datetime
from typing import Any

from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import String, TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """Persist aware UTC timestamps losslessly on SQLite as ISO-8601 text."""

    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> str | None:
        del dialect
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("database timestamps must be timezone-aware")
        return value.astimezone(UTC).isoformat()

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        del dialect
        if value is None:
            return None
        return datetime.fromisoformat(str(value)).astimezone(UTC)
