from pathlib import Path
import sqlite3
import tempfile
import unittest

import support  # noqa: F401 -- install the source path for stdlib unittest discovery
from autocd.config import AutoCDError
from autocd.locks import file_lock
from autocd.state import SCHEMA, State


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="autocd-state-")
        self.root = Path(self.temp.name)
        self.state = State(self.root / "state.sqlite3")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.state.close)
        self.project = self.state.register(self.root / "project")

    def test_recovery_preserves_switch_and_requires_review(self):
        ident = self.project["id"]
        self.state.update(ident, enabled=1, candidate_sha="sha", stable_since=100)
        job = self.state.enqueue(ident, "sha", "fp")
        self.state.start(job, self.root / "log")
        self.state.recover()
        project = self.state.project(ident)
        self.assertTrue(project["enabled"])
        self.assertTrue(project["needs_review"])
        self.assertIsNone(project["stable_since"])
        self.assertEqual(self.state.job(job)["status"], "unknown")
        self.state.resolve(ident, "succeeded")
        self.assertFalse(self.state.project(ident)["needs_review"])
        self.assertEqual(self.state.project(ident)["deployed_sha"], "sha")

    def test_pending_queue_and_healthy_window_survive_process_start(self):
        ident = self.project["id"]
        first = self.state.enqueue(ident, "sha", "fp")
        self.assertIsNone(self.state.enqueue(ident, "sha2", "fp"))
        self.state.update(ident, stable_since=100, stable_monotonic=10, observed_monotonic=20)
        self.state.recover()
        self.assertEqual(self.state.job(first)["status"], "queued")
        self.assertEqual(self.state.project(ident)["stable_monotonic"], 10)
        self.assertIsNone(self.state.enqueue(ident, "sha2", "fp"))

    def test_failure_keeps_last_successful_pointer(self):
        ident = self.project["id"]
        first = self.state.enqueue(ident, "old", "cfg")
        self.state.finish(first, "succeeded")
        second = self.state.enqueue(ident, "new", "cfg")
        self.state.finish(second, "failed", "failure")
        row = self.state.project(ident)
        self.assertEqual(row["deployed_sha"], "old")
        self.assertEqual(row["blocked_sha"], "new")

    def test_newer_schema_is_rejected(self):
        self.state.db.execute("PRAGMA user_version=9")
        with self.assertRaises(AutoCDError):
            State(self.root / "state.sqlite3")

    def test_migration_preserves_history_and_enabled_switch(self):
        path = self.root / "old.sqlite3"
        with sqlite3.connect(path) as legacy:
            legacy.executescript(SCHEMA)
            legacy.execute("PRAGMA user_version=1")
            legacy.execute("INSERT INTO projects(id,path,enabled,deployed_sha,stable_since) VALUES ('old','/test',1,'sha',100)")
            legacy.execute("INSERT INTO deployments(project_id,sha,fingerprint,status,queued_at) VALUES ('old','sha','fp','succeeded',10)")
        with State(path) as migrated:
            row = migrated.project("old")
            self.assertTrue(row["enabled"])
            self.assertEqual(row["deployed_sha"], "sha")
            self.assertIsNone(row["stable_since"])
            self.assertEqual(migrated.history()[0]["status"], "succeeded")
            self.assertEqual(migrated.db.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_live_legacy_daemon_blocks_migration(self):
        self.state.close()
        with file_lock(self.root / "run/daemon.lock") as lock:
            self.assertIsNotNone(lock)
            with self.assertRaises(AutoCDError):
                State(self.root / "state.sqlite3")

    def test_cancelled_job_cannot_be_claimed(self):
        ident = self.project["id"]
        job = self.state.enqueue(ident, "sha", "fp")
        self.state.reset_window(ident, include_manual=True)
        self.assertFalse(self.state.start(job, self.root / "log"))
