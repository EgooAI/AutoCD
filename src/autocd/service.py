"""Install timers and finite services; never run a permanent AutoCD process."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from . import __version__, ui
from .config import AutoCDError, atomic_write, read
from .locks import file_lock
from .runner import open_state
from .units import MARKER, environment_path, project_units, worker_units


def control(*args, check=True):
    command = ["systemctl", *(["--user"] if os.getuid() != 0 else []), *args]
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AutoCDError("无法调用 systemd；可使用 run-once 手动检查并部署。") from exc
    if check and result.returncode:
        raise AutoCDError("systemd 操作失败；检查用户服务管理器（用户服务需 linger），或使用 run-once。")
    return result


def unit_dir():
    return (Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user"
            if os.getuid() != 0 else Path("/etc/systemd/system"))


def installed(paths):
    if not paths.installation.exists():
        return None
    try:
        value = json.loads(paths.installation.read_text())
        prefix = "autocd-" + hashlib.sha256(str(paths.home).encode()).hexdigest()[:12]
        if (not isinstance(value, dict) or value["schema"] != 1 or value["prefix"] != prefix
                or type(value["active"]) is not bool
                or any(not isinstance(value[key], str) for key in ("program", "python", "path_env"))):
            raise ValueError
        return value
    except (ValueError, KeyError, TypeError) as exc:
        raise AutoCDError("定时任务安装记录无效，请检查 schedule.json。") from exc


def write_manifest(paths, value):
    atomic_write(paths.installation, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def ensure_owned(path):
    if path.exists() and not path.read_text().startswith(MARKER):
        raise AutoCDError(f"同名 systemd 文件不属于 AutoCD，拒绝覆盖：{path}")


def write_units(files):
    directory = unit_dir()
    directory.mkdir(parents=True, exist_ok=True)
    changed = False
    for name, content in files.items():
        path = directory / name
        ensure_owned(path)
        if not path.exists() or path.read_text() != content:
            atomic_write(path, content, 0o644)
            changed = True
    return changed


def configured_units(manifest, paths, rows):
    files = worker_units(manifest, paths)
    for row in rows:
        if row["enabled"]:
            try:
                interval = read(row["path"]).interval
            except AutoCDError:
                interval = 30
            files.update(project_units(manifest, paths, row, interval))
    return files


def sync_project(paths, row):
    """Idempotent; also called by scheduled checks after external config edits."""
    paths.prepare()
    with file_lock(paths.home / "run/schedule.lock", blocking=True):
        manifest = installed(paths)
        if manifest is None:
            return
        with open_state(paths) as state:
            row = state.register(row["path"])
        name = f"{manifest['prefix']}-{row['id']}"
        timer = name + ".timer"
        if row["enabled"]:
            try:
                interval = read(row["path"]).interval
            except AutoCDError:
                # Keep trying a broken/missing config so fixing it resumes observation.
                interval = 30
            changed = write_units(project_units(manifest, paths, row, interval))
            if changed:
                control("daemon-reload")
            if manifest["active"]:
                control("enable", "--now", timer)
                if changed:
                    control("restart", timer)
        elif (unit_dir() / timer).exists():
            control("disable", "--now", timer)
            for suffix in (".timer", ".service"):
                path = unit_dir() / (name + suffix)
                ensure_owned(path)
                path.unlink(missing_ok=True)
            control("daemon-reload")


def kick(paths):
    manifest = installed(paths)
    if manifest and manifest["active"]:
        control("start", "--no-block", manifest["prefix"] + "-worker.service")
        return True
    return False


def overview(paths):
    manifest = installed(paths)
    if manifest is None:
        return {"installed": False, "active": False, "mode": "manual"}
    try:
        result = control("list-units", "--type=timer", "--state=active", "--no-legend", "--plain",
                         manifest["prefix"] + "-*.timer")
        timers = [line.split()[0] for line in result.stdout.splitlines() if line.strip()]
        active = manifest["prefix"] + "-worker.timer" in timers
        error = None
    except AutoCDError as exc:
        active, timers, error = False, [], str(exc)
    return {"installed": True, "active": manifest["active"] and active, "mode": "systemd",
            "program": manifest["program"], "prefix": manifest["prefix"], "timers": timers, "error": error}


def install(paths):
    artifact = getattr(sys, "_autocd_artifact", None)
    if not artifact:
        raise AutoCDError("安装定时任务请使用构建产物：python3 dist/autocd.py schedule install。")
    control("show-environment")
    paths.prepare()
    with file_lock(paths.home / "run/schedule.lock", blocking=True):
        with open_state(paths) as state:
            rows = state.all()
        existing = installed(paths)
        source = Path(artifact).read_bytes()
        digest = hashlib.sha256(source).hexdigest()[:12]
        target = paths.home / "programs" / f"{__version__}-{digest}" / "autocd.py"
        if existing and existing["program"] != str(target):
            with file_lock(paths.worker_lock) as lock:
                if lock is None:
                    raise AutoCDError("有部署仍在运行；等待结束后再更新定时任务的程序副本。")
        legacy = unit_dir() / "autocd.service"
        if legacy.exists() and "Description=AutoCD Git deployment watcher" in legacy.read_text():
            raise AutoCDError("检测到旧 daemon 服务；先停止并卸载旧 autocd.service，再安装定时任务。")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists() and target.read_bytes() != source:
            raise AutoCDError("程序副本内容不匹配，拒绝覆盖。")
        if not target.exists():
            atomic_write(target, source.decode("utf-8"), 0o700)
        manifest = dict(schema=1, active=False, program=str(target), python=sys.executable,
                        prefix="autocd-" + hashlib.sha256(str(paths.home).encode()).hexdigest()[:12],
                        path_env=environment_path())
        files = configured_units(manifest, paths, rows)
        write_units(files)
        control("daemon-reload")
        write_manifest(paths, manifest)
        try:
            # Set the switch first, so immediately fired checks see an active installation.
            manifest["active"] = True
            write_manifest(paths, manifest)
            control("enable", "--now", *(name for name in files if name.endswith(".timer")))
        except AutoCDError:
            manifest["active"] = False
            write_manifest(paths, manifest)
            control("disable", "--now", *(name for name in files if name.endswith(".timer")), check=False)
            raise
    ui.notice(f"定时任务已安装；空闲时无需 AutoCD 进程。程序副本：{target}", "success")
    if os.getuid() != 0:
        ui.notice("注销后及开机自动执行需要管理员启用：loginctl enable-linger <用户名>。")


def stop(paths):
    manifest = installed(paths)
    if manifest is None:
        raise AutoCDError("尚未安装定时任务。")
    with file_lock(paths.home / "run/schedule.lock", blocking=True):
        manifest = installed(paths)
        manifest["active"] = False
        write_manifest(paths, manifest)
        timers = sorted(path.name for path in unit_dir().glob(manifest["prefix"] + "-*.timer"))
        with open_state(paths) as state:
            for row in state.all():
                state.reset_window(row["id"], include_manual=True)
        if timers:
            control("disable", "--now", *timers)
    ui.notice("定时任务已停止；已开始执行的部署继续完成。", "success")


def start(paths):
    manifest = installed(paths)
    if manifest is None:
        raise AutoCDError("请先执行 schedule install。")
    control("show-environment")
    with file_lock(paths.home / "run/schedule.lock", blocking=True):
        manifest = installed(paths)
        with open_state(paths) as state:
            rows = state.all()
        files = configured_units(manifest, paths, rows)
        if write_units(files):
            control("daemon-reload")
        manifest["active"] = True
        write_manifest(paths, manifest)
        try:
            control("enable", "--now", *(name for name in files if name.endswith(".timer")))
        except AutoCDError:
            manifest["active"] = False
            write_manifest(paths, manifest)
            control("disable", "--now", *(name for name in files if name.endswith(".timer")), check=False)
            raise
    ui.notice("定时任务已开启；各项目按配置的间隔检查。", "success")


def uninstall(paths):
    stop(paths)
    with file_lock(paths.home / "run/schedule.lock", blocking=True), file_lock(paths.worker_lock) as lock:
        if lock is None:
            raise AutoCDError("定时触发已停止；还有部署正在执行，结束后再卸载。")
        manifest = installed(paths)
        for path in unit_dir().glob(manifest["prefix"] + "-*"):
            if path.suffix in {".timer", ".service"}:
                ensure_owned(path)
                path.unlink()
        control("daemon-reload")
        paths.installation.unlink()
    ui.notice("定时任务已卸载；配置、状态、日志和发布目录保留。", "success")


def manage(paths, action):
    if action == "status":
        print(json.dumps(overview(paths), ensure_ascii=False, indent=2))
    else:
        {"install": install, "start": start, "stop": stop, "uninstall": uninstall}[action](paths)
