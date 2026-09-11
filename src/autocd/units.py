"""systemd templates embedded in the standalone distribution."""

from .config import AutoCDError


MARKER = "# Managed by AutoCD (oneshot v1)"


def quote(value, *, command=False):
    value = str(value)
    if any(ord(c) < 32 for c in value):
        raise AutoCDError("服务路径不能包含控制字符。")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return '"' + (value.replace("$", "$$") if command else value) + '"'


def service(manifest, paths, args, *, timeout):
    command = " ".join(quote(part, command=True) for part in
                       [manifest["python"], manifest["program"], "--home", paths.home, *args])
    return f'''{MARKER}
[Unit]
Description=AutoCD finite task
StartLimitIntervalSec=0
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart={command}
TimeoutStartSec={timeout}
TimeoutStopSec=15
KillMode=control-group
UMask=0077
Environment=PYTHONUNBUFFERED=1
Environment={quote("PATH=" + manifest["path_env"])}
'''


def timer(unit, interval):
    accuracy = max(0.001, min(0.5, interval / 10))
    return f'''{MARKER}
[Unit]
Description=AutoCD scheduled check

[Timer]
OnActiveSec=1s
OnUnitInactiveSec={interval:.6f}s
AccuracySec={accuracy:.6f}s
Unit={unit}

[Install]
WantedBy=timers.target
'''


def worker_units(manifest, paths):
    name = manifest["prefix"] + "-worker"
    return {name + ".service": service(manifest, paths, ["worker", "--scheduled"], timeout="infinity"),
            name + ".timer": timer(name + ".service", 5)}


def project_units(manifest, paths, row, interval):
    name = manifest["prefix"] + "-" + row["id"]
    return {name + ".service": service(manifest, paths, ["observe", "--id", row["id"], "--scheduled"], timeout="75s"),
            name + ".timer": timer(name + ".service", interval)}
