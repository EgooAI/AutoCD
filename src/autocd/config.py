"""Parse metadata as data; never source a deployment script."""

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import tomllib
from urllib.parse import urlsplit


class AutoCDError(Exception):
    """An actionable error safe to display to the operator."""


BEGIN = "# autocd:begin v1"
END = "# autocd:end"
FILENAME = ".autocd.sh"
FIELDS = {"repository", "branch", "interval_minutes", "cooldown_minutes", "timeout_minutes"}
DEFAULT_BODY = '''\nset -euo pipefail
cd "$RELEASE_DIR"
printf 'Deploying %s (previous: %s)\\n' "$DEPLOY_SHA" "$PREVIOUS_SHA"
# Replace this placeholder with build, activation and health-check commands.
printf 'Deployment commands have not been configured.\\n' >&2
exit 1
'''


@dataclass(frozen=True)
class Config:
    repository: str
    branch: str
    interval_minutes: float = 1
    cooldown_minutes: float = 5
    timeout_minutes: float = 15
    body: str = DEFAULT_BODY
    script: str = ""

    @property
    def fingerprint(self):
        return hashlib.sha256(self.script.encode()).hexdigest()

    @property
    def interval(self):
        return self.interval_minutes * 60

    @property
    def cooldown(self):
        return self.cooldown_minutes * 60

    @property
    def timeout(self):
        return self.timeout_minutes * 60


def validate_repository(value):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise AutoCDError("repository 必须为非空仓库地址。")
    if any(ord(c) < 32 or ord(c) == 127 for c in value) or value.startswith("-"):
        raise AutoCDError("repository 含无效字符。")
    if "::" in value:
        raise AutoCDError("不支持 Git 自定义 remote helper。")
    if "://" in value:
        try:
            url = urlsplit(value)
        except ValueError as exc:
            raise AutoCDError("repository 地址格式无效。") from exc
        if url.scheme not in {"https", "http", "ssh", "file"}:
            raise AutoCDError("repository 仅支持 HTTP(S)、SSH、file 或本地路径。")
        if url.password or (url.scheme in {"https", "http"} and url.username):
            raise AutoCDError("请通过 Git 凭据助手或 SSH 提供凭据，不要写入仓库地址。")
        if url.query or url.fragment:
            raise AutoCDError("仓库地址不能包含 query 或 fragment。")
        if url.scheme != "file" and not url.hostname:
            raise AutoCDError("repository 地址缺少主机名。")


def validate_branch(value):
    if not isinstance(value, str) or not value or value.startswith(("-", "/", ".")):
        raise AutoCDError("branch 必须是有效的 Git 分支名。")
    if (value == "@" or value.endswith(("/", ".")) or ".." in value or "@{" in value
            or "//" in value or re.search(r"[\x00-\x20\x7f~^:?*\[\\]", value)
            or any(p.startswith(".") or p.endswith(".lock") for p in value.split("/"))):
        raise AutoCDError("branch 必须是有效的 Git 分支名。")


def parse(script):
    if len(script.encode()) > 1024 * 1024 or "\0" in script:
        raise AutoCDError("配置必须是小于 1 MiB 的 UTF-8 文本，且不能包含 NUL。")
    lines = script.splitlines(keepends=True)
    start = 1 if lines and lines[0].startswith("#!") else 0
    if not lines or len(lines) <= start or lines[start].strip() != BEGIN:
        raise AutoCDError("配置文件开头缺少 # autocd:begin v1，或版本不受支持。")
    markers = [line.strip() for line in lines if line.strip().startswith("# autocd:")]
    if markers != [BEGIN, END]:
        raise AutoCDError("配置头标记缺失、重复或顺序错误。")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == END)
    header = []
    for line in lines[start + 1:end]:
        if not line.startswith("#"):
            raise AutoCDError("配置头的每一行都必须是 # 注释。")
        header.append(line[2:] if line.startswith("# ") else line[1:])
    try:
        values = tomllib.loads("".join(header))
    except tomllib.TOMLDecodeError as exc:
        # TOML parser errors can include credential-bearing source text.
        raise AutoCDError("配置头不是有效 TOML（检查引号、重复键和数值）。") from exc
    if set(values) - FIELDS or not {"repository", "branch", "interval_minutes", "cooldown_minutes"} <= values.keys():
        raise AutoCDError("配置头字段缺失或包含未知字段。")
    validate_repository(values["repository"])
    validate_branch(values["branch"])
    values.setdefault("timeout_minutes", 15)
    for field in ("interval_minutes", "cooldown_minutes", "timeout_minutes"):
        value = values[field]
        if type(value) not in (float, int) or not math.isfinite(value) or not 0 < value <= 525600:
            raise AutoCDError(f"{field} 必须是大于 0 且不超过 525600 的有限分钟数。")
    return Config(**values, body="".join(lines[end + 1:]), script=script)


def read(project):
    path = Path(project) / FILENAME
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            script = stream.read(1024 * 1024 + 1)
        return parse(script)
    except (OSError, UnicodeError) as exc:
        raise AutoCDError(f"无法读取 {path}（需要 UTF-8 格式）。") from exc


def render(repository, branch, interval_minutes=1, cooldown_minutes=5,
           timeout_minutes=15, body=DEFAULT_BODY):
    values = dict(repository=repository, branch=branch, interval_minutes=interval_minutes,
                  cooldown_minutes=cooldown_minutes, timeout_minutes=timeout_minutes)
    header = "\n".join(f"# {key} = {json.dumps(value, ensure_ascii=False)}" for key, value in values.items())
    script = f"#!/usr/bin/env bash\n{BEGIN}\n{header}\n{END}\n{body}"
    return parse(script).script


def atomic_write(path, text, mode=0o600):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
