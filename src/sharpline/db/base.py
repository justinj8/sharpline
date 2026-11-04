"""Declarative base with deterministic constraint names (so migrations are diffable)."""

from collections.abc import Iterable
from enum import StrEnum

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def in_values(column: str, values: Iterable[StrEnum | str]) -> str:
    """SQL for a CHECK constraint restricting a text column to a closed vocabulary."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


EXTERNALLY_MANAGED_PREFIX = "procrastinate_"


def include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """Alembic autogenerate filter: ignore schema objects owned by procrastinate.

    Its tables come from the vendored SQL in migration 0002, not from our models, so
    autogenerate must neither diff nor propose dropping them.
    """
    if type_ == "table":
        return not (name or "").startswith(EXTERNALLY_MANAGED_PREFIX)
    table = getattr(obj, "table", None)
    table_name = getattr(table, "name", "") or ""
    return not table_name.startswith(EXTERNALLY_MANAGED_PREFIX)
