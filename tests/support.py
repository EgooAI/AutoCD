import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from autocd.config import AutoCDError, atomic_write, render  # noqa: E402
from autocd.paths import Paths  # noqa: E402

BUILT = False


def git(*args, cwd=None):
    result = subprocess.run(["git", *map(str, args)], cwd=cwd, check=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return result.stdout.strip()


class Repository:
    def __init__(self, root, name="project", body=None, interval=0.003, cooldown=0.012, timeout=0.2):
        self.root = root / name
        self.root.mkdir()
        self.remote = self.root / "remote.git"
        self.source = self.root / "source"
        self.project = self.root / "project"
        self.project.mkdir()
        git("init", "--bare", "--initial-branch=main", self.remote)
        git("init", "--initial-branch=main", self.source)
        git("remote", "add", "origin", self.remote, cwd=self.source)
        self.publish("first")
        self.configure(body or 'set -euo pipefail\ncat message.txt\nprintf "%s" "$DEPLOY_SHA" > "$PROJECT_DIR/result"\n',
                       interval, cooldown, timeout)

    def publish(self, message):
        (self.source / "message.txt").write_text(message)
        git("add", ".", cwd=self.source)
        git("-c", "user.name=AutoCD Test", "-c", "user.email=test@localhost", "commit", "-m", message, cwd=self.source)
        git("push", "origin", "main", cwd=self.source)
        self.sha = git("rev-parse", "HEAD", cwd=self.source)
        return self.sha

    def configure(self, body, interval=0.003, cooldown=0.012, timeout=0.2):
        self.script = render(str(self.remote), "main", interval, cooldown, timeout, body)
        atomic_write(self.project / ".autocd.sh", self.script)


async def eventually(function, predicate=lambda result: bool(result), timeout=12):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = await function()
        if predicate(last):
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"Condition did not become true; last result: {last!r}")


class OneShot:
    def __init__(self, root):
        global BUILT
        self.root = root
        self.paths = Paths(root / "state")
        artifact = ROOT / "dist/autocd.py"
        if not BUILT:
            subprocess.run([sys.executable, str(ROOT / "tools/build_single.py")], check=True,
                           stdout=subprocess.DEVNULL)
            BUILT = True
        self.artifact = root / "standalone.py"
        shutil.copyfile(artifact, self.artifact)
        self.processes = []

    async def spawn(self, *args):
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", str(self.artifact), "--home", str(self.paths.home), *map(str, args),
            cwd=self.root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self.processes.append(process)
        return process

    async def result(self, process, *, check=True):
        out, err = await asyncio.wait_for(process.communicate(), 20)
        if check and process.returncode:
            raise AutoCDError(f"Exit {process.returncode}: {err.decode()} {out.decode()}")
        return json.loads(out) if out.strip() else None

    async def call(self, *args, check=True):
        return await self.result(await self.spawn(*args), check=check)

    async def history(self, repo):
        return await self.call("history", repo.project, "--json")

    async def row(self, repo):
        from autocd.paths import project_id
        return next(row for row in (await self.call("status", "--json"))["projects"]
                    if row["id"] == project_id(repo.project))

    async def cool(self, repo):
        async def step():
            await self.call("tick", repo.project)
            return await self.history(repo)
        return await eventually(step, lambda rows: bool(rows) and rows[0]["status"] == "queued")

    async def close(self):
        for process in self.processes:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.communicate(), 5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.communicate()
