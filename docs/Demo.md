# 模拟测试

以下命令在仓库根目录执行：

```bash
python3 tools/build_single.py
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
