# AutoCD

Egooai 的轻量持续部署工具。扫描当前目录及其直接子目录中的 `.autocd.sh`，交互选择监视项目；当指定远程分支连续保持同一提交达到冷静间隔后，检出该提交并运行部署脚本。

## 交付方式

最终发布一个独立的 `autocd.py`，仅依赖 Linux、Python 3.11+、Git 和 Bash，不需要安装 Python 第三方包。后台常驻使用 systemd；关闭交互界面不会停止监视。

首个 Release 发布后，可在项目父目录一行下载并启动：

```bash
(autocd_tmp="$(mktemp)" && trap 'rm -f "$autocd_tmp"' EXIT && curl -fsSL https://github.com/Egooai/AutoCD/releases/latest/download/autocd.py -o "$autocd_tmp" && python3 "$autocd_tmp")
```

脚本完整下载后才运行，终端输入仍用于菜单。安装后台服务时会保存独立程序副本，临时下载文件删除不影响服务。Release 只通过 GitHub Actions 手动触发发布。

## 项目配置

`.autocd.sh` 是可直接编写多行 Bash 的本地配置文件。文件头注释中的 TOML 只用于读取配置，发现和校验项目时不会执行脚本。

```bash
#!/usr/bin/env bash
# autocd:begin v1
# repository = "git@github.com:Egooai/AutoProject.git"
# branch = "main"
# interval_minutes = 1
# cooldown_minutes = 5
# timeout_minutes = 15
# autocd:end

set -euo pipefail
cd "$RELEASE_DIR"
printf 'Deploying %s (previous: %s)\n' "$DEPLOY_SHA" "$PREVIOUS_SHA"
# 在此编写构建、服务切换及健康检查。
```

配置存在表示项目可使用 CD，监视需要单独开启。凭据由运行服务的用户通过 Git/SSH 提供，配置中不保存密码。仓库建议忽略 `.autocd.sh`，以 `.autocd.example.sh` 共享模板。

## 行为边界

- SQLite 保存监视开关、候选提交、冷静窗口和部署历史；网络中断、配置变化及后台重启后重新计时。
- 各项目独立轮询，部署全局串行。排队后再次核对远程提交及配置，执行时固定提交和脚本快照。
- 在独立发布目录执行，不修改原项目工作区。脚本接收 `DEPLOY_SHA`、`PREVIOUS_SHA`、`RELEASE_DIR`。
- 同一版本失败后不自动反复重试；异常中断的部署需人工核实。脚本负责健康检查和回滚，退出码决定结果。
- 暂不提供更新公告 API、Web 面板、数据库管理或自动删除历史发布目录。

## 仓库结构

```text
src/autocd/         配置、交互、调度、状态、Git 和进程执行
tools/             构建独立脚本
tests/             单元测试及本地 Git 集成测试
examples/          项目配置示例
docs/              状态与运行约定
.github/workflows/ CI 与手动发布
dist/autocd.py     构建产物，不纳入版本控制
```

开发源码保持模块化，发布产物包含全部程序代码及模板，运行时不再下载组件。
