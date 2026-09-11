from dataclasses import dataclass
import hashlib
import os
from pathlib import Path

from .config import AutoCDError


@dataclass(frozen=True)
class Paths:
    home: Path

    @classmethod
    def default(cls, value=None):
        if value:
            return cls(Path(value).expanduser().resolve())
        if os.getuid() == 0:
            return cls(Path("/var/lib/autocd"))
        return cls(Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "autocd")

    @property
    def database(self):
        return self.home / "state.sqlite3"

    def prepare(self):
        if self.home in {Path("/"), Path.home()}:
            raise AutoCDError("--home 必须指向 AutoCD 独占的数据目录。")
        for directory in (self.home, self.home / "run", self.home / "logs",
                          self.home / "cache", self.home / "releases", self.home / "programs"):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if directory.is_symlink() or directory.stat().st_uid != os.getuid():
                raise AutoCDError(f"AutoCD 数据目录必须由当前用户独占：{directory}")
            directory.chmod(0o700)

    @property
    def worker_lock(self):
        return self.home / "run" / "worker.lock"

    @property
    def installation(self):
        return self.home / "schedule.json"


def project_id(path):
    return hashlib.sha256(os.fsencode(Path(path).resolve())).hexdigest()[:20]
