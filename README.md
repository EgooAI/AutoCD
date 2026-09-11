# AutoCD

Egooai 的轻量持续部署工具。扫描当前目录及其直接子目录中的 `.autocd.sh`，交互选择监视项目；当指定远程分支连续保持同一提交达到冷静间隔后，检出该提交并运行部署脚本。

## 交付方式

最终发布一个独立的 `autocd.py`，仅依赖 Linux、Python 3.11+、Git 2.29+ 和 Bash，无 Python 第三方包或 AutoCD 常驻进程。systemd 定期唤起短任务，检查或部署完成即退出；跨次调用的状态保存在 SQLite。

首个 Release 发布后，可在项目父目录一行下载并启动：

```bash
(autocd_tmp="$(mktemp)" && trap 'rm -f "$autocd_tmp"' EXIT && curl -fsSL https://github.com/Egooai/AutoCD/releases/latest/download/autocd.py -o "$autocd_tmp" && python3 "$autocd_tmp")
```

脚本完整下载后才运行，终端输入仍用于菜单。安装定时任务时保存独立程序副本，临时文件删除不影响后续执行。Release 只通过 GitHub Actions 手动触发发布。

## 本地运行

```bash
python3 tools/build_single.py
python3 dist/autocd.py
```

无参数进入中文菜单，扫描当前目录和直接子目录。先创建配置并填写部署命令，再安装定时任务、选择项目开启监视。默认模板以失败退出，防止空命令被记录为部署成功。

菜单以边框区分状态和操作，输入方括号内的快捷键；Enter 刷新，操作结果确认后返回菜单。颜色由终端 ANSI 支持提供，无额外依赖；设置 `NO_COLOR=1`、使用 `TERM=dumb` 或重定向输出时自动关闭。JSON 输出保持纯文本。

```bash
python3 dist/autocd.py config create /path/to/project
python3 dist/autocd.py check /path/to/project --remote
python3 dist/autocd.py schedule install
python3 dist/autocd.py enable /path/to/project
python3 dist/autocd.py status
```

普通用户安装 systemd 用户级定时任务，管理员需执行 `loginctl enable-linger <用户名>` 以保证注销后及开机执行。root 安装系统级任务。程序继承安装时的 PATH；Git 凭据、构建工具与部署权限属于运行用户。更新程序需显式重新执行 `schedule install`，数据保留。

### 无真实服务的人工测试

```bash
python3 tools/demo.py setup
```

演示项目只使用本地 Git 仓库，并向演示目录写入结果文件。监视间隔 3 秒、冷静间隔 9 秒，不构建或重启真实服务。无需启动后台：

```bash
# 菜单中选择 Demo，按 e 纳入检查，再每隔约 3 秒按 t 单次检查
python3 dist/autocd.py --home .sandbox/demo/state --root .sandbox/demo/projects

# 发布演示项目的下一个提交，重新开始观察
python3 tools/demo.py update

# 有限次测试：每轮启动独立进程，验证冷静窗口和部署后退出
python3 tools/demo.py verify
```

部署成功后查看 `.sandbox/demo/projects/Demo/deployed.txt`、菜单历史和日志。手动模式需要持续按间隔检查：若两次检查相隔超过 8.5 秒，Demo 的窗口会重新计时。正式服务器安装定时任务后自动检查。

### 常用命令

全局参数 `--home`（数据目录）和 `--root`（扫描目录）放在子命令之前；同一实例的所有调用使用相同 `--home`。

| 命令 | 行为 |
| --- | --- |
| `list` / `status` | 当前扫描范围 / 所有已登记项目及定时任务状态；支持 `--json` |
| `config create/edit/delete PATH` | 配置向导；删除要求 `--yes` |
| `config edit PATH --editor` | 用 `$VISUAL` / `$EDITOR` 编辑整个脚本 |
| `check PATH [--remote]` | 校验元数据、Bash 语法，可选远程连通性 |
| `enable PATH` / `pause PATH` | 开启监视 / 暂停后续自动部署 |
| `deploy PATH [--queue-only]` | 跳过冷静间隔部署；无定时安装时前台执行，可选仅入队 |
| `tick [PATH]` / `worker` | 检查一次并退出 / 处理当前队列快照并退出 |
| `run-once [PATH]` | 前台检查并执行就绪任务；不自行重复运行 |
| `history [PATH]` / `logs JOB_ID` | 部署记录 / 最近 64 KiB 日志 |
| `resolve PATH --outcome succeeded\|failed` | 核实中断任务后记录结果 |
| `schedule install/status/start/stop/uninstall` | 管理 systemd 定时任务；停止不打断已运行的部署 |
| `migrate` | 迁移旧状态库，保留监视开关及历史；旧 daemon 必须先停止 |

非交互创建支持 `--repository`、`--branch`、`--interval`、`--cooldown`、`--timeout` 和 `--script`（Bash 正文文件）。时间单位均为分钟，支持小数。

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

- SQLite 保存监视开关、候选提交、冷静窗口和部署历史。正常进程退出不重置窗口；网络错误、观察断档、配置变化或系统重启后重新计时。
- 各项目由独立定时任务检查，部署全局串行。检查与部署互不阻塞；执行时固定提交和脚本快照。
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

```bash
python3 -m unittest discover -s tests -v
```

测试使用临时目录、本地 bare Git 仓库和独立脚本短进程，不依赖线上服务。[运行约定](docs/design.md) 记录定时任务、并发状态与异常恢复边界。

发布时先同步 `pyproject.toml` 与 `src/autocd/__init__.py` 的版本，再在 GitHub Actions → Release → Run workflow 选择 `main`、输入对应 `vX.Y.Z`。通过构建和测试后发布 `autocd.py` 与 `SHA256SUMS`；推送分支或标签均不会发布。已有标签不覆盖。
