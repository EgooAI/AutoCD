"""Durable state shared by independent checks, workers and the interactive CLI."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time

from .config import AutoCDError
from .locks import file_lock
from .paths import project_id


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
 id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL DEFAULT 0,
 fingerprint TEXT, candidate_sha TEXT, stable_since REAL, last_observed_at REAL,
 deployed_sha TEXT, deployed_fingerprint TEXT, blocked_sha TEXT, blocked_fingerprint TEXT,
 phase TEXT NOT NULL DEFAULT 'paused', last_error TEXT, needs_review INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS deployments (
 id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL REFERENCES projects(id),
 sha TEXT NOT NULL, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
 manual INTEGER NOT NULL DEFAULT 0, queued_at REAL NOT NULL, started_at REAL, finished_at REAL,
 release_dir TEXT, log_path TEXT, error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_job ON deployments(project_id)
 WHERE status IN ('queued', 'deploying');
"""


class State:
    @classmethod
    def open(cls, paths):
        paths.prepare()
        return cls(paths.database)

    def __init__(self, path, readonly=False):
        path = Path(path).resolve()
        self.guard = None
        self.db = None
        self.lock_fds = ()
        try:
            if not readonly:
                (path.parent / "run").mkdir(mode=0o700, exist_ok=True)
            guard_path = path.parent / "run/daemon.lock"
            if guard_path.parent.exists():
                self.guard = file_lock(guard_path, shared=True)
                legacy_fd = self.guard.__enter__()
                if legacy_fd is None:
                    raise AutoCDError("旧版 daemon 或其任务仍在运行；请先用旧脚本停止，再迁移状态。")
                self.lock_fds = (legacy_fd,)
            self.db = sqlite3.connect(f"{path.as_uri()}?mode=ro" if readonly else path,
                                      uri=readonly, timeout=10, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            if not readonly:
                self.db.execute("PRAGMA journal_mode=WAL")
            with self.transaction(readonly=readonly):
                version = self.db.execute("PRAGMA user_version").fetchone()[0]
                if version not in ({2} if readonly else {0, 1, 2}):
                    raise AutoCDError("状态数据库版本不兼容；旧状态请先执行 migrate，较新状态请使用匹配版本。")
                if version == 0:
                    for statement in SCHEMA.split(";"):
                        if statement.strip():
                            self.db.execute(statement)
                if version < 2:
                    for name, kind in (("revision", "INTEGER NOT NULL DEFAULT 0"), ("boot_id", "TEXT"),
                                       ("stable_monotonic", "REAL"), ("observed_monotonic", "REAL")):
                        self.db.execute(f"ALTER TABLE projects ADD COLUMN {name} {kind}")
                    self.db.execute("ALTER TABLE deployments ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
                    self.db.execute("UPDATE projects SET candidate_sha=NULL,stable_since=NULL,last_observed_at=NULL,"
                                    "phase=CASE WHEN needs_review=1 THEN 'unknown' WHEN enabled=1 THEN 'monitoring' ELSE 'paused' END")
                    self.db.execute("PRAGMA user_version=2")
        except BaseException:
            self.close()
            raise

    @contextmanager
    def transaction(self, readonly=False):
        nested = self.db.in_transaction
        if not nested:
            self.db.execute("BEGIN" if readonly else "BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            if not nested:
                self.db.rollback()
            raise
        else:
            if not nested:
                self.db.commit()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self.db:
            self.db.close()
            self.db = None
        if self.guard:
            self.guard.__exit__(None, None, None)
            self.guard = None

    def all(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM projects ORDER BY path")]

    def project(self, ident):
        row = self.db.execute("SELECT * FROM projects WHERE id=?", (ident,)).fetchone()
        if not row:
            raise AutoCDError("项目尚未登记，请先扫描或开启监视。")
        return dict(row)

    def register(self, path):
        path = Path(path).resolve()
        ident = project_id(path)
        with self.transaction():
            self.db.execute("INSERT OR IGNORE INTO projects(id,path) VALUES (?,?)", (ident, str(path)))
        return self.project(ident)

    def update(self, ident, **fields):
        # Column names only come from internal callers.
        with self.transaction():
            self.db.execute(f"UPDATE projects SET {','.join(key + '=?' for key in fields)} WHERE id=?",
                            (*fields.values(), ident))

    def observation(self, ident, phase, **fields):
        with self.transaction():
            row = self.project(ident)
            if row["phase"] not in {"queued", "deploying"} and not row["needs_review"]:
                fields["phase"] = phase if row["enabled"] else "paused"
            self.update(ident, **fields)

    def history(self, ident=None, limit=20):
        where = "WHERE project_id=?" if ident else ""
        arguments = (ident, limit) if ident else (limit,)
        return [dict(row) for row in self.db.execute(
            f"SELECT * FROM deployments {where} ORDER BY id DESC LIMIT ?", arguments)]

    def job(self, job_id):
        row = self.db.execute("SELECT * FROM deployments WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise AutoCDError("部署记录不存在。")
        return dict(row)

    def queued(self):
        return [row[0] for row in self.db.execute("SELECT id FROM deployments WHERE status='queued' ORDER BY id")]

    def active(self, ident):
        return self.db.execute("SELECT 1 FROM deployments WHERE project_id=? AND status IN ('queued','deploying')",
                               (ident,)).fetchone() is not None

    def enqueue(self, ident, sha, fingerprint, manual=False):
        with self.transaction():
            project = self.project(ident)
            if project["needs_review"]:
                raise AutoCDError("此项目有中断任务，先核实并使用 resolve 标记结果。")
            if self.active(ident):
                return None
            cursor = self.db.execute(
                "INSERT INTO deployments(project_id,sha,fingerprint,status,manual,queued_at,revision) VALUES (?,?,?,'queued',?,?,?)",
                (ident, sha, fingerprint, int(manual), time.time(), project["revision"]))
            self.update(ident, phase="queued", last_error=None)
            return cursor.lastrowid

    def start(self, job_id, log_path):
        with self.transaction():
            job = self.job(job_id)
            if job["status"] != "queued":
                return False
            if self.project(job["project_id"])["needs_review"]:
                self.finish(job_id, "cancelled", "此项目有待核实的中断任务。")
                return False
            self.db.execute("UPDATE deployments SET status='deploying',started_at=?,log_path=? WHERE id=?",
                            (time.time(), str(log_path), job_id))
            self.update(job["project_id"], phase="deploying")
            return True

    def release(self, job_id, release):
        with self.transaction():
            self.db.execute("UPDATE deployments SET release_dir=? WHERE id=?", (str(release), job_id))

    def finish(self, job_id, status, error=None):
        with self.transaction():
            job = self.job(job_id)
            project = self.project(job["project_id"])
            fields = dict(last_error=error)
            if status == "succeeded":
                fields.update(deployed_sha=job["sha"], deployed_fingerprint=job["fingerprint"],
                              blocked_sha=None, blocked_fingerprint=None)
            elif status in {"failed", "unknown"}:
                fields.update(blocked_sha=job["sha"], blocked_fingerprint=job["fingerprint"])
            phase = {"succeeded": "current", "failed": "failed", "unknown": "unknown",
                     "cancelled": "monitoring"}[status]
            if status == "succeeded" and project["candidate_sha"] not in {None, job["sha"]}:
                phase = "cooling"
            if status == "unknown":
                fields["needs_review"] = 1
            fields["phase"] = phase if project["enabled"] or status == "unknown" else "paused"
            self.db.execute("UPDATE deployments SET status=?,finished_at=?,error=? WHERE id=?",
                            (status, time.time(), error, job_id))
            self.update(job["project_id"], **fields)

    def reset_window(self, ident, error=None, *, include_manual=False):
        with self.transaction():
            row = self.project(ident)
            query = "SELECT id FROM deployments WHERE project_id=? AND status='queued'"
            if not include_manual:
                query += " AND manual=0"
            for job in list(self.db.execute(query, (ident,))):
                self.finish(job[0], "cancelled", error or "观察窗口失效，重新观察。")
            self.observation(ident, "error" if error else "monitoring",
                             candidate_sha=None, stable_since=None, last_observed_at=None,
                             stable_monotonic=None, observed_monotonic=None, boot_id=None,
                             revision=row["revision"] + 1, last_error=error)

    def recover(self):
        """Caller MUST hold the global worker lock; never reset healthy windows here."""
        with self.transaction():
            for row in list(self.db.execute("SELECT id FROM deployments WHERE status='deploying'")):
                self.finish(row[0], "unknown", "执行进程已退出且未记录结果；请检查实际服务后核实。")
                self.reset_window(self.job(row[0])["project_id"])

    def resolve(self, ident, outcome):
        with self.transaction():
            row = self.project(ident)
            if not row["needs_review"]:
                raise AutoCDError("此项目没有待核实的中断任务。")
            job = self.db.execute("SELECT id FROM deployments WHERE project_id=? AND status='unknown' ORDER BY id DESC LIMIT 1",
                                  (ident,)).fetchone()
            if outcome not in {"succeeded", "failed"} or not job:
                raise AutoCDError("请将已核实的任务标记为 succeeded 或 failed。")
            self.finish(job[0], outcome, "操作员已核实中断任务的结果。")
            self.update(ident, needs_review=0)
            self.reset_window(ident)
