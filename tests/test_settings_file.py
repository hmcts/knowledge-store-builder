"""A store's settings are committed with it, in `config/knowledgestore.toml`.

Settings that change what a layer contains used to live only in a shell, so a
rebuild in a shell that lacked them succeeded and wrote a poorer layer (#483).
These tests run the real CLI in a child process against a real store on disk:
the file is read when the library is imported, so only a fresh interpreter can
show what a store's own file does. The estate holds two chart conflicts each
setting settles: a wrapper republishing two applications' charts, which
`KSB_UMBRELLA_REPOS = "app-m"` resolves, and a chart defined both by `chart-foo`
and by `foo-service`, which `KSB_CHART_REPO_PATTERN = "chart-{name}"` resolves.
It also holds a Maven coordinate published by `lib` and by its copy `fork-of-lib`,
which `KSB_FORK_REPOS = "fork-of-lib"` resolves. Each test names the break it
catches.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from unittest import mock

from settings_isolation import SettingsIsolated, store_root
from knowledgestore import config

SRC = Path(__file__).resolve().parents[1] / "src"
SETTINGS = Path("config") / "knowledgestore.toml"
# Token-shaped, built so the source never holds the literal (as test_secret_mask does).
GITHUB_TOKEN = "gh" + "p_" + "x" * 36


def _chart(name: str, *deps: str) -> str:
    text = f"apiVersion: v2\nname: {name}\nversion: 0.1.0\n"
    if deps:
        text += "dependencies:\n" + "".join(f"  - name: {d}\n    version: 1.0.0\n" for d in deps)
    return text


def _pom(artifact: str, *deps: str) -> str:
    uses = "".join(
        f"<dependency><groupId>com.example</groupId><artifactId>{d}</artifactId>"
        "<version>1.0</version></dependency>"
        for d in deps
    )
    return (
        "<project><groupId>com.example</groupId>"
        f"<artifactId>{artifact}</artifactId><version>1.0</version>"
        f"<dependencies>{uses}</dependencies></project>\n"
    )


ESTATE = {
    "app-a": {"Chart.yaml": _chart("a")},
    "app-m": {"a/Chart.yaml": _chart("a", "a"), "z/Chart.yaml": _chart("z", "z")},
    "app-z": {"Chart.yaml": _chart("z")},
    "consumer": {"Chart.yaml": _chart("consumer", "a", "z")},
    "uses-foo": {"Chart.yaml": _chart("uses-foo", "foo")},
    "chart-foo": {"Chart.yaml": _chart("foo")},
    "foo-service": {"Chart.yaml": _chart("foo")},
    "lib": {"pom.xml": _pom("lib")},
    "fork-of-lib": {"pom.xml": _pom("lib")},
    "uses-lib": {"pom.xml": _pom("uses-lib", "lib")},
}
# Each setting the file is tested with, as the environment would give it.
UMBRELLA = {"KSB_UMBRELLA_REPOS": "app-m"}
PATTERN = {"KSB_CHART_REPO_PATTERN": "chart-{name}"}
FORKS = {"KSB_FORK_REPOS": "fork-of-lib"}


def _toml(settings: dict[str, str]) -> str:
    """`settings` as the file a maintainer writes, values in TOML basic strings."""
    return "[settings]\n" + "".join(f'{key} = "{value}"\n' for key, value in settings.items())


def _store(root: Path, settings: str | None = None) -> Path:
    for clone, files in ESTATE.items():
        (root / "repositories" / clone / ".git").mkdir(parents=True)
        for rel, text in files.items():
            path = root / "repositories" / clone / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    if settings is not None:
        (root / "config").mkdir(exist_ok=True)
        (root / SETTINGS).write_text(settings, encoding="utf-8")
    return root


def _layer(root: Path) -> dict[str, bytes]:
    base = root / "knowledge" / "relationships"
    return {
        str(path.relative_to(base)): path.read_bytes()
        for path in sorted(base.rglob("*"))
        if path.is_file()
    }


def run_cli(cwd: Path, *argv: str, **settings: str) -> subprocess.CompletedProcess[str]:
    """`python -m knowledgestore ARGV` in a fresh interpreter, from `cwd`, with no
    `KSB_*` in its environment but `settings` - so the parent's shell cannot leak in."""
    env = {name: value for name, value in os.environ.items() if not name.startswith("KSB_")}
    env.update(PYTHONPATH=str(SRC), **settings)
    return subprocess.run(
        [sys.executable, "-B", "-m", "knowledgestore", *argv],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


class TheFileSetsWhatTheEnvironmentWould(SettingsIsolated):
    def test_a_file_setting_changes_the_layer_exactly_as_the_environment_variable_does(self):
        """Break caught: the file not read, read and ignored, or read only from the
        working directory - so `--root`, the way the guides run a stage, builds the
        poorer layer this issue is about. `chart-{name}` is the value a `.env` file
        could not carry without a quoting trap; here it must arrive whole."""
        bare = _store(store_root(self))
        self.assertEqual(run_cli(bare, "relationships").returncode, 0)
        for setting in (UMBRELLA, PATTERN, FORKS):
            from_env = _store(store_root(self))
            result = run_cli(from_env, "relationships", **setting)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotEqual(_layer(from_env), _layer(bare), f"{setting} must change the layer")
            for route in ("working directory", "--root"):
                with self.subTest(setting=setting, route=route):
                    root = _store(store_root(self), _toml(setting))
                    if route == "--root":
                        result = run_cli(store_root(self), "--root", str(root), "relationships")
                    else:
                        result = run_cli(root, "relationships")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(_layer(root), _layer(from_env))

    def test_the_chart_pattern_arrives_exactly_as_written(self):
        """Break caught: `chart-{name}` mangled on the way in - braces, quotes or
        anything else - so the pattern names no clone and the chart stays unresolved."""
        root = _store(store_root(self), _toml(PATTERN))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KSB_CHART_REPO_PATTERN", None)
            config.configure(root=root)
            self.assertEqual(config.CHART_REPO_PATTERN, "chart-{name}")

    def test_the_environment_overrides_the_file(self):
        """Break caught: the file winning, so CI and a one-off override cannot change a
        setting the store committed."""
        bare = _store(store_root(self))
        run_cli(bare, "relationships")
        root = _store(store_root(self), _toml(UMBRELLA))
        result = run_cli(root, "relationships", KSB_UMBRELLA_REPOS="not-a-wrapper")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(_layer(root), _layer(bare))

    def test_a_value_is_the_string_toml_reads(self):
        """Break caught: a `$` expanded, a `#` inside a string read as a comment, or
        the value trimmed, so the file means something other than what it says."""
        root = store_root(self)
        (root / "config").mkdir()
        (root / SETTINGS).write_text(
            '# the explorer\'s title\n\n[settings]\nKSB_EXPLORER_TITLE = " Estate $HOME #1 "\n',
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KSB_EXPLORER_TITLE", None)
            config.configure(root=root)
            self.assertEqual(config.EXPLORER_TITLE, " Estate $HOME #1 ")


class ConfigureFollowsTheRoot(SettingsIsolated):
    def test_a_new_root_reads_its_own_file_and_drops_the_last_ones(self):
        """Break caught: a setting from one store's file surviving a move to a store
        without one, so a programmatic caller building two stores builds the second
        with the first one's settings."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KSB_EXPLORER_TITLE", None)
            os.environ.pop("KSB_MIN_ENTRY_DEGREE", None)
            first = store_root(self)
            (first / "config").mkdir()
            (first / SETTINGS).write_text(
                _toml({"KSB_EXPLORER_TITLE": "First", "KSB_MIN_ENTRY_DEGREE": "7"}),
                encoding="utf-8",
            )
            config.configure(root=first)
            self.assertEqual((config.EXPLORER_TITLE, config.MIN_ENTRY_DEGREE), ("First", 7))
            config.configure(root=store_root(self))
            self.assertEqual(
                (config.EXPLORER_TITLE, config.MIN_ENTRY_DEGREE), ("Estate Explorer", 3)
            )


class TheFileIsRefused(SettingsIsolated):
    def refused(self, text: str) -> subprocess.CompletedProcess[str]:
        root = _store(store_root(self), text)
        result = run_cli(root, "relationships")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse((root / "knowledge").exists(), "a refused file must stop the stage")
        return result

    def test_an_unknown_key_fails_naming_it(self):
        """Break caught: a misspelt setting accepted and ignored, which is the silent
        poorer layer again with a file to point at."""
        result = self.refused(_toml({"KSB_UMBRELA_REPOS": "app-m"}))
        self.assertIn("KSB_UMBRELA_REPOS", result.stderr)
        self.assertIn("not a setting", result.stderr)

    def test_a_key_outside_the_library_fails(self):
        """Break caught: the file used as a general environment file, so anything
        in it reaches nothing and nobody learns."""
        stderr = self.refused(_toml({"PATH": "/tmp"})).stderr
        self.assertIn("PATH", stderr)
        self.assertIn("only KSB_* settings belong here", stderr)

    def test_a_secret_looking_key_fails_even_when_the_library_reads_it(self):
        """Break caught: a credential committed with the store. `KSB_TRACKER_TOKEN` is
        a real setting, so being known is not enough."""
        for key in ("KSB_TRACKER_TOKEN", "KSB_DEPLOY_KEY", "KSB_X_SECRET", "KSB_DB_PASSWORD"):
            with self.subTest(key=key):
                result = self.refused(_toml({key: "placeholder"}))
                self.assertIn(key, result.stderr)
                self.assertIn("secret", result.stderr)

    def test_a_secret_shaped_value_fails_without_printing_it(self):
        """Break caught: a token pasted under an innocent key committed with the store,
        or the refusal echoing the token into a log."""
        result = self.refused(_toml({"KSB_EXPLORER_TITLE": GITHUB_TOKEN}))
        self.assertIn("KSB_EXPLORER_TITLE", result.stderr)
        self.assertNotIn(GITHUB_TOKEN, result.stderr + result.stdout)

    def test_ksb_root_in_the_file_is_refused(self):
        """Break caught: the file naming another root, which cannot work - the root
        is what says where the file is - and would be honoured by nothing."""
        result = self.refused(_toml({"KSB_ROOT": "/elsewhere"}))
        self.assertIn("KSB_ROOT", result.stderr)

    def test_anything_outside_the_settings_table_is_refused_by_name(self):
        """Break caught: a second table, or a key above `[settings]`, read and ignored -
        the misplaced setting is then as absent as one never written."""
        for text, named in (
            ('[other]\nKSB_UMBRELLA_REPOS = "app-m"\n', "other is outside [settings]"),
            ('KSB_UMBRELLA_REPOS = "app-m"\n', "KSB_UMBRELLA_REPOS is outside [settings]"),
            ('settings = "app-m"\n', "settings must be the [settings] table"),
        ):
            with self.subTest(text=text):
                self.assertIn(named, self.refused(text).stderr)

    def test_a_value_that_is_not_a_string_is_refused_by_name(self):
        """Break caught: a number or a list passed on as something the setting never
        receives from the environment, or dropped."""
        for value, kind in (("7", "int"), ('["app-m"]', "list"), ("true", "bool")):
            with self.subTest(value=value):
                stderr = self.refused(f"[settings]\nKSB_UMBRELLA_REPOS = {value}\n").stderr
                self.assertIn(f"KSB_UMBRELLA_REPOS must be a string, not a TOML {kind}", stderr)

    def test_a_file_toml_cannot_read_is_refused(self):
        """Break caught: a `.env`-style line accepted, or the parser's error escaping
        as a traceback that does not name the file."""
        stderr = self.refused("[settings]\nKSB_UMBRELLA_REPOS=app-m\n").stderr
        self.assertIn(f"{SETTINGS}: not valid TOML", stderr)


class TheFileReadIsReported(SettingsIsolated):
    def test_version_and_status_name_the_file_and_its_keys_never_its_values(self):
        """Break caught: no way to tell from a build log whether the store's settings
        were in force; or a value printed, which turns the log into a copy of it."""
        root = _store(
            store_root(self),
            _toml({"KSB_EXPLORER_TITLE": "Distinctive title", **UMBRELLA}),
        )
        for argv in (("--version",), ("status",)):
            with self.subTest(argv=argv):
                result = run_cli(root, *argv, KSB_UMBRELLA_REPOS="app-m")
                self.assertEqual(result.returncode, 0, result.stderr)
                line = next(
                    line for line in result.stdout.splitlines() if str(root / SETTINGS) in line
                )
                self.assertIn(
                    "set KSB_EXPLORER_TITLE; the environment overrides KSB_UMBRELLA_REPOS", line
                )
                self.assertNotIn("Distinctive", result.stdout)

    def test_a_store_without_the_file_says_so(self):
        """Break caught: the line missing when there is no file, so its absence in a
        log cannot be told from an older library that never looked."""
        root = store_root(self)
        result = run_cli(root, "--version")
        self.assertIn(f"settings none ({root / SETTINGS} does not exist)", result.stdout)


class TheAllowedKeysAreDerived(SettingsIsolated):
    def test_the_derivation_finds_every_key_config_reads(self):
        """Break caught: a setting the derivation misses, which the file would refuse
        as unknown although the library reads it. Held against every quoted `KSB_*`
        literal in config.py, and against names known by hand."""
        keys = config.settings_keys()
        for known in (
            "KSB_UMBRELLA_REPOS",
            "KSB_FORK_REPOS",
            "KSB_CHART_REPO_PATTERN",
            "KSB_GITHUB_ORG",
            "KSB_INDEX_WARN_MB",
            "KSB_TICKET_PATTERN",
            "KSB_SECRET_PATTERNS",
            "KSB_TRACKER_FETCH_COMMENTS",
            "KSB_DIVE_TOP_FILES",
        ):
            self.assertIn(known, keys)
        source = (SRC / "knowledgestore" / "config.py").read_text(encoding="utf-8")
        self.assertEqual(keys, frozenset(re.findall(r'"(KSB_[A-Z0-9_]+)"', source)))
