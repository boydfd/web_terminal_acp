import asyncio

import pytest

from app.services import summary_worker


@pytest.mark.asyncio
async def test_summary_worker_loop_yields_after_processed_job(monkeypatch) -> None:
    process_calls = 0
    sleeps: list[float] = []

    async def fake_process_summary_jobs_once(*args, **kwargs):
        nonlocal process_calls
        process_calls += 1
        if process_calls > 1:
            raise AssertionError("worker loop did not yield after processing a job")
        return True

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        raise asyncio.CancelledError

    monkeypatch.setattr(summary_worker, "process_summary_jobs_once", fake_process_summary_jobs_once)
    monkeypatch.setattr(summary_worker.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await summary_worker.run_summary_job_worker_loop(lambda: None)

    assert sleeps == [2.0]
