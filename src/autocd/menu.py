from pathlib import Path

from . import __version__, ui
from .config import AutoCDError, read
from .operations import (call, check, delete_config, edit_in_editor, format_history, project_lines,
                         project_rows, save_config, show_rows, wizard)
from .service import manage, sync_project


def project_menu(paths, row):
    path = row["path"]
    while True:
        try:
            rows, online = project_rows(paths, path)
        except (AutoCDError, OSError) as exc:
            ui.notice(str(exc), "error")
            ui.pause()
            return
        try:
            row = next((item for item in rows if item["path"] == path), None)
            if row is None:
                ui.notice("项目配置已被移除，返回项目列表。", "warning")
                ui.pause()
                return
            print()
            ui.panel("项目管理", project_lines(row, online))
            if not online["active"]:
                ui.notice("当前为手动模式；[t] 检查一次，或在首页 [s] 安装定时任务。", "warning")
            ui.actions([
                ("监视与部署", [("e", "开启监视"), ("p", "暂停监视"), ("t", "单次检查"), ("d", "立即部署")]),
                ("配置", [("c", "修改配置"), ("b", "编辑脚本"), ("v", "校验配置与远程")]),
                ("记录与维护", [("h", "部署历史"), ("l", "部署日志"), ("o", "核实中断结果"), ("x", "删除配置")]),
                ("导航", [("r", "刷新状态"), ("q", "返回项目列表")]),
            ], "Enter 刷新 · 暂停监视后，正在执行的部署仍会完成")
            choice = ui.prompt("选择操作", "r").lower()
            if choice == "q":
                return
            if choice == "r":
                continue
            print()
            if choice in {"e", "p"}:
                call(paths, "enable" if choice == "e" else "pause", path=path)
                ui.notice("已纳入检查，将从完整冷静窗口开始。" if choice == "e" else "已暂停后续自动部署。", "success")
                if choice == "e" and not online["active"]:
                    ui.notice("自动执行需安装并开启定时任务；现在可用 [t] 手动检查。", "warning")
            elif choice == "d":
                if ui.confirm("立即部署", f"项目：{path}\n将跳过冷静间隔，部署当前远程提交。"):
                    result = call(paths, "deploy", path=path)
                    ui.notice(f"任务 #{result['job_id']} · {result['sha'][:12]} · {result['status']}",
                              "error" if result["status"] in {"failed", "unknown"} else "success")
                else:
                    continue
            elif choice == "c":
                wizard(path, edit=True)
                sync_project(paths, row)
            elif choice == "b":
                old = read(path)
                save_config(path, edit_in_editor(old.script), expect=old.fingerprint)
                sync_project(paths, row)
                ui.notice("脚本已保存。", "success")
            elif choice == "t":
                ui.notice("检查远程并更新冷静窗口；达到条件时在本次操作中部署。")
                result = call(paths, "run-once", path=path)
                for item in result["checks"]:
                    ui.notice(item.get("error") or "检查完成，冷静窗口已保存。", "error" if item.get("error") else "success")
                if result["deployments"]:
                    format_history(result["deployments"])
            elif choice == "v":
                ui.notice("正在校验配置和远程连接…")
                result = check(path, remote=True)
                ui.notice(f"校验通过 · 远程 {result['sha'][:12]}", "success")
            elif choice == "h":
                format_history(call(paths, "history", path=path))
            elif choice == "l":
                jobs = call(paths, "history", path=path)
                format_history(jobs)
                if jobs:
                    job_id = int(ui.prompt("部署编号", jobs[0]["id"]))
                    if job_id not in {job["id"] for job in jobs}:
                        raise AutoCDError("请从上面的部署编号中选择。")
                    log = call(paths, "logs", job_id=job_id)
                    ui.panel(f"部署日志 · #{job_id}", [ui.inline(line) for line in log.splitlines()])
            elif choice == "o":
                ui.panel("核实中断结果", ["请先检查实际服务状态，再记录中断任务的结果。",
                                         ui.color("成功输入 succeeded；失败输入 failed；Enter 取消。", "muted")])
                outcome = ui.prompt("核实结果", "")
                if not outcome:
                    continue
                call(paths, "resolve", path=path, outcome=outcome)
                ui.notice("结果已记录。失败任务可使用立即部署重试。", "success")
            elif choice == "x":
                if ui.confirm("删除项目配置", f"项目：{path}\n删除 .autocd.sh，保留部署历史；已运行的任务继续完成。", "delete"):
                    delete_config(paths, path)
                    ui.pause()
                    return
                continue
            else:
                ui.notice("未识别的操作，请输入方括号中的快捷键。", "warning")
        except (AutoCDError, ValueError, OSError) as exc:
            ui.notice(str(exc), "error")
        ui.pause()


def menu(paths, root):
    while True:
        ui.heading(f"项目总览 · v{__version__}", f"扫描目录：{root}")
        try:
            rows, online = project_rows(paths, root)
        except (AutoCDError, OSError) as exc:
            ui.notice(str(exc), "error")
            return
        try:
            show_rows(rows, online, interactive=True)
            ui.actions([
                ("项目", [("编号", "进入项目管理"), ("n", "新建配置")]),
                ("批量操作", [("a", "全部开启监视"), ("p", "全部暂停监视")]),
                ("定时任务", [("s", "安装/更新"), ("u", "开启定时触发"), ("z", "停止定时触发")]),
                ("导航", [("r", "刷新"), ("q", "退出")]),
            ], "Enter 刷新 · 关闭菜单不影响系统定时触发")
            choice = ui.prompt("选择项目编号或操作", "r").lower()
            if choice == "q":
                return
            if choice == "r":
                continue
            if choice.isdigit() and 0 < int(choice) <= len(rows):
                project_menu(paths, rows[int(choice) - 1])
                continue
            print()
            if choice == "n":
                path = Path(ui.prompt("项目目录", str(root))).expanduser().resolve()
                wizard(path)
            elif choice == "s":
                manage(paths, "install")
            elif choice in {"u", "z"}:
                manage(paths, "start" if choice == "u" else "stop")
            elif choice in {"a", "p"}:
                if not rows:
                    ui.notice("当前没有可操作的项目，请先新建配置。", "warning")
                else:
                    if choice == "a" and not ui.confirm("批量开启监视", "为以上所有有效配置开启自动部署，并开始冷静计时。"):
                        continue
                    completed, failed = 0, 0
                    for row in rows:
                        try:
                            call(paths, "enable" if choice == "a" else "pause", path=row["path"])
                            completed += 1
                            ui.notice(row["path"], "success")
                        except AutoCDError as exc:
                            failed += 1
                            ui.notice(f"{row['path']}: {exc}", "error")
                    ui.notice(f"批量操作结束：完成 {completed} 项，失败 {failed} 项。", "warning" if failed else "success")
            else:
                ui.notice("请输入列表中的项目编号，或方括号中的快捷键。", "warning")
        except (AutoCDError, ValueError, OSError) as exc:
            ui.notice(str(exc), "error")
        ui.pause()
