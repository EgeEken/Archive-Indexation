"""SQLite persistence for an archive workspace."""

from .connection import connect
from .schema import SCHEMA_VERSION, apply_migrations

__all__ = ["SCHEMA_VERSION", "apply_migrations", "connect"]
