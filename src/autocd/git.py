import asyncio
import hashlib
import os
from pathlib import Path
import re

from .config import AutoCDError
from .process import run


def environment():
    env = os.environ.copy()
    # Do not inherit a caller's repository/index into the dedicated deployment checkout.
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        env.pop(key, None)
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=15 -o ConnectionAttempts=1")
    return env


async def git(*arguments, cwd=None, timeout=30, pass_fds=()):
    try:
        code, output = await run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.ext.allow=never", *arguments],
            cwd=cwd, timeout=timeout, env=environment(), pass_fds=pass_fds,
        )
    except asyncio.TimeoutError as exc:
        raise AutoCDError("Git 操作超时；请检查服务用户的网络和认证。") from exc
    if code:
        # Remote messages and argv can contain credentials: never echo either.
        raise AutoCDError("Git 操作失败；请检查地址、分支及服务用户的 Git/SSH 权限。")
    return output.strip()


def repository_url(config, project):
    value = config.repository
    if ":" not in value:
        return str((Path(project) / Path(value).expanduser()).resolve())
    return value


async def remote_sha(config, project, pass_fds=()):
    ref = f"refs/heads/{config.branch}"
    output = await git("ls-remote", "--exit-code", repository_url(config, project), ref,
                       cwd=project, pass_fds=pass_fds)
    matches = [line.split() for line in output.splitlines() if line.split()[-1:] == [ref]]
    if len(matches) != 1 or len(matches[0]) != 2 or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", matches[0][0]):
        raise AutoCDError("远程未返回唯一的分支提交指针。")
    return matches[0][0]


async def checkout(config, project, sha, paths, ident, job_id, pass_fds=()):
    url = repository_url(config, project)
    cache_id = hashlib.sha256(url.encode()).hexdigest()[:20]
    cache = paths.home / "cache" / f"{ident}-{cache_id}.git"
    if not cache.exists():
        await git("init", "--bare", f"--object-format={'sha256' if len(sha) == 64 else 'sha1'}",
                  cache, pass_fds=pass_fds)
    ref = "refs/heads/autocd-candidate"
    await git("--git-dir", cache, "fetch", "--no-tags", "--depth=1", "--force", url,
              f"+refs/heads/{config.branch}:{ref}", timeout=120, pass_fds=pass_fds)
    fetched = await git("--git-dir", cache, "rev-parse", ref, pass_fds=pass_fds)
    if fetched != sha:
        raise CandidateChanged("准备期间远程分支发生变化，重新开始观察。")
    release = paths.home / "releases" / ident / f"{job_id}-{sha[:12]}"
    release.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    await git("clone", "--no-hardlinks", "--no-checkout", "--branch", "autocd-candidate",
              cache, release, timeout=120, pass_fds=pass_fds)
    await git("checkout", "--detach", sha, cwd=release, timeout=120, pass_fds=pass_fds)
    return release


class CandidateChanged(AutoCDError):
    pass
