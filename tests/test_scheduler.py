from backend.workers.celery_app import celery_app


def test_celery_schedule_is_configured():
    schedule = celery_app.conf.beat_schedule
    assert "nexora-refresh-monitored-matches" in schedule
    entry = schedule["nexora-refresh-monitored-matches"]
    assert entry["task"] == "backend.workers.tasks.enqueue_monitored_match_jobs"
    assert entry["schedule"] > 0
