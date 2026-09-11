import argparse
import asyncio
import json
from pathlib import Path
import sqlite3
import sys

from . import __version__, ui
from .config import AutoCDError, read, render
from .menu import menu
from .operations import (call, check, delete_config, edit_in_editor, format_history, project_rows,
                         save_config, show_rows, status, wake_worker, wizard)
from .paths import Paths
from .runner import interruptible, observe, recover_if_idle, worker
from .service import manage, sync_project
from .state import State


def parser():
    result = argparse.ArgumentParser(prog="autocd", description="AutoCD · 交互式 Git 自动部署；无参数进入菜单")
    result.add_argument("--version", action="version", version=__version__)
    result.add_argument("--home", help="AutoCD 独占的数据目录（所有短任务共用）")
    result.add_argument("--root", type=Path, default=Path.cwd(), help="扫描目录，默认当前目录；仅扫描一层子目录")
    commands = result.add_subparsers(dest="command")
    for name, help_text in (("list", "列出扫描目录中的项目"), ("status", "所有已登记项目状态")):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--json", action="store_true")
    commands.add_parser("migrate", help="迁移旧状态并核实是否存在中断任务")
    for name, description in (("tick", "检查一次，更新冷静窗口后退出"), ("run-once", "检查一次并执行就绪的部署，然后退出")):
        sub = commands.add_parser(name, help=description)
        sub.add_argument("path", type=Path, nargs="?", help="省略时检查所有已开启的项目")
    sub = commands.add_parser("observe", help="单项目检查（供定时任务调用）")
    sub.add_argument("--id", required=True)
    sub.add_argument("--scheduled", action="store_true")
    sub = commands.add_parser("worker", help="执行当前排队任务，队列快照处理完成即退出")
    sub.add_argument("--scheduled", action="store_true")
    for name in ("enable", "pause", "deploy"):
        sub = commands.add_parser(name, help={"enable": "开启项目监视", "pause": "暂停后续部署", "deploy": "立即部署，跳过冷静间隔"}[name])
        sub.add_argument("path", type=Path)
        if name == "deploy":
            sub.add_argument("--queue-only", action="store_true", help="仅入队，稍后执行 worker")
    sub = commands.add_parser("check", help="校验配置和 Bash 语法，默认不访问远程")
    sub.add_argument("path", type=Path)
    sub.add_argument("--remote", action="store_true")
    sub = commands.add_parser("history", help="查看部署历史")
    sub.add_argument("path", nargs="?", type=Path)
    sub.add_argument("--json", action="store_true")
    sub = commands.add_parser("logs", help="输出最近 64 KiB 部署日志")
    sub.add_argument("job_id", type=int)
    sub = commands.add_parser("resolve", help="标记已人工核实的中断任务")
    sub.add_argument("path", type=Path)
    sub.add_argument("--outcome", choices=["succeeded", "failed"], required=True)
    sub = commands.add_parser("schedule", help="管理 systemd 定时任务（root 使用系统服务）")
    sub.add_argument("action", choices=["install", "uninstall", "status", "start", "stop"])
    config = commands.add_parser("config", help="创建、编辑、删除 .autocd.sh").add_subparsers(dest="action", required=True)
    create = config.add_parser("create", help="省略 --repository 时进入配置向导")
    create.add_argument("path", type=Path)
    create.add_argument("--repository")
    create.add_argument("--branch", default="main")
    create.add_argument("--interval", type=float, default=1, metavar="MINUTES")
    create.add_argument("--cooldown", type=float, default=5, metavar="MINUTES")
    create.add_argument("--timeout", type=float, default=15, metavar="MINUTES")
    create.add_argument("--script", type=Path, help="读取此文件作为 Bash 正文")
    for name in ("edit", "delete"):
        sub = config.add_parser(name)
        sub.add_argument("path", type=Path)
        if name == "edit":
            sub.add_argument("--editor", action="store_true", help="直接用 EDITOR 编辑完整配置")
        else:
            sub.add_argument("--yes", action="store_true", required=True, help="确认仅删除 .autocd.sh")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    paths = Paths.default(args.home)
    try:
        if args.command is None:
            if not sys.stdin.isatty():
                raise AutoCDError("交互菜单需要终端；非交互运行请指定 list/status 等子命令。")
            menu(paths, args.root.resolve())
        elif args.command == "migrate":
            with State.open(paths) as state:
                recover_if_idle(state, paths)
            ui.notice("状态已迁移，监视开关与历史保留。", "success")
        elif args.command in {"tick", "run-once"}:
            result = call(paths, args.command, path=args.path)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if args.command == "run-once" and any(row["status"] in {"failed", "unknown"} for row in result["deployments"]):
                return 1
        elif args.command == "observe":
            result = asyncio.run(interruptible(observe(paths, ident=args.id, scheduled=args.scheduled)))
            if "project_id" in result:
                with State.open(paths) as state:
                    row = state.project(result["project_id"])
                sync_project(paths, row)
                wake_worker(paths)
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == "worker":
            result = asyncio.run(interruptible(worker(paths, args.scheduled)))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if any(row["status"] in {"failed", "unknown"} for row in result["deployments"]):
                return 1
        elif args.command in {"list", "status"}:
            rows, online = project_rows(paths, args.root) if args.command == "list" else status(paths)
            if args.json:
                print(json.dumps(dict(schedule=online, projects=rows), ensure_ascii=False, indent=2))
            else:
                show_rows(rows, online)
        elif args.command in {"enable", "pause", "deploy"}:
            result = call(paths, args.command, path=str(args.path.expanduser().resolve()),
                          queue_only=getattr(args, "queue_only", False))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if args.command == "deploy" and result["status"] in {"failed", "unknown", "cancelled"}:
                return 1
        elif args.command == "check":
            print(json.dumps(check(args.path.resolve(), args.remote), ensure_ascii=False, indent=2))
        elif args.command == "history":
            rows = call(paths, "history", path=str(args.path.resolve()) if args.path else None)
            print(json.dumps(rows, ensure_ascii=False, indent=2)) if args.json else format_history(rows)
        elif args.command == "logs":
            print(ui.terminal_text(call(paths, "logs", job_id=args.job_id)))
        elif args.command == "resolve":
            print(json.dumps(call(paths, "resolve", path=str(args.path.resolve()), outcome=args.outcome), ensure_ascii=False))
        elif args.command == "schedule":
            manage(paths, args.action)
        elif args.command == "config":
            if args.action == "create":
                if args.repository:
                    body = {"body": args.script.read_text()} if args.script else {}
                    script = render(args.repository, args.branch, args.interval, args.cooldown, args.timeout, **body)
                    print(save_config(args.path, script, create=True))
                else:
                    wizard(args.path)
            elif args.action == "edit":
                if args.editor:
                    old = read(args.path)
                    print(save_config(args.path, edit_in_editor(old.script), expect=old.fingerprint))
                else:
                    wizard(args.path, edit=True)
            else:
                delete_config(paths, args.path)
            if args.action == "edit":
                with State.open(paths) as state:
                    row = state.register(args.path)
                sync_project(paths, row)
        return 0
    except (AutoCDError, OSError, UnicodeError, sqlite3.Error) as exc:
        ui.notice(f"AutoCD: {exc}", "error", stream=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError, asyncio.CancelledError):
        ui.notice("当前进程已退出；系统定时任务不受影响。", stream=sys.stderr)
        return 130
