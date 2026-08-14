"""Tests for cron resilience: non-blocking job execution and semaphore safety."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import kiro_crew.heartbeat as hb_mod
from kiro_crew.acp.client import AcpClient
from kiro_crew.cron import CronJob, CronService
from kiro_crew.heartbeat import _HEADER, HeartbeatService


async def _wait_for(predicate, timeout=5.0, interval=0.05):
    """Poll until predicate is true or timeout."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("Timed out waiting for predicate")
        await asyncio.sleep(interval)


class TestCronNonBlocking:
    """Verify that a slow/stuck job does not block other jobs."""

    @pytest.mark.asyncio
    async def test_slow_job_does_not_block_fast_job(self, tmp_path: Path) -> None:
        finished: dict[str, float] = {}

        async def callback(job: CronJob) -> None:
            if job.name == "slow":
                await asyncio.sleep(2)
            finished[job.name] = time.monotonic()

        svc = CronService(base_dir=tmp_path, on_job=callback)
        await svc.start()
        svc.add_job("slow", "msg", every_secs=60)
        svc.add_job("fast", "msg", every_secs=60)
        for j in svc._jobs:
            j.last_run_ts = time.time() - 120

        await svc._on_timer()
        await _wait_for(lambda: len(finished) == 2, timeout=10.0)
        assert finished["fast"] < finished["slow"]
        assert finished["slow"] - finished["fast"] > 1.0
        await svc.stop()

    @pytest.mark.asyncio
    async def test_failing_job_does_not_block_others(self, tmp_path: Path) -> None:
        executed: list[str] = []

        async def callback(job: CronJob) -> None:
            if job.name == "fail":
                raise RuntimeError("boom")
            executed.append(job.name)

        svc = CronService(base_dir=tmp_path, on_job=callback)
        await svc.start()
        svc.add_job("fail", "msg", every_secs=60)
        svc.add_job("ok", "msg", every_secs=60)
        for j in svc._jobs:
            j.last_run_ts = time.time() - 120

        await svc._on_timer()
        await _wait_for(lambda: "ok" in executed)
        fail_job = next(j for j in svc._jobs if j.name == "fail")
        await _wait_for(lambda: fail_job.last_status == "error")
        await svc.stop()

    @pytest.mark.asyncio
    async def test_job_result_merged_to_disk(self, tmp_path: Path) -> None:
        async def callback(job: CronJob) -> None:
            pass

        svc = CronService(base_dir=tmp_path, on_job=callback)
        await svc.start()
        svc.add_job("test", "msg", every_secs=60)
        job_id = svc._jobs[0].id
        svc._jobs[0].last_run_ts = time.time() - 120

        await svc._on_timer()
        # Await the job task itself rather than polling for the in-memory
        # last_status. _execute sets last_status BEFORE _run_job_isolated's
        # finally block offloads _merge_job_result to a worker thread, so a
        # predicate on last_status can go true while crons.json still holds the
        # pre-run state — the disk assertion below would then read a store that
        # was never written. The task completes only after that offloaded merge
        # returns, making it the one signal that actually implies the write.
        task = svc._running_tasks[job_id]
        await task
        assert svc._jobs[0].last_status == "ok"

        # Reload from disk and verify
        svc2 = CronService(base_dir=tmp_path)
        svc2._load()
        assert svc2._jobs[0].last_status == "ok"
        await svc.stop()

    @pytest.mark.asyncio
    async def test_task_references_stored(self, tmp_path: Path) -> None:
        """Verify fire-and-forget tasks are stored to prevent GC collection."""
        gate = asyncio.Event()

        async def callback(job: CronJob) -> None:
            await gate.wait()

        svc = CronService(base_dir=tmp_path, on_job=callback)
        await svc.start()
        svc.add_job("held", "msg", every_secs=60)
        svc._jobs[0].last_run_ts = time.time() - 120

        await svc._on_timer()
        job_id = svc._jobs[0].id
        assert job_id in svc._running_tasks
        gate.set()
        await _wait_for(lambda: job_id not in svc._running_tasks)
        await svc.stop()


