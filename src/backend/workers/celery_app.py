from celery import Celery

from backend.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "nexora",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["backend.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    beat_schedule={
        "nexora-refresh-monitored-matches": {
            "task": "backend.workers.tasks.enqueue_monitored_match_jobs",
            "schedule": settings.scheduled_collection_interval_minutes * 60,
        },
        "nexora-dispatch-notification-outbox": {
            "task": "backend.workers.tasks.dispatch_pending_notifications",
            "schedule": 30,
        },
    },
)
