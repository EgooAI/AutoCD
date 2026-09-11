import asyncio
import os
from pathlib import Path
import signal
import tempfile
import unittest

from support import OneShot, Repository, eventually, git
from autocd.config import AutoCDError


class DeploymentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="autocd-it-")
        self.root = Path(self.temp.name)
        self.client = OneShot(self.root)

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def enable(self, repo):
        return await self.client.call("enable", repo.project)

    async def manual(self, repo):
        return await self.client.call("deploy", repo.project, "--queue-only")

    async def worker(self):
        return await self.client.call("worker", check=False)

    async def started(self, path):
        async def exists():
            return path.exists()
        await eventually(exists)

    async def test_separate_processes_preserve_cooling_and_exit(self):
        repo = Repository(self.root)
        await self.enable(repo)
        await self.client.call("tick", repo.project)
        first = await self.client.row(repo)
        self.assertIsNotNone(first["stable_monotonic"])
        self.assertEqual(await self.client.history(repo), [])
        await self.client.cool(repo)
        second = await self.client.row(repo)
        self.assertEqual(first["stable_since"], second["stable_since"])
        result = await self.worker()
        self.assertEqual(result["deployments"][0]["status"], "succeeded", result)
        self.assertTrue(all(process.returncode is not None for process in self.client.processes))
        self.assertFalse((self.client.paths.home / "run/control.sock").exists())
        self.assertEqual((repo.project / "result").read_text(), repo.sha)

    async def test_pinned_checkout_previous_sha_and_no_duplicate_deployment(self):
        repo = Repository(self.root, body='cat message.txt\nprintf "%s" "$PREVIOUS_SHA" > "$PROJECT_DIR/previous"\n')
        original = repo.sha
        await self.enable(repo)
        await self.client.cool(repo)
        await self.worker()
        first = (await self.client.history(repo))[0]
        self.assertEqual(git("rev-parse", "HEAD", cwd=Path(first["release_dir"])), original)
        self.assertEqual(git("rev-parse", "HEAD", cwd=repo.source), original)
        await self.client.call("run-once", repo.project)
        self.assertEqual(len(await self.client.history(repo)), 1)
        repo.publish("second")
        await self.client.cool(repo)
        await self.worker()
        rows = await self.client.history(repo)
        self.assertEqual(rows[0]["status"], "succeeded")
        self.assertEqual(rows[0]["sha"], repo.sha)
        self.assertEqual((repo.project / "previous").read_text(), original)

    async def test_failure_requires_explicit_retry(self):
        repo = Repository(self.root, body="echo failure\nexit 7\n")
        await self.enable(repo)
        await self.client.cool(repo)
        await self.worker()
        for _ in range(3):
            await self.client.call("run-once", repo.project)
        self.assertEqual(len(await self.client.history(repo)), 1)
        await self.manual(repo)
        result = await self.worker()
        self.assertEqual(result["deployments"][0]["status"], "failed")
        self.assertIn("7", result["deployments"][0]["error"])
        self.assertEqual(len(await self.client.history(repo)), 2)

    async def test_config_change_can_redeploy_same_sha(self):
        repo = Repository(self.root)
        await self.enable(repo)
        await self.client.cool(repo)
        await self.worker()
        repo.configure('echo new-script > "$PROJECT_DIR/result"\n')
        await self.client.cool(repo)
        await self.worker()
        rows = await self.client.history(repo)
        self.assertEqual(rows[0]["sha"], rows[1]["sha"])
        self.assertNotEqual(rows[0]["fingerprint"], rows[1]["fingerprint"])
        self.assertEqual(rows[0]["status"], "succeeded")

    async def test_queued_manual_job_revalidates_remote_and_config(self):
        for change in ("remote", "config"):
            with self.subTest(change=change):
                repo = Repository(self.root, change)
                await self.manual(repo)
                if change == "remote":
                    repo.publish("new head")
                else:
                    repo.configure('echo changed > "$PROJECT_DIR/result"\n')
                rows = (await self.worker())["deployments"]
                self.assertEqual(rows[0]["status"], "cancelled")
                self.assertFalse((repo.project / "result").exists())

    async def test_checks_continue_during_deployment_and_worker_is_exclusive(self):
        slow = Repository(self.root, "slow", body='echo yes > "$PROJECT_DIR/started"\nsleep 3\n')
        other = Repository(self.root, "other")
        await self.manual(slow)
        process = await self.client.spawn("worker")
        await self.started(slow.project / "started")
        await self.enable(other)
        await self.client.cool(other)
        self.assertEqual((await self.client.row(slow))["phase"], "deploying")
        self.assertFalse((await self.client.row(slow))["needs_review"])
        self.assertTrue((await self.worker())["busy"])
        self.assertFalse((other.project / "result").exists())
        await self.client.result(process)
        # Refresh the persisted observation before claiming the next snapshot.
        await self.client.cool(other)
        await self.worker()
        first = (await self.client.history(slow))[0]
        second = (await self.client.history(other))[0]
        self.assertEqual(second["status"], "succeeded")
        self.assertGreaterEqual(second["started_at"], first["finished_at"])

    async def test_pause_cancels_queue_but_running_script_finishes(self):
        repo = Repository(self.root, body='echo yes > "$PROJECT_DIR/started"\nsleep 0.8\necho done > "$PROJECT_DIR/result"\n')
        await self.enable(repo)
        await self.client.cool(repo)
        process = await self.client.spawn("worker")
        await self.started(repo.project / "started")
        await self.client.call("pause", repo.project)
        await self.client.result(process)
        self.assertEqual((await self.client.history(repo))[0]["status"], "succeeded")
        self.assertEqual((await self.client.row(repo))["phase"], "paused")
        await self.manual(repo)
        await self.client.call("pause", repo.project)
        await self.worker()
        self.assertEqual((await self.client.history(repo))[0]["status"], "cancelled")

    async def test_network_error_and_observation_gap_reset_window(self):
        repo = Repository(self.root, cooldown=0.04)
        await self.enable(repo)
        await self.client.call("tick", repo.project)
        first = await self.client.row(repo)
        moved = repo.remote.with_name("unavailable.git")
        repo.remote.rename(moved)
        result = await self.client.call("tick", repo.project)
        self.assertIn("error", result[0])
        self.assertIsNone((await self.client.row(repo))["stable_since"])
        moved.rename(repo.remote)
        await self.client.call("tick", repo.project)
        second = await self.client.row(repo)
        self.assertGreater(second["stable_since"], first["stable_since"])
        await asyncio.sleep(1.6)
        await self.client.call("tick", repo.project)
        third = await self.client.row(repo)
        self.assertGreater(third["stable_since"], second["stable_since"])
        self.assertEqual(await self.client.history(repo), [])

    async def test_timeout_kills_script_children(self):
        repo = Repository(self.root, timeout=0.025,
                          body='sleep 60 &\necho "$!" > "$PROJECT_DIR/child.pid"\nwait\n')
        await self.manual(repo)
        result = await self.worker()
        self.assertEqual(result["deployments"][0]["status"], "failed")
        self.assertIn("超时", result["deployments"][0]["error"])
        self.assert_child_dead(repo)

    async def test_interrupted_worker_requires_manual_resolution(self):
        repo = Repository(self.root, body='sleep 60 &\necho "$!" > "$PROJECT_DIR/child.pid"\nwait\n')
        await self.manual(repo)
        process = await self.client.spawn("worker")
        await self.started(repo.project / "child.pid")
        process.terminate()
        await self.client.result(process, check=False)
        self.assert_child_dead(repo)
        self.assertEqual((await self.client.history(repo))[0]["status"], "unknown")
        with self.assertRaises(AutoCDError):
            await self.manual(repo)
        await self.client.call("resolve", repo.project, "--outcome", "failed")
        self.assertFalse((await self.client.row(repo))["needs_review"])

    async def test_crash_lock_survives_parent_and_recovery_waits_for_children(self):
        repo = Repository(self.root, body='echo "$$" > "$PROJECT_DIR/bash.pid"\nsleep 60\n')
        await self.manual(repo)
        process = await self.client.spawn("worker")
        await self.started(repo.project / "bash.pid")
        group = int((repo.project / "bash.pid").read_text())
        process.kill()
        await process.wait()
        try:
            self.assertTrue((await self.worker())["busy"])
            self.assertEqual((await self.client.row(repo))["phase"], "deploying")
        finally:
            os.killpg(group, signal.SIGKILL)
        await asyncio.sleep(0.1)
        self.assertEqual((await self.client.row(repo))["phase"], "unknown")

    async def test_script_snapshot_survives_configuration_edit(self):
        repo = Repository(self.root, body='echo yes > "$PROJECT_DIR/started"\nsleep 0.5\necho original > "$PROJECT_DIR/result"\n')
        await self.manual(repo)
        process = await self.client.spawn("worker")
        await self.started(repo.project / "started")
        repo.configure('echo replacement > "$PROJECT_DIR/result"\n')
        await self.client.result(process)
        self.assertEqual((repo.project / "result").read_text(), "original\n")

    async def test_project_lock_works_across_instances(self):
        repo = Repository(self.root, body='echo yes > "$PROJECT_DIR/started"\nsleep 1\n')
        await self.manual(repo)
        process = await self.client.spawn("worker")
        await self.started(repo.project / "started")
        other_root = self.root / "other-instance"
        other_root.mkdir()
        other = OneShot(other_root)
        try:
            await other.call("deploy", repo.project, "--queue-only")
            result = await other.call("worker", check=False)
            self.assertEqual(result["deployments"][0]["status"], "failed")
            self.assertIn("另一个 AutoCD", result["deployments"][0]["error"])
        finally:
            await other.close()
        await self.client.result(process)

    async def test_concurrent_manual_requests_create_one_job(self):
        repo = Repository(self.root)
        requests = await asyncio.gather(self.manual(repo), self.manual(repo), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, dict) for result in requests), 1)
        self.assertEqual(len(await self.client.history(repo)), 1)

    def assert_child_dead(self, repo):
        child = (repo.project / "child.pid").read_text().strip()
        proc = Path(f"/proc/{child}/stat")
        if proc.exists():
            self.assertIn(proc.read_text().split()[2], {"Z", "X"})
