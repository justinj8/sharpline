import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration


def test_procrastinate_tables_exist(db_session: Session) -> None:
    names = set(inspect(db_session.get_bind()).get_table_names())
    assert {"procrastinate_jobs", "procrastinate_events", "procrastinate_periodic_defers"} <= names
