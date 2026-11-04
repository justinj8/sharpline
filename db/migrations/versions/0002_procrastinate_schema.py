"""procrastinate job-queue schema

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-04 13:20:00.000000

Applies the procrastinate schema vendored in `db/migrations/sql/`, pinned to the
procrastinate version it came from, so this migration always creates the same objects
no matter which procrastinate version is installed later. A procrastinate upgrade gets
its own migration built from the library's shipped migration SQL.
"""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA_SQL = Path(__file__).resolve().parents[1] / "sql" / "procrastinate_3.10.0_schema.sql"

DROP_PROCRASTINATE_OBJECTS = """
DO $$
DECLARE r record;
BEGIN
    FOR r IN SELECT tablename FROM pg_tables
             WHERE schemaname = 'public' AND tablename LIKE 'procrastinate\\_%' LOOP
        EXECUTE format('DROP TABLE IF EXISTS public.%I CASCADE', r.tablename);
    END LOOP;
    FOR r IN SELECT p.oid::regprocedure AS sig FROM pg_proc p
             JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname = 'public' AND p.proname LIKE 'procrastinate\\_%' LOOP
        EXECUTE format('DROP FUNCTION IF EXISTS %s CASCADE', r.sig);
    END LOOP;
    FOR r IN SELECT t.typname FROM pg_type t
             JOIN pg_namespace n ON n.oid = t.typnamespace
             WHERE n.nspname = 'public' AND t.typname LIKE 'procrastinate\\_%'
               AND t.typtype IN ('e', 'c') LOOP
        EXECUTE format('DROP TYPE IF EXISTS public.%I CASCADE', r.typname);
    END LOOP;
END $$;
"""


def _execute_raw(sql: str) -> None:
    """Run SQL on the migration's own connection with no parameters at all.

    The schema contains literal `:` and `%` characters. SQLAlchemy's text() would read
    `:name` as a bind parameter, and passing even an empty parameter list makes psycopg
    read `%` as a placeholder; a parameterless DBAPI cursor call does neither.
    """
    dbapi_conn = op.get_bind().connection.dbapi_connection
    assert dbapi_conn is not None
    with dbapi_conn.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_raw(SCHEMA_SQL.read_text())


def downgrade() -> None:
    _execute_raw(DROP_PROCRASTINATE_OBJECTS)
