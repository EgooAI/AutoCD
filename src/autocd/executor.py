"""Pinned checkouts and an immutable script snapshot per deployment."""

import asyncio
import fcntl
import os

from .config import AutoCDError, atomic_write, read
from .git import CandidateChanged, checkout, remote_sha
from .process import check_bash, run


async def deploy(state, paths, job_id, lock_fd=None, still_ready=lambda: True):
    project = state.project(state.job(job_id)["project_id"])
    try:
        project_lock = os.open(project["path"], os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        state.finish(job_id, "failed", "无法访问项目目录。")
        return
    try:
        try:
            # Linux supports flock on a directory: coordinate across --home instances
            # without creating files in the original project checkout.
            fcntl.flock(project_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            state.finish(job_id, "failed", "另一个 AutoCD 实例正在部署此项目；完成后可手动重试。")
            return
        inherited = (project_lock,) if lock_fd is None else ((*lock_fd, project_lock) if isinstance(lock_fd, tuple) else (lock_fd, project_lock))
        await _deploy(state, paths, job_id, inherited, still_ready)
    finally:
        os.close(project_lock)


async def _deploy(state, paths, job_id, lock_fd, still_ready):
    job = state.job(job_id)
    project = state.project(job["project_id"])
    script_started = False
    log_path = paths.home / "logs" / f"{job_id}.log"
    if not state.start(job_id, log_path):
        return
    try:
        config = read(project["path"])
        if config.fingerprint != job["fingerprint"]:
            raise CandidateChanged("排队期间配置发生变化，重新开始观察。")
        await check_bash(config.script, lock_fd)

        async def execute():
            nonlocal script_started
            if await remote_sha(config, project["path"], lock_fd) != job["sha"]:
                raise CandidateChanged("排队期间远程分支发生变化，重新开始观察。")
            release = await checkout(config, project["path"], job["sha"], paths,
                                     project["id"], job_id, lock_fd)
            state.release(job_id, release)
            snapshot = paths.home / "logs" / f"{job_id}.sh"
            atomic_write(snapshot, config.script)
            if (read(project["path"]).fingerprint != config.fingerprint
                    or await remote_sha(config, project["path"], lock_fd) != job["sha"]):
                raise CandidateChanged("准备期间配置或远程分支发生变化，重新开始观察。")
            current = state.project(project["id"])
            if not still_ready() or (not job["manual"] and not current["enabled"]):
                raise CandidateChanged("监视已暂停或观察窗口已失效，本次任务取消。")
            env = os.environ.copy()
            # Bash startup variables must not execute code before the saved script.
            env.pop("BASH_ENV", None)
            env.pop("ENV", None)
            env.update(DEPLOY_SHA=job["sha"], PREVIOUS_SHA=project["deployed_sha"] or "",
                       RELEASE_DIR=str(release), PROJECT_DIR=project["path"],
                       AUTOCD_DEPLOYMENT_ID=str(job_id))
            with log_path.open("ab", buffering=0) as log:
                os.chmod(log_path, 0o600)
                log.write(f"AutoCD deployment {job_id} / {job['sha']}\n".encode())
                script_started = True
                code, _ = await run(["bash", "--noprofile", "--norc", snapshot], cwd=release,
                                    env=env, output=log, timeout=config.timeout, lock_fd=lock_fd)
            if code:
                raise AutoCDError(f"部署命令退出码为 {code}；查看本次部署日志。")

        # Includes remote verification, fetch, checkout and the full Bash process group.
        await asyncio.wait_for(execute(), timeout=config.timeout)
        state.finish(job_id, "succeeded")
    except CandidateChanged as exc:
        state.finish(job_id, "cancelled", str(exc))
    except asyncio.TimeoutError:
        state.finish(job_id, "failed", "部署超时，已终止命令进程组；请核实服务状态后重试。")
    except asyncio.CancelledError:
        state.finish(job_id, "unknown" if script_started else "cancelled", "部署进程停止，任务已中断。")
        raise
    except AutoCDError as exc:
        state.finish(job_id, "failed", str(exc))
    except Exception:
        state.finish(job_id, "unknown" if script_started else "failed", "执行器发生异常，请检查任务日志并核实服务状态。")
        raise