class TestArmTimer:
    """Verify _arm_timer always creates a new timer."""

    @pytest.mark.asyncio
    async def test_arm_timer_always_arms(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._running = True
        svc._executing.add("some_job")

        svc._arm_timer()
        assert svc._timer_task is not None
        assert not svc._timer_task.done()
        await svc.stop()


class TestJobCompletionRearms:
    """A job finishing must re-arm the timer (#3651): a long-running job is
    invisible to `_next_wake_secs` while `_executing`, so the wake computed
    during its run can arm up to `_TIMER_POLL_SECS` (30s) out. Without a
    re-arm on completion, the service waits for that stale wake instead of
    the job's true (often much sooner) next-due delay.
    """

    @pytest.mark.asyncio
    async def test_job_completion_calls_arm_timer(self, tmp_path: Path) -> None:
        """Wiring guard: `_run_job_isolated`'s finally must call `_arm_timer`.
        `wraps=` keeps the real re-arm behavior intact while tracking the call,
        so this fails loudly (not just silently) if the call site is reverted."""
        gate = asyncio.Event()

        async def callback(job: CronJob) -> None:
            await gate.wait()

        svc = CronService(base_dir=tmp_path, on_job=callback)
        await svc.start()
        svc.add_job("j", "msg", every_secs=60)
        svc._jobs[0].last_run_ts = time.time() - 120
        job_id = svc._jobs[0].id

        await svc._on_timer()
        assert job_id in svc._executing

        with patch.object(svc, "_arm_timer", wraps=svc._arm_timer) as mock_arm:
            gate.set()
            await svc._running_tasks[job_id]
            assert mock_arm.called, "job completion must re-arm the timer"
        await svc.stop()

    @pytest.mark.asyncio
    async def test_arm_timer_does_not_cancel_an_in_flight_on_timer(self, tmp_path: Path) -> None:
        """The hazard the naive fix has: `_on_timer` awaits `asyncio.to_thread`
        for its due-scan, a real interleave window. A DIFFERENT task (a job
        completing) calling `_arm_timer` during that window must not cancel
        the in-flight tick -- that would drop any due jobs not yet dispatched
        this sweep. Stands in for `_on_timer`'s one await point with a plain
        gate so the interleave is deterministic rather than timing-dependent.
        """
        svc = CronService(base_dir=tmp_path)
        svc._running = True
        gate = asyncio.Event()

        async def fake_tick() -> None:
            svc._on_timer_running = True
            try:
                await gate.wait()
            finally:
                svc._on_timer_running = False

        original_task = asyncio.create_task(fake_tick())
        svc._timer_task = original_task
        await asyncio.sleep(0)  # let fake_tick start and set the flag
        assert svc._on_timer_running is True

        svc._arm_timer()  # stands in for a job's completion re-arm

        assert svc._timer_task is original_task
        assert not svc._timer_task.cancelled()
        assert not svc._timer_task.done()

        gate.set()
        await svc._timer_task  # must complete normally, no CancelledError
        await svc.stop()

    @pytest.mark.asyncio
    async def test_deferred_rearm_still_happens_once_on_timer_completes(
        self, tmp_path: Path
    ) -> None:
        """The re-arm request skipped during the in-flight window above must
        not be LOST -- `_tick`'s own finally re-arms unconditionally once
        `_on_timer` returns, by which point a job that finished during the
        window is out of `_executing`, so that arm reflects its true delay."""
        svc = CronService(base_dir=tmp_path)
        svc._running = True
        gate = asyncio.Event()

        async def fake_tick() -> None:
            svc._on_timer_running = True
            try:
                await gate.wait()
            finally:
                svc._on_timer_running = False
                svc._arm_timer()  # mirrors _tick's own post-_on_timer re-arm

        original_task = asyncio.create_task(fake_tick())
        svc._timer_task = original_task
        await asyncio.sleep(0)

        svc._arm_timer()  # deferred re-arm request, swallowed per the guard
        assert not original_task.cancelled()

        gate.set()
        await original_task
        await asyncio.sleep(0)  # let the finally's _arm_timer() install the new task
        # A genuinely new timer task now exists -- the deferred request was
        # honored once it was safe, not dropped on the floor.
        assert svc._timer_task is not original_task
        assert svc._timer_task is not None and not svc._timer_task.done()
        await svc.stop()

    @pytest.mark.asyncio
    async def test_on_timer_running_flag_true_only_during_dispatch(
        self, tmp_path: Path
    ) -> None:
        """Confirms the flag is wired into the REAL `_tick`, not just the
        `fake_tick` stand-ins above: true while `_on_timer` runs, false
        immediately before and after."""
        svc = CronService(base_dir=tmp_path)
        svc._running = True
        seen_during: list[bool] = []
        real_on_timer = svc._on_timer

        async def spy_on_timer() -> None:
            seen_during.append(svc._on_timer_running)
            await real_on_timer()

        svc._on_timer = spy_on_timer  # type: ignore[method-assign]
        svc._effective_delay = lambda: 0.01  # type: ignore[method-assign]

        assert svc._on_timer_running is False
        svc._arm_timer()
        task = svc._timer_task
        assert task is not None
        await asyncio.wait_for(task, timeout=5)

        assert seen_during == [True]
        assert svc._on_timer_running is False
        await svc.stop()


class TestAcpResponsiveness:
    """Verify ACP zombie detection via is_responsive."""

    def test_fresh_client_is_responsive(self) -> None:
        client = AcpClient()
        # No process, so _is_process_alive is False
        assert not client.is_responsive()

    def test_stale_activity_detected(self) -> None:
        client = AcpClient()
        # Simulate alive process with stale activity
        client._process = MagicMock()
        client._process.returncode = None
        client._last_activity = time.monotonic() - 700  # 700s ago
        assert not client.is_responsive(stale_threshold=600.0)

    def test_recent_activity_is_responsive(self) -> None:
        client = AcpClient()
        client._process = MagicMock()
        client._process.returncode = None
        client._last_activity = time.monotonic() - 10  # 10s ago
        assert client.is_responsive(stale_threshold=600.0)

    def test_recently_created_is_responsive(self) -> None:
        client = AcpClient()
        client._process = MagicMock()
        client._process.returncode = None
        # _last_activity initialized to time.monotonic() in __init__
        assert client.is_responsive()


class TestHeartbeatParallel:
    """Verify heartbeat tasks run in parallel."""

    @pytest.mark.asyncio
    async def test_tasks_run_concurrently(self, tmp_path: Path) -> None:
        started: list[float] = []

        async def on_task(text: str, deliver: str) -> None:
            started.append(time.monotonic())
            await asyncio.sleep(0.5)

        svc = HeartbeatService(memory=MagicMock(), on_task=on_task)
        # Write 3 tasks
        hb_path = tmp_path / "HEARTBEAT.md"
        hb_path.write_text(_HEADER + "- task1\n- task2\n- task3\n")

        original = hb_mod.heartbeat_path
        hb_mod.heartbeat_path = lambda: hb_path
        try:
            await svc._process_heartbeat_file()
        finally:
            hb_mod.heartbeat_path = original

        assert len(started) == 3
        # All 3 should start within 0.1s of each other (parallel)
        assert max(started) - min(started) < 0.2
