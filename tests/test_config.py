from pathlib import Path
import tempfile
import unittest

import support  # noqa: F401 -- install the source path for stdlib unittest discovery
from autocd.config import AutoCDError, atomic_write, parse, read, render
from autocd.discovery import discover
from autocd.operations import save_config
from autocd.ui import terminal_text


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="autocd-config-")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def test_body_preserved_and_never_executed(self):
        body = f'''\nset -eu
cat <<'END'
    indentation: "quotes" # literal comment
END
touch {self.root / 'must-not-exist'}
'''
        script = render("git@example.org:org/repo.git", "feature/cd", body=body)
        config = parse(script)
        self.assertEqual(config.body, body)
        self.assertEqual(parse(render(config.repository, config.branch, 2, 3, body=config.body)).body, body)
        atomic_write(self.root / ".autocd.sh", script)
        self.assertEqual(read(self.root).script, script)
        self.assertEqual(len(discover(self.root)), 1)
        self.assertFalse((self.root / "must-not-exist").exists())

    def test_invalid_metadata_rejected_without_leaking_source(self):
        script = render("https://example.org/repo.git", "main")
        variants = [
            script.replace("begin v1", "begin v2"),
            script.replace("# autocd:end", ""),
            script + "\n# autocd:end\n",
            script.replace("# branch =", "branch ="),
            script.replace('# branch = "main"', '# branch = "main"\n# branch = "duplicate"'),
            script.replace("# interval_minutes = 1", "# interval_minutes = nan"),
            script.replace("# interval_minutes = 1", "# interval_minutes = true"),
            script.replace("# interval_minutes = 1", "# interval_minutes = 0"),
            script.replace("# interval_minutes = 1", "# interval_minutes = -1"),
            script.replace("# interval_minutes = 1", "# interval_minutes = inf"),
            script.replace('# branch = "main"', '# branch = "../main"'),
            script.replace("https://example.org/repo.git", "https://user:secret@example.org/repo.git"),
            script.replace("https://example.org/repo.git", "ext::malicious"),
            script.replace("https://example.org/repo.git", "https://["),
            script.replace("# autocd:end", "# token = 'secret'\n# autocd:end"),
        ]
        for variant in variants:
            with self.subTest(variant=variant.splitlines()[1:3]), self.assertRaises(AutoCDError) as error:
                parse(variant)
            self.assertNotIn("secret", str(error.exception))

    def test_one_level_discovery_symlink_dedup_and_errors(self):
        first = self.root / "first"
        nested = first / "nested"
        nested.mkdir(parents=True)
        bad = self.root / "bad"
        bad.mkdir()
        atomic_write(first / ".autocd.sh", render("/local/repo", "main"))
        atomic_write(nested / ".autocd.sh", render("/local/repo", "main"))
        (bad / ".autocd.sh").write_text("invalid")
        (self.root / "alias").symlink_to(first, target_is_directory=True)
        rows = discover(self.root)
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(bool(row["error"]) for row in rows), 1)
        self.assertNotIn(str(nested), {row["path"] for row in rows})

    def test_editor_syntax_error_and_stale_edit_do_not_overwrite(self):
        script = render("/local/repo", "main", body="echo valid\n")
        save_config(self.root, script, create=True)
        fingerprint = read(self.root).fingerprint
        with self.assertRaises(AutoCDError):
            save_config(self.root, render("/local/repo", "main", body="if\n"), expect=fingerprint)
        self.assertEqual(read(self.root).script, script)
        replacement = render("/local/repo", "main", body="echo replacement\n")
        save_config(self.root, replacement, expect=fingerprint)
        with self.assertRaises(AutoCDError):
            save_config(self.root, script, expect=fingerprint)
        self.assertEqual(read(self.root).script, replacement)
        self.assertEqual((self.root / ".autocd.sh").stat().st_mode & 0o777, 0o600)

    def test_terminal_control_characters_are_neutralized(self):
        self.assertNotIn("\x1b", terminal_text("\x1b]52;secret\x07"))
