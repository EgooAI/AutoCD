"""Shared operations for the interactive menu and CLI."""

import asyncio
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time

from .config import AutoCDError, FILENAME, atomic_write, parse, read, render, validate_repository
from .discovery import discover
from .git import git, remote_sha
from .paths import project_id
from .process import check_bash
from .runner import enqueue_manual, interruptible, recover_if_idle, set_enabled, tick, worker
from .scheduler import clock, from_project
from .service import installed, kick, overview, sync_project
from .state import State
from . import ui


def call(paths, action, **payload):
    if action in {"enable", "pause"}:
        row = asyncio.run(interruptible(set_enabled(paths, payload["path"], action == "enable")))
        sync_project(paths, row)
        return row
    if action in {"tick", "run-once"}:
        results = asyncio.run(interruptible(tick(paths, payload.get("path"))))
        for item in results:
            with State.open(paths) as state:
                row = state.project(item["project_id"])
            sync_project(paths, row)
        if action == "run-once":
            return {"checks": results, **asyncio.run(interruptible(worker(paths)))}
        wake_worker(paths)
        return results
    if action == "deploy":
        result = asyncio.run(interruptible(enqueue_manual(paths, payload["path"])))
        if not payload.get("queue_only"):
            if not kick(paths):
                asyncio.run(interruptible(worker(paths)))
        with State.open(paths) as state:
            result.update(status=state.job(result["job_id"])["status"])
        return result
    with State.open(paths) as state:
        recover_if_idle(state, paths)
        if action == "history":
            return state.history(project_id(payload["path"]) if payload.get("path") else None)
        if action == "resolve":
            ident = project_id(payload["path"])
            state.resolve(ident, payload["outcome"])
            return state.project(ident)
        if action == "logs":
            job = state.job(int(payload["job_id"]))
            path = paths.home / "logs" / f"{job['id']}.log"
            if not path.exists():
                return "此任务尚未输出日志。"
            with path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 65536))
                return stream.read().decode("utf-8", errors="replace")
    raise AutoCDError("不支持的操作。")


def wake_worker(paths):
    if installed(paths):
        with State.open(paths) as state:
            queued = bool(state.queued())
        if queued:
            kick(paths)


def status(paths):
    schedule = overview(paths)
    if not paths.database.exists():
        return [], schedule
    with State.open(paths) as state:
        recover_if_idle(state, paths)
        rows = state.all()
    for row in rows:
        row["remaining_seconds"] = None
        row["observation_fresh"] = False
        row["timer_active"] = (schedule["active"] and
                               f"{schedule.get('prefix')}-{row['id']}.timer" in schedule.get("timers", []))
        try:
            config = read(row["path"])
            row["branch"] = config.branch
            window = from_project(row)
            now = clock()
            row["observation_fresh"] = (window.observed is not None and window.fingerprint == config.fingerprint
                                        and 0 <= now - window.observed <= config.interval * 2.5 + 1)
            if row["phase"] == "cooling" and row["observation_fresh"]:
                row["remaining_seconds"] = window.remaining(now, config.cooldown)
        except AutoCDError as exc:
            row["error"] = str(exc)
    return rows, schedule


def project_rows(paths, root):
    found = discover(root)
    stored, online = status(paths)
    mapped = {row["id"]: row for row in stored}
    rows = []
    for entry in found:
        rows.append({**dict(enabled=0, phase="paused", deployed_sha=None, remaining_seconds=None),
                     **mapped.get(entry["id"], {}), **entry})
    return rows, online


PHASES = dict(paused="已暂停", monitoring="等待观察", cooling="冷静中", queued="排队中",
              deploying="部署中", current="已是最新", failed="失败待重试", unknown="需人工核实", error="观察异常")


def project_lines(row, index=None):
    phase = "配置错误" if row.get("error") else PHASES.get(row["phase"], row["phase"])
    tone = ("error" if row.get("error") or row["phase"] in {"failed", "error", "unknown"}
            else "success" if row["phase"] == "current"
            else "warning" if row["phase"] in {"cooling", "queued", "deploying"} else "muted")
    label = (ui.color(f"[{index}]", "accent") + " ") if index is not None else ""
    label += ui.color(Path(row["path"]).name, "title")
    switch = ("自动监视已开启" if row.get("timer_active") else "已纳入检查（手动触发）") if row["enabled"] else "监视已暂停"
    remaining = row.get("remaining_seconds")
    if remaining is not None and row["phase"] == "cooling":
        phase += f" · 剩余 {remaining:.0f}s"
    if row["enabled"] and not row.get("observation_fresh") and row["phase"] in {"current", "cooling", "monitoring"}:
        phase += "（等待新观察）"
    lines = [label, f"{ui.color(switch, 'accent' if row['enabled'] else 'muted')}  ·  {ui.color(phase, tone)}",
             ui.color(row["path"], "muted")]
    branch = row.get("branch") or "—"
    sha = (row.get("deployed_sha") or "—")[:12]
    lines.append(f"分支 {ui.inline(branch)}  ·  已部署 {ui.inline(sha)}")
    error = row.get("error") or row.get("last_error")
    if error:
        lines.append(ui.color(f"原因：{error}", "error"))
    return lines


