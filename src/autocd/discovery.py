from pathlib import Path

from .config import AutoCDError, FILENAME, read
from .paths import project_id


def discover(root):
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise AutoCDError(f"扫描目录不存在：{root}")
    found = {}
    try:
        candidates = [root, *sorted(root.iterdir())]
        for candidate in candidates:
            if not candidate.is_dir():
                continue
            candidate = candidate.resolve()
            if not (candidate / FILENAME).exists() or candidate in found:
                continue
            row = dict(id=project_id(candidate), path=str(candidate), error=None)
            try:
                config = read(candidate)
                row.update(branch=config.branch, interval=config.interval, cooldown=config.cooldown)
            except AutoCDError as exc:
                row["error"] = str(exc)
            found[candidate] = row
    except OSError as exc:
        raise AutoCDError(f"无法扫描目录：{root}") from exc
    return list(found.values())
