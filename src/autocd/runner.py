"""Finite invocations: observe once, or drain a snapshot of queued deployments."""

import asyncio
from pathlib import Path
import signal
import time

from .config import AutoCDError, read
from .executor import deploy
from .git import remote_sha
from .locks import file_lock
from .process import check_bash
from .scheduler import boot_id, clock, from_project, ready
from .service import installed
from .state import State


def recover_if_idle(state, paths):
    with file_lock(paths.worker_lock) as lock:
        if lock is not None:
            state.recover()
            return True
    return False


async def set_enabled(paths, project, enabled):
    project = Path(project).expanduser().resolve()
    with State.open(paths) as state:
        recover_if_idle(state, paths)
        if enabled:
            await check_bash(read(project).script, state.lock_fds)
        row = state.register(project)
        with state.transaction():
            row = state.project(row["id"])
            if enabled and row["needs_review"]:
                raise AutoCDError("此项目有中断任务，先核实并使用 resolve 标记结果。")
            state.reset_window(row["id"], include_manual=True)
            state.update(row["id"], enabled=int(enabled))
            state.observation(row["id"], "monitoring", last_error=None)
        return state.project(row["id"])


def record_observation(state, row, config, sha):
    with state.transaction():
        current = state.project(row["id"])
        if current["revision"] != row["revision"] or not current["enabled"] or current["needs_review"]:
            return {"project_id": row["id"], "skipped": "changed_during_check"}
        window = from_project(current)
        previous_start = window.since
        is_ready = window.observe(sha, config.fingerprint, clock(), time.time(),
                                  config.interval, config.cooldown)
        if previous_start != window.since:
            state.reset_window(row["id"])
        state.observation(row["id"], "cooling", fingerprint=config.fingerprint, candidate_sha=sha,
                          stable_since=window.since_utc, last_observed_at=time.time(), boot_id=boot_id(),
                          stable_monotonic=window.since, observed_monotonic=window.observed, last_error=None)
        result = dict(project_id=row["id"], sha=sha, job_id=None)
        if (current["deployed_sha"], current["deployed_fingerprint"]) == (sha, config.fingerprint):
            state.observation(row["id"], "current", last_error=None)
        elif (current["blocked_sha"], current["blocked_fingerprint"]) == (sha, config.fingerprint):
            state.observation(row["id"], "failed", last_error="此版本部署失败；等待新版本或手动重试。")
        elif is_ready:
            result["job_id"] = state.enqueue(row["id"], sha, config.fingerprint)
        return result


async def observe(paths, *, path=None, ident=None, scheduled=False):
    if scheduled:
        manifest = installed(paths)
        if not manifest or not manifest["active"]:
            return {"skipped": "schedule_stopped"}
    with State.open(paths) as state:
        recover_if_idle(state, paths)
        if path is not None:
            row = state.register(Path(path).expanduser().resolve())
        else:
            row = state.project(ident)
        if not row["enabled"] or row["needs_review"]:
            return {"project_id": row["id"], "skipped": "paused_or_needs_review"}
        lock_path = paths.home / "run" / f"check-{row['id']}.lock"
        with file_lock(lock_path) as lock:
            if lock is None:
                return {"project_id": row["id"], "skipped": "check_running"}
            inherited = (lock, *state.lock_fds)
            try:
                config = read(row["path"])
                if config.fingerprint != row["fingerprint"]:
                    await check_bash(config.script, inherited)
                sha = await remote_sha(config, row["path"], inherited)
                if read(row["path"]).fingerprint != config.fingerprint:
                    raise AutoCDError("观察期间配置发生变化，下次重新观察。")
                return record_observation(state, row, config, sha)
            except (AutoCDError, OSError, asyncio.TimeoutError) as exc:
                message = str(exc) if isinstance(exc, AutoCDError) else "检查失败或超时，请检查配置和目录权限。"
                with state.transaction():
                    if state.project(row["id"])["revision"] == row["revision"]:
                        state.reset_window(row["id"], message)
                return {"project_id": row["id"], "error": message}


async def tick(paths, path=None):
    if path:
        return [await observe(paths, path=path)]
    with State.open(paths) as state:
        ids = [row["id"] for row in state.all() if row["enabled"]]
    # Checks are independent; one slow remote never holds a database write transaction.
    return await asyncio.gather(*(observe(paths, ident=ident) for ident in ids))


async def enqueue_manual(paths, path):
    path = Path(path).expanduser().resolve()
    with State.open(paths) as state:
        recover_if_idle(state, paths)
        row = state.register(path)
        if row["needs_review"]:
            raise AutoCDError("先核实中断任务并使用 resolve 标记结果。")
        config = read(path)
        await check_bash(config.script, state.lock_fds)
        sha = await remote_sha(config, path, state.lock_fds)
        if read(path).fingerprint != config.fingerprint:
            raise AutoCDError("查询期间配置已修改；请重新发起部署。")
        with state.transaction():
            if state.project(row["id"])["revision"] != row["revision"]:
                raise AutoCDError("查询期间监视设置发生变化；请重新发起部署。")
            job_id = state.enqueue(row["id"], sha, config.fingerprint, True)
            if job_id is None:
                raise AutoCDError("此项目已有排队或执行中的任务。")
        return {"job_id": job_id, "sha": sha}


def still_ready(state, job):
    project = state.project(job["project_id"])
    config = read(project["path"])
    if project["needs_review"] or config.fingerprint != job["fingerprint"]:
        return False
    return True if job["manual"] else ready(project, config, job)


async def worker(paths, scheduled=False):
    with State.open(paths) as state, file_lock(paths.worker_lock) as lock:
        if lock is None:
            return {"busy": True, "deployments": []}
        state.recover()
        # Bound this invocation to a snapshot: new jobs can wake the next invocation.
        pending = state.queued()
        completed = []
        inherited = (lock, *state.lock_fds)
        for job_id in pending:
            if scheduled:
                manifest = installed(paths)
                if not manifest or not manifest["active"]:
                    break
            job = state.job(job_id)
            if job["status"] != "queued":
                continue
            try:
                if not still_ready(state, job):
                    state.finish(job_id, "cancelled", "排队任务的配置或观察窗口已失效。")
                else:
                    await deploy(state, paths, job_id, inherited, lambda: still_ready(state, job))
            except AutoCDError as exc:
                if state.job(job_id)["status"] in {"queued", "deploying"}:
                    state.finish(job_id, "cancelled", str(exc))
            except Exception:
                if state.job(job_id)["status"] in {"queued", "deploying"}:
                    state.finish(job_id, "unknown", "执行器发生异常；请人工核实任务结果。")
            completed.append(state.job(job_id))
        return {"busy": False, "deployments": completed}


async def interruptible(coroutine):
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(coroutine)
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    try:
        return await task
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
