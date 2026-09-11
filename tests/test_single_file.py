import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import support
from autocd import __version__
from autocd.config import AutoCDError
from autocd.paths import Paths
from autocd.service import install, installed, start, stop, sync_project, uninstall
from autocd.state import State
from autocd.units import project_units, service, worker_units


class SingleFileTests(unittest.TestCase):
    def test_deterministic_and_isolated_execution(self):
        with tempfile.TemporaryDirectory(prefix="autocd-build-") as directory:
            root = Path(directory)
            first = root / "a/autocd.py"
            second = root / "b/autocd.py"
            for target in (first, second):
                subprocess.run([sys.executable, str(support.ROOT / "tools/build_single.py"), "--output", str(target)],
                               check=True, stdout=subprocess.DEVNULL)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertIn(hashlib.sha256(first.read_bytes()).hexdigest(), (first.parent / "SHA256SUMS").read_text())
            result = subprocess.run([sys.executable, "-I", str(first), "--version"], cwd=root,
                                    text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout.strip(), __version__)
            result = subprocess.run([sys.executable, "-I", str(first), "--home", str(root / "state"), "list", "--json"],
                                    cwd=root, text=True, capture_output=True, check=True)
            self.assertIn('"projects": []', result.stdout)

    def test_service_quotes_paths_and_persists_environment(self):
        manifest = dict(python="/usr/bin/python3", program="/tmp/app space/autocd.py", path_env="/bin:/test path")
        unit = service(manifest, Paths(Path("/tmp/cd")), ["worker"], timeout="infinity")
        self.assertIn('"/tmp/app space/autocd.py"', unit)
        self.assertIn("KillMode=control-group", unit)
        self.assertIn('Environment="PATH=/bin:/test path"', unit)
        self.assertIn("Type=oneshot", unit)
        self.assertNotIn("Restart=", unit)

    @unittest.skipIf(os.getuid() == 0, "User service installation test requires a non-root user")
    def test_service_keeps_program_after_temporary_download_is_removed(self):
        with tempfile.TemporaryDirectory(prefix="autocd-service-") as directory:
            root = Path(directory)
            download = root / "download.py"
            download.write_text("#!/usr/bin/env python3\nprint('test artifact')\n")
            paths = Paths(root / "state")
            with (patch("autocd.service.unit_dir", return_value=root / "units"),
                  patch.object(sys, "_autocd_artifact", str(download), create=True),
                  patch("autocd.service.control") as control,
                  patch("builtins.print")):
                install(paths)
                stored = list((paths.home / "programs").glob("*/autocd.py"))
                self.assertEqual(len(stored), 1)
                self.assertEqual(stored[0].read_bytes(), download.read_bytes())
                manifest = installed(paths)
                unit = root / "units" / (manifest["prefix"] + "-worker.service")
                self.assertIn(str(stored[0]), unit.read_text())
                download.write_text("print('new artifact')\n")
                install(paths)
                self.assertNotEqual(manifest["program"], installed(paths)["program"])
                download.unlink()
                self.assertTrue(stored[0].exists())
                self.assertTrue(any(call.args[:2] == ("enable", "--now") for call in control.call_args_list))

    def test_missing_systemd_does_not_leave_an_installation(self):
        with tempfile.TemporaryDirectory(prefix="autocd-service-") as directory:
            paths = Paths(Path(directory) / "state")
            with (patch.object(sys, "_autocd_artifact", "irrelevant", create=True),
                  patch("autocd.service.control", side_effect=AutoCDError("offline"))):
                with self.assertRaises(AutoCDError):
                    install(paths)
            self.assertFalse(paths.home.exists())

    def test_projects_have_independent_timers_and_worker_fallback(self):
        paths = Paths(Path("/tmp/cd"))
        manifest = dict(prefix="autocd-test", python="/usr/bin/python3", program="/tmp/a.py", path_env="/bin")
        first = project_units(manifest, paths, {"id": "one"}, 3)
        second = project_units(manifest, paths, {"id": "two"}, 60)
        self.assertIn("OnUnitInactiveSec=3.000000s", first["autocd-test-one.timer"])
        self.assertIn("OnUnitInactiveSec=60.000000s", second["autocd-test-two.timer"])
        self.assertIn('"observe" "--id" "one" "--scheduled"', first["autocd-test-one.service"])
        self.assertIn("autocd-test-worker.timer", worker_units(manifest, paths))

    def test_timer_lifecycle_preserves_state_and_never_stops_running_worker(self):
        with tempfile.TemporaryDirectory(prefix="autocd-units-") as directory:
            root = Path(directory)
            paths = Paths(root / "state")
            artifact = root / "download.py"
            artifact.write_text("# standalone program\n")
            repo = support.Repository(root)
            with State.open(paths) as state:
                row = state.register(repo.project)
                state.update(row["id"], enabled=1)
            with (patch("autocd.service.unit_dir", return_value=root / "units"),
                  patch.object(sys, "_autocd_artifact", str(artifact), create=True),
                  patch("autocd.service.control") as control,
                  patch("builtins.print")):
                install(paths)
                manifest = installed(paths)
                timer = root / "units" / f"{manifest['prefix']}-{row['id']}.timer"
                self.assertTrue(timer.exists())
                repo.configure("echo updated\n", interval=2)
                sync_project(paths, row)
                self.assertIn("120.000000s", timer.read_text())
                stop(paths)
                self.assertFalse(installed(paths)["active"])
                self.assertFalse(any(call.args[:1] == ("stop",) for call in control.call_args_list))
                start(paths)
                self.assertTrue(installed(paths)["active"])
                uninstall(paths)
                self.assertFalse(paths.installation.exists())
                self.assertEqual(list((root / "units").iterdir()), [])
                with State.open(paths) as state:
                    self.assertTrue(state.project(row["id"])["enabled"])

    def test_partial_timer_start_failure_keeps_schedule_disabled(self):
        with tempfile.TemporaryDirectory(prefix="autocd-start-") as directory:
            root = Path(directory)
            paths = Paths(root / "state")
            artifact = root / "download.py"
            artifact.write_text("# standalone program\n")
            with (patch("autocd.service.unit_dir", return_value=root / "units"),
                  patch.object(sys, "_autocd_artifact", str(artifact), create=True),
                  patch("autocd.service.control") as control,
                  patch("builtins.print")):
                install(paths)
                stop(paths)

                def fail_enable(*args, **kwargs):
                    if args[0] == "enable":
                        self.assertTrue(installed(paths)["active"])
                        raise AutoCDError("simulated timer failure")

                control.side_effect = fail_enable
                for action in (start, install):
                    with self.subTest(action=action.__name__), self.assertRaises(AutoCDError):
                        action(paths)
                    self.assertFalse(installed(paths)["active"])
                    self.assertEqual(control.call_args.args[:2], ("disable", "--now"))

    def test_invalid_config_keeps_retry_timer_and_recovers_after_edit(self):
        with tempfile.TemporaryDirectory(prefix="autocd-retry-") as directory:
            root = Path(directory)
            paths = Paths(root / "state")
            artifact = root / "download.py"
            artifact.write_text("# standalone program\n")
            repo = support.Repository(root)
            config = repo.project / ".autocd.sh"
            config.write_text("invalid config\n")
            with State.open(paths) as state:
                row = state.register(repo.project)
                state.update(row["id"], enabled=1)
            with (patch("autocd.service.unit_dir", return_value=root / "units"),
                  patch.object(sys, "_autocd_artifact", str(artifact), create=True),
                  patch("autocd.service.control"), patch("builtins.print")):
                install(paths)
                timer = root / "units" / f"{installed(paths)['prefix']}-{row['id']}.timer"
                self.assertIn("OnUnitInactiveSec=30.000000s", timer.read_text())
                repo.configure("echo repaired\n", interval=2)
                sync_project(paths, row)
                self.assertIn("OnUnitInactiveSec=120.000000s", timer.read_text())
                config.unlink()
                sync_project(paths, row)
                self.assertIn("OnUnitInactiveSec=30.000000s", timer.read_text())
                stop(paths)
                start(paths)
                self.assertIn("OnUnitInactiveSec=30.000000s", timer.read_text())
                self.assertTrue(installed(paths)["active"])

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze not available")
    def test_systemd_accepts_generated_units(self):
        with tempfile.TemporaryDirectory(prefix="autocd-verify-") as directory:
            root = Path(directory)
            manifest = dict(prefix="autocd-verify", python=sys.executable,
                            program=str(support.ROOT / "dist/autocd.py"), path_env=os.environ["PATH"])
            paths = Paths(root / "state")
            files = {**worker_units(manifest, paths), **project_units(manifest, paths, {"id": "demo"}, 3)}
            for name, contents in files.items():
                (root / name).write_text(contents)
            result = subprocess.run(["systemd-analyze", "verify", *(str(root / name) for name in files)],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
