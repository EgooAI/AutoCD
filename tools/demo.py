#!/usr/bin/env python3
"""Create a local Git remote and a harmless project for manual testing."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from autocd.config import atomic_write, render  # noqa: E402


def git(*args, cwd=None):
    return subprocess.run(["git", *map(str, args)], cwd=cwd, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()


def setup(directory):
    if directory.exists():
        raise SystemExit(f"Already exists; choose a new --directory: {directory}")
    directory.mkdir(parents=True)
    remote = directory / "remote.git"
    source = directory / "source"
    project = directory / "projects/Demo"
    project.mkdir(parents=True)
    git("init", "--bare", "--initial-branch=main", remote)
    git("init", "--initial-branch=main", source)
    (source / "message.txt").write_text("Hello from AutoCD v1\n")
    git("add", "message.txt", cwd=source)
    git("-c", "user.name=AutoCD Demo", "-c", "user.email=demo@localhost", "commit", "-m", "demo: initial", cwd=source)
    git("remote", "add", "origin", remote, cwd=source)
    git("push", "origin", "main", cwd=source)
    body = '''
set -euo pipefail
printf 'Release: %s\\nPrevious: %s\\n' "$DEPLOY_SHA" "$PREVIOUS_SHA"
cat message.txt
sleep 2
# Only write a result file inside this demo project. No services are changed.
printf '%s\\n' "$DEPLOY_SHA" > "$PROJECT_DIR/deployed.txt"
printf 'Demo deployment complete.\\n'
'''
    atomic_write(project / ".autocd.sh", render(str(remote), "main", 0.05, 0.15, 1, body))
    print(f"Demo project: {project}\nPolling: 3s · Cooling: 9s\nState: {directory / 'state'}")
    print(f"Publish another demo commit: python3 tools/demo.py update --directory {directory}")


def update(directory):
    source = directory / "source"
    if not (source / ".git").exists():
        raise SystemExit("Demo does not exist; run setup first.")
    count = int(git("rev-list", "--count", "HEAD", cwd=source)) + 1
    (source / "message.txt").write_text(f"Hello from AutoCD v{count}\n")
    git("add", "message.txt", cwd=source)
    git("-c", "user.name=AutoCD Demo", "-c", "user.email=demo@localhost", "commit", "-m", f"demo: version {count}", cwd=source)
    git("push", "origin", "main", cwd=source)
    print(git("rev-parse", "HEAD", cwd=source))


def verify(directory):
    artifact = ROOT / "dist/autocd.py"
    project = directory / "projects/Demo"
    if not (project / ".autocd.sh").exists():
        raise SystemExit("Run setup first.")
    command = [sys.executable, str(artifact), "--home", str(directory / "state")]
    subprocess.run([*command, "enable", str(project)], check=True, stdout=subprocess.DEVNULL)
    expected = git("rev-parse", "HEAD", cwd=directory / "source")
    for attempt in range(5):
        result = subprocess.run([*command, "run-once", str(project)], check=True, capture_output=True, text=True)
        output = json.loads(result.stdout)
        print(f"Check {attempt + 1}/5 · subprocess exited · deployments: {len(output['deployments'])}", flush=True)
        if attempt < 4:
            time.sleep(3)
    result = project / "deployed.txt"
    if not result.exists() or result.read_text().strip() != expected:
        raise SystemExit("Demo did not deploy the expected commit; inspect history and logs.")
    print(f"Verified {expected}. All test invocations have exited.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["setup", "update", "verify"])
    parser.add_argument("--directory", type=Path, default=ROOT / ".sandbox/demo")
    args = parser.parse_args()
    globals()[args.action](args.directory.expanduser().resolve())
