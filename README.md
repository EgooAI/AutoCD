# AutoCD

Egooai 的轻量持续部署工具。扫描当前目录及其直接子目录中的 `.autocd.sh`，交互选择监视项目；当指定远程分支连续保持同一提交达到冷静间隔后，检出该提交并运行部署脚本。

## 下载与启动

AutoCD 是独立的 `autocd.py` 脚本，运行需要 Linux、Python 3.11+、Git 2.29+ 和 Bash，自动调度需要 systemd。无需安装 Python 第三方包，也无需保持终端开启。

在项目父目录下载并启动：

```bash
(autocd_tmp="$(mktemp)" && trap 'rm -f "$autocd_tmp"' EXIT && curl -fsSL https://github.com/Egooai/AutoCD/releases/latest/download/autocd.py -o "$autocd_tmp" && python3 "$autocd_tmp")
```

安装定时任务时会保存程序副本，临时下载文件删除后仍可自动检查和部署。

## 本地运行

```bash
python3 tools/build_single.py
python3 dist/autocd.py
```

无参数进入中文菜单。先创建配置并填写部署命令，再安装定时任务、选择项目开启监视。默认模板以失败退出，需替换为实际部署命令。

输入方括号内的快捷键操作，Enter 刷新。设置 `NO_COLOR=1` 可关闭颜色；`TERM=dumb` 或重定向输出时自动关闭。

```bash
python3 dist/autocd.py config create /path/to/project
python3 dist/autocd.py check /path/to/project --remote
python3 dist/autocd.py schedule install
python3 dist/autocd.py enable /path/to/project
python3 dist/autocd.py status
```

普通用户安装 systemd 用户级定时任务，管理员需执行 `loginctl enable-linger <用户名>` 以保证注销后及开机执行。root 安装系统级任务。程序继承安装时的 PATH；Git 凭据、构建工具与部署权限属于运行用户。更新程序需显式重新执行 `schedule install`，数据保留。

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

- 监视设置、冷静窗口和部署历史会持久保存。正常进程退出不重置窗口；网络错误、观察断档、配置变化或系统重启后重新计时。
- 各项目由独立定时任务检查，部署全局串行。检查与部署互不阻塞；执行时固定提交和脚本快照。
- 在独立发布目录执行，不修改原项目工作区。脚本接收 `DEPLOY_SHA`、`PREVIOUS_SHA`、`RELEASE_DIR`。
- 同一版本失败后不自动反复重试；异常中断的部署需人工核实。脚本负责健康检查和回滚，退出码决定结果。
- 历史发布目录需自行清理。

可通过[模拟测试](docs/Demo.md) 体验完整部署流程；定时任务、数据目录和异常恢复说明见[运行约定](docs/Design.md)。
