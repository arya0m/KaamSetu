"""Celery application. Run with: celery -A app.workers.celery_app worker"""

from celery import Celery

from app.config import get_settings


def create_celery() -> Celery:
    settings = get_settings()
    celery = Celery("kaamsetu", broker=settings.broker_url, include=["app.workers.tasks"])
    celery.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        task_ignore_result=True,
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        broker_connection_retry_on_startup=True,
        timezone="UTC",
    )
    return celery


celery_app = create_celery()
