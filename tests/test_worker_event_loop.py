import asyncio

from backend.workers import tasks


def test_celery_async_operations_reuse_the_same_event_loop() -> None:
    async def current_loop_id() -> int:
        return id(asyncio.get_running_loop())

    try:
        first_loop_id = tasks._run_worker_async(current_loop_id())
        second_loop_id = tasks._run_worker_async(current_loop_id())

        assert first_loop_id == second_loop_id
        assert tasks._worker_loop is not None
        assert not tasks._worker_loop.is_closed()
    finally:
        tasks._close_worker_async_loop()
