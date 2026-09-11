"""Own process groups so cancellation also reaches grandchildren."""

import asyncio
import os
import signal

from .config import AutoCDError


def kill_group(pid, sig):
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


async def run(argv, *, timeout=30, cwd=None, env=None, output=None, input_data=None, lock_fd=None):
    inherited = () if lock_fd is None else ((lock_fd,) if isinstance(lock_fd, int) else tuple(lock_fd))
    try:
        process = await asyncio.create_subprocess_exec(
            *map(str, argv), cwd=cwd, env=env, start_new_session=True,
            pass_fds=inherited,
            stdin=asyncio.subprocess.PIPE if input_data is not None else asyncio.subprocess.DEVNULL,
            stdout=output if output is not None else asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT if output is not None else asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise AutoCDError(f"缺少运行程序：{argv[0]}") from exc
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(input_data), timeout)
        return process.returncode, (stdout or b"").decode("utf-8", errors="replace")
    except (asyncio.TimeoutError, asyncio.CancelledError):
        kill_group(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), 2)
        except asyncio.TimeoutError:
            pass
        kill_group(process.pid, signal.SIGKILL)
        await process.wait()
        raise
    finally:
        # Deployment scripts must use service managers for long-lived services.
        kill_group(process.pid, signal.SIGKILL)


async def check_bash(script, lock_fd=None):
    environment = os.environ.copy()
    environment.pop("BASH_ENV", None)
    environment.pop("ENV", None)
    code, _ = await run(["bash", "--noprofile", "--norc", "-n"],
                        input_data=script.encode(), env=environment, lock_fd=lock_fd)
    if code:
        raise AutoCDError("部署脚本未通过 bash -n 语法检查。")
