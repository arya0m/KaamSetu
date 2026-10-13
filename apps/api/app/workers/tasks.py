"""Background tasks. Phase 1 has only a ping to prove the queue round-trips."""

from app.workers.celery_app import celery_app


@celery_app.task(name="kaamsetu.ping")  # type: ignore[untyped-decorator]
def ping() -> str:
    return "pong"
