import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from support import Repository
from autocd.config import read
from autocd.locks import file_lock
from autocd.paths import Paths, project_id
from autocd.runner import observe, open_state, set_enabled, worker
from autocd.scheduler import clock, from_project, ready


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="autocd-race-")
        self.root = Path(self.temp.name)
        self.paths = Paths(self.root / "state")
        self.repo = Repository(self.root)
        self.ident = project_id(self.repo.project)
        await set_enabled(self.paths, self.repo.project, True)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_pause_during_network_request_discards_stale_result(self):
        entered, proceed = asyncio.Event(), asyncio.Event()

        async def delayed(*args):
            entered.set()
            await proceed.wait()
            return self.repo.sha

        with patch("autocd.runner.remote_sha", side_effect=delayed):
            task = asyncio.create_task(observe(self.paths, path=self.repo.project))
            await entered.wait()
            await set_enabled(self.paths, self.repo.project, False)
            proceed.set()
            result = await task
        self.assertEqual(result["skipped"], "changed_during_check")
        with open_state(self.paths) as state:
            row = state.project(self.ident)
            self.assertFalse(row["enabled"])
            self.assertIsNone(row["candidate_sha"])
            self.assertEqual(state.queued(), [])

    async def test_duplicate_check_does_not_reset_another_process_window(self):
        await observe(self.paths, path=self.repo.project)
        with open_state(self.paths) as state:
            before = state.project(self.ident)
        with file_lock(self.paths.home / "run" / f"check-{self.ident}.lock"):
            result = await observe(self.paths, path=self.repo.project)
        self.assertEqual(result["skipped"], "check_running")
        with open_state(self.paths) as state:
            self.assertEqual(state.project(self.ident)["stable_monotonic"], before["stable_monotonic"])

    async def test_reboot_invalidates_persisted_window(self):
        await observe(self.paths, path=self.repo.project)
        with open_state(self.paths) as state:
            state.update(self.ident, boot_id="previous-boot", stable_monotonic=clock() - 100,
                         observed_monotonic=clock())
            row = state.project(self.ident)
            self.assertIsNone(from_project(row).since)
            self.assertFalse(ready(row, read(self.repo.project)))
        await observe(self.paths, path=self.repo.project)
        with open_state(self.paths) as state:
            row = state.project(self.ident)
            self.assertNotEqual(row["boot_id"], "previous-boot")
            self.assertGreater(row["stable_monotonic"], clock() - 2)
            self.assertEqual(state.queued(), [])

    async def test_expired_queued_window_cannot_deploy(self):
        await observe(self.paths, path=self.repo.project)
        with open_state(self.paths) as state:
            state.update(self.ident, observed_monotonic=clock() - 100)
            job = state.enqueue(self.ident, self.repo.sha, read(self.repo.project).fingerprint)
        await worker(self.paths)
        with open_state(self.paths) as state:
            self.assertEqual(state.job(job)["status"], "cancelled")
        self.assertFalse((self.repo.project / "result").exists())

    async def test_disabled_scheduler_does_not_run_scheduled_check(self):
        result = await observe(self.paths, ident=self.ident, scheduled=True)
        self.assertEqual(result["skipped"], "schedule_stopped")
        with open_state(self.paths) as state:
            self.assertIsNone(state.project(self.ident)["stable_since"])

    async def test_resume_after_suspend_restarts_window(self):
        with patch("autocd.scheduler.time.clock_gettime", return_value=100):
            await observe(self.paths, path=self.repo.project)
        with patch("autocd.scheduler.time.clock_gettime", return_value=2000):
            with open_state(self.paths) as state:
                self.assertFalse(ready(state.project(self.ident), read(self.repo.project)))
            result = await observe(self.paths, path=self.repo.project)
        self.assertIsNone(result["job_id"])
        with open_state(self.paths) as state:
            self.assertEqual(state.project(self.ident)["stable_monotonic"], 2000)
            self.assertEqual(state.queued(), [])
