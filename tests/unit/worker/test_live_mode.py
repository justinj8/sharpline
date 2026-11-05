import procrastinate.testing

from sharpline.config import Settings
from sharpline_worker.app import CAPTURE_CRON, CAPTURE_QUEUE, OPS_QUEUE, build_app, worker_queues


def _settings(*, live: bool) -> Settings:
    return Settings(database_url="postgresql://u:p@h/d", env="test", live_capture=live)


def _periodic(live: bool) -> dict[str, tuple[str, str | None]]:
    app = build_app(_settings(live=live), connector=procrastinate.testing.InMemoryConnector())
    return {
        p.periodic_id: (p.cron, p.task.queue) for p in app.periodic_registry.periodic_tasks.values()
    }


def test_live_mode_off_registers_only_heartbeat() -> None:
    assert _periodic(live=False) == {"heartbeat": ("*/5 * * * *", OPS_QUEUE)}
    assert worker_queues(_settings(live=False)) == [OPS_QUEUE]


def test_live_mode_on_registers_capture_job() -> None:
    assert CAPTURE_CRON == "*/15 * * * *" and CAPTURE_QUEUE == "capture"
    assert _periodic(live=True) == {
        "heartbeat": ("*/5 * * * *", OPS_QUEUE),
        "plan_and_enqueue": (CAPTURE_CRON, CAPTURE_QUEUE),
    }
    # The worker must consume the capture queue, or the periodic jobs would pile up.
    assert worker_queues(_settings(live=True)) == [OPS_QUEUE, CAPTURE_QUEUE]