def show_rows(rows, schedule, *, interactive=False):
    label = "[调度] 定时任务已开启 · 无常驻进程" if schedule["active"] else "[调度] 手动模式 · 使用单次检查，或安装并开启定时任务"
    print(ui.color(label, "success" if schedule["active"] else "warning"))
    if schedule.get("error"):
        ui.notice(schedule["error"], "error")
    lines = []
    if not rows:
        hint = "按 [n] 新建项目配置，再开启监视。" if interactive else "使用 config create PATH 创建项目配置。"
        lines = ["暂无项目", ui.color(hint, "muted")]
    for index, row in enumerate(rows, 1):
        if lines:
            lines.append("")
        lines.extend(project_lines(row, index))
    ui.panel(f"项目 · {len(rows)}", lines)


def save_config(project, script, *, expect=None, create=False):
    project = Path(project).expanduser().resolve()
    if not project.is_dir():
        raise AutoCDError("项目目录不存在，请先创建目录。")
    config = parse(script)
    asyncio.run(check_bash(script))
    path = project / FILENAME
    if create and path.exists():
        raise AutoCDError("配置已存在，请使用 config edit。")
    if expect is not None and read(project).fingerprint != expect:
        raise AutoCDError("编辑期间配置已被修改；未覆盖原文件，请重新编辑。")
    atomic_write(path, config.script)
    return path


def edit_in_editor(script):
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    argv = shlex.split(editor)
    if not argv:
        raise AutoCDError("EDITOR 不能为空。")
    with tempfile.TemporaryDirectory(prefix="autocd-edit-") as directory:
        path = Path(directory) / FILENAME
        atomic_write(path, script)
        try:
            code = subprocess.call([*argv, str(path)])
        except OSError as exc:
            raise AutoCDError("无法启动编辑器，请设置 EDITOR。") from exc
        if code:
            raise AutoCDError("编辑器未正常退出，原配置保持不变。")
        return path.read_text(encoding="utf-8")


async def defaults(project):
    repository, branch = "", "main"
    try:
        repository = await git("remote", "get-url", "origin", cwd=project)
        branch = await git("symbolic-ref", "--short", "HEAD", cwd=project)
        validate_repository(repository)
    except AutoCDError:
        repository = ""
    return repository, branch


def wizard(project, edit=False):
    project = Path(project).expanduser().resolve()
    old = read(project) if edit else None
    ui.heading("修改项目配置" if edit else "新建项目配置", project)
    ui.notice("Enter 保留默认值；Ctrl+C 退出。部署命令将在编辑器中填写。")
    repo, branch = asyncio.run(defaults(project)) if not old else (old.repository, old.branch)
    try:
        script = render(
            ui.prompt("仓库地址", repo), ui.prompt("监视分支", branch),
            float(ui.prompt("监视间隔（分钟）", old.interval_minutes if old else 1)),
            float(ui.prompt("冷静间隔（分钟）", old.cooldown_minutes if old else 5)),
            float(ui.prompt("部署超时（分钟）", old.timeout_minutes if old else 15)),
            **({"body": old.body} if old else {}),
        )
    except (ValueError, TypeError) as exc:
        raise AutoCDError("请输入有效的分钟数。") from exc
    if not edit or ui.prompt("打开编辑器填写部署命令？y/N", "n").lower() == "y":
        script = edit_in_editor(script)
    path = save_config(project, script, expect=old.fingerprint if old else None, create=not edit)
    ui.notice(f"已保存 {path}", "success")


def delete_config(paths, project):
    project = Path(project).expanduser().resolve()
    call(paths, "pause", path=str(project))
    (project / FILENAME).unlink()
    ui.notice("已删除配置。执行中的任务使用快照继续完成，历史数据保留。", "success")


def check(project, remote=False):
    config = read(project)
    asyncio.run(check_bash(config.script))
    result = {"valid": True, "branch": config.branch, "fingerprint": config.fingerprint}
    if remote:
        result["sha"] = asyncio.run(remote_sha(config, project))
    return result


def format_history(rows):
    lines = []
    statuses = dict(succeeded=("成功", "success"), failed=("失败", "error"), unknown=("需核实", "warning"),
                    queued=("排队中", "warning"), deploying=("部署中", "warning"), cancelled=("已取消", "muted"))
    for row in rows:
        if lines:
            lines.append("")
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["queued_at"]))
        label, tone = statuses.get(row["status"], (row["status"], "muted"))
        lines.append(ui.color(f"#{row['id']}", "accent") + "  " + ui.color(label, tone)
                     + "  " + ui.inline(row["sha"][:12]))
        lines.append(ui.color(when, "muted"))
        if row.get("error"):
            lines.append(ui.color(row["error"], "error" if row["status"] == "failed" else "warning"))
    if not rows:
        lines = [ui.color("尚无部署记录。", "muted")]
    ui.panel("部署历史", lines)
