import procrastinate.testing

from sharpline.config import Settings
from sharpline_worker.app import HEARTBEAT_CRON, build_app, procrastinate_conninfo

SETTINGS = Settings(database_url="postgresql://u:p@h/d", env="test")


async def test_heartbeat_runs_with_in_memory_connector() -> None:
    connector = procrastinate.testing.InMemoryConnector()
    app = build_app(SETTINGS, connector=connector)
    async with app.open_async():
        job_id = await app.tasks["sharpline_worker.app.heartbeat"].defer_async(timestamp=0)
        await app.run_worker_async(queues=["ops"], wait=False, install_signal_handlers=False)
    # The worker also fires the periodic schedule, so assert on the job we deferred.
    assert connector.jobs[job_id]["status"] == "succeeded"


def test_heartbeat_is_periodic_every_five_minutes() -> None:
    app = build_app(SETTINGS, connector=procrastinate.testing.InMemoryConnector())
    crons = [p.cron for p in app.periodic_registry.periodic_tasks.values()]
    assert HEARTBEAT_CRON == "*/5 * * * *"
    assert crons == [HEARTBEAT_CRON]


def test_conninfo_strips_sqlalchemy_driver_suffix() -> None:
    # psycopg's conninfo parser rejects the "+psycopg" SQLAlchemy driver marker.
    assert procrastinate_conninfo("postgres://u:p@h:5432/d") == "postgresql://u:p@h:5432/d"
    assert procrastinate_conninfo("postgresql+psycopg://u:p@h/d") == "postgresql://u:p@h/d"
