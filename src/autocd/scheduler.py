"""A cooling window requires uninterrupted, monotonic observations."""

from dataclasses import dataclass
from pathlib import Path
import time


@dataclass
class Window:
    sha: str | None = None
    fingerprint: str | None = None
    since: float | None = None
    observed: float | None = None
    since_utc: float | None = None

    def reset(self):
        self.sha = self.fingerprint = self.since = self.observed = self.since_utc = None

    def observe(self, sha, fingerprint, monotonic, utc, interval, cooldown):
        gap = self.observed is not None and monotonic - self.observed > interval * 2.5 + 1
        if (self.sha != sha or self.fingerprint != fingerprint or self.since is None
                or gap or monotonic < self.observed):
            self.sha, self.fingerprint = sha, fingerprint
            self.since, self.since_utc = monotonic, utc
        self.observed = monotonic
        return monotonic - self.since >= cooldown

    def remaining(self, now, cooldown):
        return max(0, cooldown - (now - self.since)) if self.since is not None else None


def boot_id():
    # Include the clock domain so records from the old clock cannot be reused.
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":boottime"


def clock():
    # Linux BOOTTIME includes suspend, so resuming cannot conceal an observation gap.
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def from_project(project):
    if project.get("boot_id") != boot_id():
        return Window()
    return Window(project["candidate_sha"], project["fingerprint"], project.get("stable_monotonic"),
                  project.get("observed_monotonic"), project["stable_since"])


def ready(project, config, job=None):
    window = from_project(project)
    now = clock()
    if (not project["enabled"] or project["needs_review"] or window.since is None
            or window.observed is None or window.fingerprint != config.fingerprint):
        return False
    if job and (job["sha"] != window.sha or job["fingerprint"] != window.fingerprint
                or job["revision"] != project["revision"]):
        return False
    return (0 <= now - window.observed <= config.interval * 2.5 + 1
            and now - window.since >= config.cooldown)
