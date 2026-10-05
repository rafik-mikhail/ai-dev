"""Tests for the emkit installer: packaging, `init`, `doctor` and the uvx entry point.

Run from the repository root:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path, PurePosixPath, PureWindowsPath

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
PKG = SRC / "emkit"
sys.path.insert(0, str(SRC))

from emkit import __version__, cli  # noqa: E402

HAVE_GIT = shutil.which("git") is not None
HAVE_UVX = shutil.which("uvx") is not None
EXPECTED_FILES = {
    ".study/kernel.py", ".study/PROTOCOL.md", ".study/schema.json", ".study/VERSION",
    ".study/templates/system.md", ".study/templates/flow.md", ".study/templates/finding.md",
    ".study/templates/run.md", ".study/AGENTS.md", "AGENTS.md",
    ".claude/skills/engineering-study/SKILL.md", ".agents/skills/engineering-study/SKILL.md",
}
EXPECTED_DIRS = {".study", ".study/templates", ".study/systems", ".study/flows", ".study/findings",
                 ".study/runs", ".study/scratch", ".claude", ".claude/skills", ".claude/skills/engineering-study",
                 ".agents", ".agents/skills", ".agents/skills/engineering-study"}


def git(root, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(root)] + list(args),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def snapshot(root, skip=()):
    """{relative posix path: sha256} for files, plus directories as None."""
    out = {}
    root = Path(root)
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if any(rel == s or rel.startswith(s + "/") for s in skip):
            continue
        out[rel] = sha(p) if p.is_file() else None
    return out


def child_env(**extra):
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.update(extra)
    return env


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="emkit-test-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()

    def emkit(self, *args, cwd=None, env=None, check=None):
        """Run `python -m emkit ...` from the source tree in a fresh interpreter."""
        e = child_env(PYTHONPATH=str(SRC))
        if env:
            e.update(env)
        proc = subprocess.run([sys.executable, "-m", "emkit"] + [str(a) for a in args], cwd=str(cwd or self.tmp),
                              env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if check is True:
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        if check is False:
            self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def init(self, path=None, *flags):
        return self.emkit("init", str(path or self.repo), *flags, check=True)

    def make_git_repo(self):
        if not HAVE_GIT:
            self.skipTest("git not available")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
        (self.repo / "README.md").write_text("# demo\n", encoding="utf-8")
        git(self.repo, "init", "-q")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "init")

    def kernel(self, *args, cwd=None):
        return subprocess.run([sys.executable, str(self.repo / ".study" / "kernel.py")] + [str(a) for a in args],
                              cwd=str(cwd or self.repo), env=child_env(), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True)


class TestPackaging(unittest.TestCase):
    """Builds the wheel once, then installs it into a throwaway virtualenv."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="emkit-wheel-"))
        cls.skip_reason = None
        src = cls.tmp / "src-copy"
        shutil.copytree(str(ROOT), str(src), ignore=shutil.ignore_patterns(
            ".git", "__pycache__", "build", "dist", "*.egg-info", ".venv", "venv", "*.zip"))
        out = cls.tmp / "wheelhouse"
        out.mkdir()
        attempts = (["-m", "pip", "wheel", ".", "--no-deps", "-w", str(out), "-q"],
                    ["-m", "pip", "wheel", ".", "--no-deps", "--no-build-isolation", "-w", str(out), "-q"])
        errors = []
        for argv in attempts:
            proc = subprocess.run([sys.executable] + argv, cwd=str(src), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, universal_newlines=True)
            if proc.returncode == 0 and list(out.glob("*.whl")):
                break
            errors.append(proc.stderr[-800:])
        else:
            cls.skip_reason = "cannot build a wheel here (needs setuptools>=69 from an index or installed): %s" % (
                errors[-1] if errors else "")
            return
        cls.wheel = next(out.glob("*.whl"))
        cls.venv = cls.tmp / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(cls.venv)], check=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)
        bindir = cls.venv / ("Scripts" if os.name == "nt" else "bin")
        cls.venv_python = bindir / ("python.exe" if os.name == "nt" else "python")
        cls.emkit_exe = bindir / ("emkit.exe" if os.name == "nt" else "emkit")
        proc = subprocess.run([str(cls.venv_python), "-m", "pip", "install", "--no-index", "--no-deps", "-q",
                               str(cls.wheel)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True)
        if proc.returncode != 0:
            cls.skip_reason = "cannot install the wheel into a venv: " + proc.stderr[-500:]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(str(cls.tmp), True)

    def setUp(self):
        if self.skip_reason:
            self.skipTest(self.skip_reason)

    def test_01_wheel_builds(self):
        self.assertTrue(self.wheel.name.startswith("engineering_memory_kit-%s-" % __version__), self.wheel.name)
        self.assertTrue(self.wheel.name.endswith("-py3-none-any.whl"), self.wheel.name)

    def test_02_wheel_contains_every_resource_once(self):
        with zipfile.ZipFile(str(self.wheel)) as z:
            names = z.namelist()
            for required in ("emkit/__init__.py", "emkit/__main__.py", "emkit/cli.py", "emkit/kernel.py",
                             "emkit/resources/PROTOCOL.md", "emkit/resources/AGENTS.md", "emkit/resources/SKILL.md",
                             "emkit/resources/schema.json", "emkit/resources/templates/system.md",
                             "emkit/resources/templates/flow.md", "emkit/resources/templates/finding.md",
                             "emkit/resources/templates/run.md"):
                self.assertIn(required, names)
            self.assertEqual([n for n in names if n.endswith("kernel.py")], ["emkit/kernel.py"])
            self.assertEqual([n for n in names if n.endswith("PROTOCOL.md")], ["emkit/resources/PROTOCOL.md"])
            self.assertEqual(z.read("emkit/kernel.py"), (PKG / "kernel.py").read_bytes())
            self.assertEqual(json.loads(z.read("emkit/resources/schema.json").decode("utf-8"))["$schema"],
                             "https://json-schema.org/draft/2020-12/schema")
            meta = next(n for n in names if n.endswith(".dist-info/METADATA"))
            text = z.read(meta).decode("utf-8")
            self.assertIn("Name: engineering-memory-kit", text)
            self.assertIn("Version: %s" % __version__, text)
            self.assertIn("Requires-Python: >=3.9", text)
            self.assertNotIn("Requires-Dist", text)
            entry = z.read(next(n for n in names if n.endswith("entry_points.txt"))).decode("utf-8")
            self.assertIn("emkit = emkit.cli:main", entry)

    def test_03_console_script_and_module_report_version(self):
        proc = subprocess.run([str(self.emkit_exe), "--version"], env=child_env(), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, "emkit " + __version__), proc.stderr)
        proc = subprocess.run([str(self.venv_python), "-m", "emkit", "--version"], env=child_env(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, "emkit " + __version__), proc.stderr)

    def test_04_installed_wheel_inits_from_package_data(self):
        target = self.tmp / "from wheel"
        proc = subprocess.run([str(self.emkit_exe), "init", str(target)], env=child_env(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for m in cli.MANAGED:
            self.assertTrue((target / Path(*m.dest.split("/"))).is_file(), m.dest)
        self.assertEqual(sha(target / ".study" / "kernel.py"), sha(PKG / "kernel.py"))
        # emkit doctor from the installed package agrees
        proc = subprocess.run([str(self.emkit_exe), "doctor", str(target)], env=child_env(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(proc.returncode, 0, proc.stdout)


class TestMetadata(unittest.TestCase):
    def test_pyproject_matches_package(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('version = "%s"' % __version__, text)
        self.assertIn('name = "engineering-memory-kit"', text)
        self.assertIn('emkit = "emkit.cli:main"', text)
        self.assertIn("dependencies = []", text)
        self.assertIn('requires-python = ">=3.9"', text)

    def test_readme_leads_with_quick_start(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        headings = [l for l in text.splitlines() if l.startswith("## ")]
        self.assertEqual(headings[0], "## Quick start")
        first = text[text.index("## Quick start"):text.index(headings[1])]
        self.assertIn("uvx --from git+https://github.com/rafik-mikhail/ai-dev@v%s emkit init ." % __version__, first)
        self.assertIn("python .study/kernel.py --help", first)
        for needle in ("uv tool install", "--force", "--dry-run", "source ZIP", "not a read-only sandbox",
                       "--no-agents", "--detect", "codebase add", "emkit:begin", "## Methodology"):
            self.assertIn(needle, text)

    def test_every_managed_resource_is_packaged(self):
        for m in cli.MANAGED:
            if m.source is not None:
                self.assertTrue(PKG.joinpath(*m.source).is_file(), m.source)
        # the root AGENTS.md is deliberately not a managed file: emkit only maintains a marked block in it
        self.assertEqual({m.dest for m in cli.MANAGED}, EXPECTED_FILES - {"AGENTS.md"} - {m.dest for m in cli.SKILL_FILES})


class TestInit(Base):
    def test_05_init_creates_expected_tree(self):
        proc = self.init()
        files = set()
        dirs = set()
        for p in self.repo.rglob("*"):
            rel = p.relative_to(self.repo).as_posix()
            if rel.startswith(".study/study.db"):
                continue
            (files if p.is_file() else dirs).add(rel)
        self.assertEqual(files, EXPECTED_FILES)
        self.assertEqual(dirs, EXPECTED_DIRS)
        self.assertTrue((self.repo / ".study" / "study.db").is_file())
        self.assertEqual((self.repo / ".study" / "VERSION").read_text(encoding="utf-8"), __version__ + "\n")
        for m in cli.MANAGED:
            if m.source:
                self.assertEqual((self.repo / Path(*m.dest.split("/"))).read_bytes(),
                                 PKG.joinpath(*m.source).read_bytes(), m.dest)
        root_agents = (self.repo / "AGENTS.md").read_text(encoding="utf-8")
        self.assertTrue(root_agents.startswith("# AGENTS.md\n\n" + cli.BLOCK_BEGIN + "\n"))
        self.assertTrue(root_agents.endswith(cli.BLOCK_END + "\n"))
        self.assertIn(cli.agents_body(), root_agents)
        for line in (".study/kernel.py", ".study/templates/run.md", "AGENTS.md", "kernel init: ok",
                     "kernel check: ok", "next steps:", "kernel.py commands", "run start --goal"):
            self.assertIn(line, proc.stdout)

    def test_06_installed_kernel_never_imports_emkit(self):
        self.init()
        kernel = self.repo / ".study" / "kernel.py"
        text = kernel.read_text(encoding="utf-8")
        self.assertNotIn("import emkit", text)
        self.assertNotIn("from emkit", text)
        # Block `emkit` outright, run in isolated mode with no PYTHONPATH, and exercise the kernel.
        code = ("import sys, runpy; sys.modules['emkit'] = None; sys.argv = [%r, 'status', '--root', %r]; "
                "runpy.run_path(%r, run_name='__main__')") % (str(kernel), str(self.repo), str(kernel))
        proc = subprocess.run([sys.executable, "-I", "-c", code], env=child_env(), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("documents:", proc.stdout)
        # A real session with the installed copy alone.
        self.assertEqual(self.kernel("run", "start", "--goal", "g").stdout.strip(), "RUN-0001")
        res = self.kernel("new", "system", "app", "--title", "App", "--run", "RUN-0001")
        self.assertEqual(res.returncode, 0, res.stderr)
        res = self.kernel("run", "end", "--id", "RUN-0001", "--summary", "done")
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_07_second_init_overwrites_nothing(self):
        self.init()
        again = self.init()
        self.assertEqual(again.stdout.count("unchanged"), len(EXPECTED_FILES))
        self.assertNotIn("replace", again.stdout)
        (self.repo / ".study" / "PROTOCOL.md").write_text("local edit\n", encoding="utf-8")
        (self.repo / ".study" / "templates" / "flow.md").write_text("tampered\n", encoding="utf-8")
        third = self.emkit("init", self.repo, check=True)
        self.assertEqual((self.repo / ".study" / "PROTOCOL.md").read_text(encoding="utf-8"), "local edit\n")
        self.assertEqual((self.repo / ".study" / "templates" / "flow.md").read_text(encoding="utf-8"), "tampered\n")
        for dest in (".study/PROTOCOL.md", ".study/templates/flow.md"):
            self.assertRegex(third.stdout, r"skip\s+%s " % dest.replace(".", r"\."))
        self.assertIn("use --force", third.stdout)

    def test_08_force_replaces_managed_and_keeps_everything_else(self):
        self.init()
        self.assertEqual(self.kernel("run", "start", "--goal", "g").returncode, 0)
        self.assertEqual(self.kernel("new", "system", "app", "--title", "Unique Quokka Title",
                                     "--run", "RUN-0001").returncode, 0)
        (self.repo / ".study" / "notes.txt").write_text("keep me\n", encoding="utf-8")
        (self.repo / ".study" / "templates" / "custom.md").write_text("mine\n", encoding="utf-8")
        (self.repo / "unrelated.txt").write_text("source\n", encoding="utf-8")
        for rel in (".study/kernel.py", ".study/PROTOCOL.md", ".study/templates/system.md", ".study/AGENTS.md",
                    ".study/VERSION"):
            (self.repo / Path(*rel.split("/"))).write_text("broken\n", encoding="utf-8")
        agents = self.repo / "AGENTS.md"  # tamper inside the block, and keep text around it
        agents.write_text("before\n" + agents.read_text(encoding="utf-8").replace("Study mode only", "WRONG")
                          + "\nafter\n", encoding="utf-8")
        keep = {rel: sha(self.repo / rel) for rel in
                (".study/notes.txt", ".study/templates/custom.md", "unrelated.txt",
                 ".study/systems/app.md", ".study/runs/RUN-0001/summary.md",
                 ".study/runs/RUN-0001/events.jsonl", ".study/runs/RUN-0001/evidence.jsonl")}
        proc = self.emkit("init", self.repo, "--force", check=True)
        self.assertEqual(proc.stdout.count("replace "), 6, proc.stdout)
        for m in cli.MANAGED:
            self.assertEqual(cli.file_state(self.repo, m), "same", m.dest)
        text = agents.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("before\n# AGENTS.md"))
        self.assertTrue(text.endswith("\nafter\n"))
        self.assertNotIn("WRONG", text)
        self.assertEqual(cli.agents_plan(self.repo)[0], "same")
        for rel, digest in keep.items():
            self.assertEqual(sha(self.repo / rel), digest, rel)
        found = self.kernel("search", "quokka")
        self.assertIn("SYS-app", found.stdout)
        self.assertEqual(self.kernel("check").returncode, 0)
        self.assertEqual([p.name for p in (self.repo / ".study").iterdir() if p.name.startswith(".emkit-tmp")], [])

    def test_09_dry_run_writes_nothing(self):
        self.repo.joinpath("keep.txt").write_text("x\n", encoding="utf-8")
        before = snapshot(self.tmp)
        proc = self.emkit("init", self.repo, "--dry-run", check=True)
        self.assertEqual(snapshot(self.tmp), before)
        self.assertIn("would create    .study/kernel.py", proc.stdout)
        self.assertIn("dry run: nothing written", proc.stdout)
        missing = self.tmp / "does" / "not" / "exist"
        proc = self.emkit("init", missing, "--dry-run", check=True)
        self.assertFalse((self.tmp / "does").exists())
        self.assertIn("would create directory", proc.stdout)
        self.init()
        before = snapshot(self.tmp)
        (self.repo / ".study" / "PROTOCOL.md").write_text("edit\n", encoding="utf-8")
        before = snapshot(self.tmp)
        proc = self.emkit("init", self.repo, "--dry-run", "--force", check=True)
        self.assertIn("would replace   .study/PROTOCOL.md", proc.stdout)
        self.assertEqual(snapshot(self.tmp), before)

    def test_10_destination_with_spaces_and_unicode(self):
        for name in ("my project", "caf\u00e9 \u00fcber repo"):
            target = self.tmp / name / "inner dir"
            proc = self.emkit("init", target)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertTrue((target / ".study" / "kernel.py").is_file())
            res = subprocess.run([sys.executable, str(target / ".study" / "kernel.py"), "status"], cwd=str(target),
                                 env=child_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 universal_newlines=True)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertEqual(self.emkit("doctor", target).returncode, 0)

    def test_11_missing_git_does_not_block_init_or_doctor(self):
        empty = self.tmp / "emptybin"
        empty.mkdir()
        env = {"PATH": str(empty)}
        if os.name == "nt":
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
        proc = self.emkit("init", self.repo, env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        doc = self.emkit("doctor", self.repo, env=env)
        self.assertEqual(doc.returncode, 0, doc.stdout)
        self.assertIn("git not found", doc.stdout)
        # Also a plain directory that is simply not a repository.
        plain = self.tmp / "plain"
        self.assertEqual(self.emkit("init", plain).returncode, 0)
        self.assertIn("not a Git repository", self.emkit("doctor", plain).stdout if HAVE_GIT else "not a Git repository")

    def test_12_path_forms_and_separators(self):
        proj = self.tmp / "proj"
        (proj / "sub").mkdir(parents=True)
        for arg in (".", "./", "sub/..", "."):
            proc = self.emkit("init", arg, cwd=proj)
            self.assertEqual(proc.returncode, 0, (arg, proc.stderr))
        self.assertEqual(self.emkit("init", "sub", cwd=proj).returncode, 0)
        self.assertTrue((proj / "sub" / ".study" / "kernel.py").is_file())
        self.assertEqual(self.emkit("doctor", cwd=proj).returncode, 0)  # default PATH is the cwd
        out = self.emkit("init", proj, "--dry-run").stdout
        self.assertNotIn("\\", out.replace("\\\\", ""), "reported paths must use '/' on every OS")
        # Path joining is OS-independent: build destinations from POSIX parts onto either flavour.
        self.assertEqual(cli.local_path(PurePosixPath("/a b/c"), ".study/kernel.py").as_posix(),
                         "/a b/c/.study/kernel.py")
        win = cli.local_path(PureWindowsPath("C:/Users/me/my repo"), ".study/templates/run.md")
        self.assertEqual(str(win), "C:\\Users\\me\\my repo\\.study\\templates\\run.md")
        for m in cli.MANAGED:
            self.assertNotIn("\\", m.dest)
            self.assertFalse(m.dest.startswith("/"))

    def test_13_init_rejects_unsafe_layouts(self):
        afile = self.tmp / "afile"
        afile.write_text("x", encoding="utf-8")
        self.assertNotEqual(self.emkit("init", afile).returncode, 0)
        outside = self.tmp / "outside"
        outside.mkdir()
        try:
            os.symlink(str(outside), str(self.repo / ".study"))
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("cannot create symlinks")
        proc = self.emkit("init", self.repo)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("symlink", proc.stderr)
        self.assertEqual(list(outside.iterdir()), [])
        os.unlink(str(self.repo / ".study"))
        (self.repo / ".study").mkdir()
        try:
            os.symlink(str(outside / "target.txt"), str(self.repo / "AGENTS.md"))
        except (OSError, NotImplementedError):
            self.skipTest("cannot create symlinks")
        proc = self.emkit("init", self.repo, "--force")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(list(outside.iterdir()), [])

    def test_14_install_touches_nothing_outside_study_and_agents(self):
        self.make_git_repo()
        (self.repo / "src" / "app.py").write_text("def main():\n    return 2  # uncommitted\n", encoding="utf-8")
        (self.repo / "untracked.txt").write_text("u\n", encoding="utf-8")
        sibling = self.tmp / "sibling.txt"
        sibling.write_text("s\n", encoding="utf-8")
        before = snapshot(self.tmp, skip=("repo/.study", "repo/AGENTS.md", "repo/.git/info/exclude",
                                              "repo/.claude", "repo/.agents"))
        index_before = (self.repo / ".git" / "index").read_bytes()
        exclude = self.repo / ".git" / "info" / "exclude"
        exclude_before = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        self.init()
        after = snapshot(self.tmp, skip=("repo/.study", "repo/AGENTS.md", "repo/.git/info/exclude",
                                         "repo/.claude", "repo/.agents"))
        self.assertEqual(after, before)
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), index_before)
        added = exclude.read_text(encoding="utf-8")[len(exclude_before):]
        self.assertEqual(set(l for l in added.splitlines() if l.strip()),
                         {".study/", ".study/study.db", ".study/study.db-wal", ".study/study.db-shm",
                          ".study/scratch/", ".claude/skills/engineering-study/",
                          ".agents/skills/engineering-study/"})
        status = git(self.repo, "status", "--porcelain").stdout.decode().splitlines()
        self.assertEqual(sorted(l[3:] for l in status), ["AGENTS.md", "src/app.py", "untracked.txt"])

    def exclude_lines(self):
        path = self.repo / ".git" / "info" / "exclude"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else None

    def test_14b_exclude_is_created_when_missing(self):
        self.make_git_repo()
        shutil.rmtree(str(self.repo / ".git" / "info"), True)
        proc = self.init()
        self.assertIn("create .git/info/exclude: .study/", proc.stdout)
        self.assertEqual(self.exclude_lines().count(".study/"), 1)
        self.assertNotIn(".study/", git(self.repo, "status", "--porcelain").stdout.decode())

    def test_14c_exclude_is_appended_not_rewritten_and_idempotent(self):
        self.make_git_repo()
        exclude = self.repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(exist_ok=True)
        exclude.write_text("# mine\n*.log", encoding="utf-8")  # no trailing newline
        first = self.init()
        self.assertIn("append to .git/info/exclude: .study/", first.stdout)
        lines = self.exclude_lines()
        self.assertEqual(lines[:2], ["# mine", "*.log"])
        self.assertEqual(lines.count(".study/"), 1)
        snap = exclude.read_bytes()
        again = self.init()
        self.assertNotIn("exclude", again.stdout)
        self.assertEqual(exclude.read_bytes(), snap)
        # an existing entry written by hand is respected
        exclude.write_text(".study/\n", encoding="utf-8")
        self.init()
        self.assertEqual(self.exclude_lines().count(".study/"), 1)

    def test_14d_dry_run_reports_exclude_but_writes_nothing(self):
        self.make_git_repo()
        before = (self.repo / ".git" / "info" / "exclude").read_bytes() if (
            self.repo / ".git" / "info" / "exclude").exists() else None
        proc = self.emkit("init", self.repo, "--dry-run", check=True)
        self.assertIn("exclude: .study/", proc.stdout)
        path = self.repo / ".git" / "info" / "exclude"
        self.assertEqual(path.read_bytes() if path.exists() else None, before)

    def test_14e_no_exclude_outside_git(self):
        proc = self.init()
        self.assertNotIn("exclude", proc.stdout)
        self.assertFalse((self.repo / ".git").exists())

    def test_15_missing_support_files_make_init_fail_loudly(self):
        # emkit reports incomplete installs: simulate a kernel that cannot initialize.
        self.init()
        kernel = self.repo / ".study" / "kernel.py"
        kernel.write_text("import sys\nsys.stderr.write('boom')\nsys.exit(3)\n", encoding="utf-8")
        proc = self.emkit("init", self.repo)  # kept (differs), then run: it fails
        self.assertEqual(proc.returncode, 1)
        self.assertIn("kernel init failed", proc.stderr)
        self.assertIn("installation incomplete", proc.stderr)


class TestAgentsFile(Base):
    """The root AGENTS.md belongs to the repository: emkit only adds or refreshes one marked block."""

    def agents(self):
        return self.repo / "AGENTS.md"

    def test_a1_existing_file_gets_the_block_appended_and_nothing_else_changes(self):
        original = "# Team rules\n\nRun tests before pushing.\n"
        self.agents().write_text(original, encoding="utf-8")
        proc = self.init()
        self.assertIn("append    AGENTS.md", proc.stdout)
        text = self.agents().read_text(encoding="utf-8")
        self.assertTrue(text.startswith(original))
        self.assertEqual(text.count(cli.BLOCK_BEGIN), 1)
        self.assertIn(cli.agents_body(), text)
        self.assertNotIn("# AGENTS.md", text)  # no second title injected into an existing file
        snap = self.agents().read_bytes()
        again = self.init()
        self.assertIn("unchanged AGENTS.md", again.stdout)
        self.assertEqual(self.agents().read_bytes(), snap)

    def test_a2_missing_trailing_newline_and_empty_file(self):
        self.agents().write_text("no newline at end", encoding="utf-8")
        self.init()
        text = self.agents().read_text(encoding="utf-8")
        self.assertTrue(text.startswith("no newline at end\n\n" + cli.BLOCK_BEGIN))
        self.agents().write_text("", encoding="utf-8")
        self.init(self.repo, "--force")
        self.assertTrue(self.agents().read_text(encoding="utf-8").startswith(cli.BLOCK_BEGIN))

    def test_a3_crlf_files_stay_crlf(self):
        self.agents().write_bytes(b"# Rules\r\nBe kind.\r\n")
        self.init()
        raw = self.agents().read_bytes()
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        self.assertIn(cli.BLOCK_BEGIN.encode() + b"\r\n", raw)
        self.assertIn("unchanged AGENTS.md", self.init().stdout)

    def test_a4_tampered_block_needs_force_and_only_the_block_is_replaced(self):
        self.agents().write_text("top\n", encoding="utf-8")
        self.init()
        text = self.agents().read_text(encoding="utf-8")
        edited = "intro\n" + text.replace("Study mode only", "CHANGED") + "outro\n"
        self.agents().write_text(edited, encoding="utf-8")
        skipped = self.init()
        self.assertIn("skip      AGENTS.md (Study block differs", skipped.stdout)
        self.assertEqual(self.agents().read_text(encoding="utf-8"), edited)
        forced = self.init(self.repo, "--force")
        self.assertIn("replace   AGENTS.md (Study block only)", forced.stdout)
        fixed = self.agents().read_text(encoding="utf-8")
        self.assertTrue(fixed.startswith("intro\ntop\n"))
        self.assertTrue(fixed.endswith("outro\n"))
        self.assertNotIn("CHANGED", fixed)

    def test_a5_no_agents_leaves_the_root_file_alone(self):
        self.init(self.repo, "--no-agents")
        self.assertFalse(self.agents().exists())
        self.assertTrue((self.repo / ".study" / "AGENTS.md").is_file())
        self.agents().write_text("mine\n", encoding="utf-8")
        proc = self.init(self.repo, "--no-agents", "--force")
        self.assertIn("--no-agents", proc.stdout)
        self.assertEqual(self.agents().read_text(encoding="utf-8"), "mine\n")

    def test_a6_unbalanced_markers_and_binary_files_are_never_touched(self):
        for payload in (("x\n" + cli.BLOCK_BEGIN + "\nhalf\n").encode(),
                        (cli.BLOCK_END + "\n" + cli.BLOCK_BEGIN + "\n").encode(),
                        b"\xff\xfe\x00binary"):
            self.agents().write_bytes(payload)
            for flags in ((), ("--force",)):
                proc = self.init(self.repo, *flags)
                self.assertRegex(proc.stdout, r"skip\s+AGENTS.md")
                self.assertEqual(self.agents().read_bytes(), payload)

    def test_a7_dry_run_reports_without_writing(self):
        self.agents().write_text("mine\n", encoding="utf-8")
        proc = self.emkit("init", self.repo, "--dry-run", check=True)
        self.assertIn("would append    AGENTS.md", proc.stdout)
        self.assertEqual(self.agents().read_text(encoding="utf-8"), "mine\n")

    def test_a8_doctor_warns_when_agents_cannot_see_the_rules(self):
        self.init()
        self.agents().write_text("replaced by the owner\n", encoding="utf-8")
        res = self.emkit("doctor", self.repo)
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertIn("AGENTS.md has no Study block", res.stdout)
        self.agents().unlink()
        self.assertIn("no AGENTS.md", self.emkit("doctor", self.repo).stdout)


class TestWorkspaceInstall(Base):
    """`emkit init` in a folder that holds several repositories."""

    def setUp(self):
        super().setUp()
        if not HAVE_GIT:
            self.skipTest("git not available")
        for name in ("api", "web"):
            (self.repo / name / "src").mkdir(parents=True)
            (self.repo / name / "src" / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
            git(self.repo / name, "init", "-q")
            git(self.repo / name, "add", "-A")
            git(self.repo / name, "commit", "-qm", "init")
        (self.repo / "notes").mkdir()

    def registered(self):
        path = self.repo / ".study" / "codebases.json"
        return [e["path"] for e in json.loads(path.read_text(encoding="utf-8"))["codebases"]] if path.exists() else []

    def test_w1_detect_registers_every_repository(self):
        proc = self.init(self.repo, "--detect")
        self.assertIn("registered api", proc.stdout)
        self.assertEqual(self.registered(), ["api", "web"])
        self.assertTrue((self.repo / "AGENTS.md").is_file())  # created at the workspace root, which has none
        doc = self.emkit("doctor", self.repo)
        self.assertEqual(doc.returncode, 0, doc.stdout)
        self.assertIn("workspace mode: 2 registered codebase(s): api, web", doc.stdout)
        self.assertNotIn("not a Git repository", doc.stdout)

    def test_w2_codebase_flag_is_relative_to_the_workspace_and_repeatable(self):
        proc = self.emkit("init", self.repo, "--codebase", "api", "--codebase", self.repo / "web", cwd=self.tmp)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.registered(), ["api", "web"])
        again = self.emkit("init", self.repo, "--codebase", "api", check=True)
        self.assertIn("already registered api", again.stdout)

    def test_w3_bad_codebase_fails_the_install_loudly(self):
        proc = self.emkit("init", self.repo, "--codebase", "notes")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("codebase registration failed", proc.stderr)
        proc = self.emkit("init", self.repo, "--codebase", "nope")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not found", proc.stderr)
        outside = self.tmp / "outside"
        outside.mkdir()
        proc = self.emkit("init", self.repo, "--codebase", outside)
        self.assertEqual(proc.returncode, 1)

    def test_w4_dry_run_registers_nothing_and_force_keeps_the_registry(self):
        proc = self.emkit("init", self.repo, "--detect", "--dry-run", check=True)
        self.assertIn("would register", proc.stdout)
        self.assertFalse((self.repo / ".study").exists())
        self.init(self.repo, "--detect")
        self.init(self.repo, "--force")
        self.assertEqual(self.registered(), ["api", "web"])

    def test_w5_study_session_end_to_end_across_two_repositories(self):
        self.init(self.repo, "--detect")
        k = lambda *a: self.kernel(*a)
        self.assertEqual(k("run", "start", "--goal", "g").stdout.strip(), "RUN-0001")
        self.assertEqual(k("new", "system", "app", "--title", "App", "--run", "RUN-0001").returncode, 0)
        for repo in ("api", "web"):
            res = k("anchor", "add", "SYS-app", repo + "/src/app.py", "--symbol", "main", "--run", "RUN-0001")
            self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(k("run", "end", "--id", "RUN-0001", "--summary", "done").returncode, 0)
        # the studied repositories were not modified: still clean
        for repo in ("api", "web"):
            self.assertEqual(git(self.repo / repo, "status", "--porcelain").stdout, b"")


class TestSkills(Base):
    """The packaged SKILL.md (open Agent Skills format) is installed next to the AGENTS.md block."""

    DIRS = (".claude/skills/engineering-study", ".agents/skills/engineering-study")

    def skill(self, d):
        return self.repo / Path(*d.split("/")) / "SKILL.md"

    def test_s1_installed_in_both_locations_and_valid(self):
        self.make_git_repo()
        proc = self.init()
        for d in self.DIRS:
            self.assertIn("create    %s/SKILL.md" % d, proc.stdout)
            text = self.skill(d).read_text(encoding="utf-8")
            self.assertTrue(text.startswith("---\nname: engineering-study\ndescription: "), d)
            head, _, body = text[4:].partition("\n---\n")
            fields = dict(line.split(": ", 1) for line in head.splitlines())
            self.assertEqual(fields["name"], Path(d).name)  # the spec: name matches the directory
            self.assertTrue(re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", fields["name"]))
            self.assertLessEqual(len(fields["name"]), 64)
            self.assertTrue(0 < len(fields["description"]) <= 1024)
            self.assertIn(cli.agents_body(), body)  # one source: the same block as AGENTS.md
            self.assertNotIn("{{", text)
        self.assertEqual(self.skill(self.DIRS[0]).read_bytes(), self.skill(self.DIRS[1]).read_bytes())

    def test_s2_agents_text_teaches_commands_and_tool_calls(self):
        body = cli.agents_body()
        self.assertIn("kernel.py commands", body)
        self.assertIn("study_*", body)
        self.assertIn("tools call", body)
        self.assertIn("tools list", body)
        self.assertNotIn("kernel.py --help", body)

    def test_s3_git_tree_stays_clean_apart_from_agents_md(self):
        self.make_git_repo()
        self.init()
        status = git(self.repo, "status", "--porcelain").stdout.decode().strip().splitlines()
        self.assertEqual(status, ["?? AGENTS.md"])
        exclude = (self.repo / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
        for entry in (".study/", ".claude/skills/engineering-study/", ".agents/skills/engineering-study/"):
            self.assertEqual(exclude.count(entry), 1, entry)
        self.init()
        exclude = (self.repo / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
        self.assertEqual(exclude.count(".claude/skills/engineering-study/"), 1)

    def test_s4_idempotent_force_and_kept_files(self):
        self.init()
        snap = {d: self.skill(d).read_bytes() for d in self.DIRS}
        again = self.init()
        for d in self.DIRS:
            self.assertIn("unchanged %s/SKILL.md" % d, again.stdout)
        mine = self.skill(self.DIRS[0])
        mine.write_text("my own skill\n", encoding="utf-8")
        kept = self.init()
        self.assertIn("skip      %s/SKILL.md" % self.DIRS[0], kept.stdout)
        self.assertIn("use --force", kept.stdout)
        self.assertEqual(mine.read_text(encoding="utf-8"), "my own skill\n")
        forced = self.init(self.repo, "--force")
        self.assertIn("replace   %s/SKILL.md" % self.DIRS[0], forced.stdout)
        self.assertEqual({d: self.skill(d).read_bytes() for d in self.DIRS}, snap)

    def test_s5_no_skills_and_dry_run_write_nothing(self):
        dry = self.emkit("init", self.repo, "--dry-run", check=True)
        self.assertIn("would create    %s/SKILL.md" % self.DIRS[0], dry.stdout)
        self.assertFalse((self.repo / ".claude").exists() or (self.repo / ".agents").exists())
        self.init(self.repo, "--no-skills")
        self.assertIn("skip      agent skills (--no-skills)", self.emkit("init", self.repo, "--no-skills").stdout)
        self.assertFalse((self.repo / ".claude").exists() or (self.repo / ".agents").exists())

    def test_s6_existing_skills_directory_is_extended_not_replaced(self):
        other = self.repo / ".claude" / "skills" / "other" / "SKILL.md"
        other.parent.mkdir(parents=True)
        other.write_text("keep me\n", encoding="utf-8")
        self.init()
        self.assertEqual(other.read_text(encoding="utf-8"), "keep me\n")
        self.assertTrue(self.skill(self.DIRS[0]).is_file())

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_s7_symlinked_parent_leaving_the_repo_is_refused(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.repo / ".claude").symlink_to(outside, target_is_directory=True)
        proc = self.init()
        self.assertIn("skip      .claude/skills/engineering-study/SKILL.md (a parent directory is a symlink", proc.stdout)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue(self.skill(self.DIRS[1]).is_file())

    def test_s8_doctor_reports_the_skill(self):
        self.init()
        ok = self.emkit("doctor", self.repo)
        self.assertIn("agent skill engineering-study is current", ok.stdout)
        self.skill(self.DIRS[1]).write_text("changed\n", encoding="utf-8")
        self.skill(self.DIRS[0]).unlink()
        res = self.emkit("doctor", self.repo)
        self.assertEqual(res.returncode, 0, res.stdout)  # warnings only
        self.assertIn("agent skill differs", res.stdout)
        self.assertIn("agent skill not installed", res.stdout)


class TestDoctor(Base):
    def test_16_doctor_healthy_and_failure_modes(self):
        self.init()
        ok = self.emkit("doctor", self.repo)
        self.assertEqual(ok.returncode, 0, ok.stdout)
        self.assertIn("installation usable", ok.stdout)
        self.assertIn("kit version %s" % __version__, ok.stdout)

        # deleted template
        tpl = self.repo / ".study" / "templates" / "finding.md"
        saved = tpl.read_bytes()
        tpl.unlink()
        res = self.emkit("doctor", self.repo)
        self.assertEqual(res.returncode, 1)
        self.assertIn("managed file missing: .study/templates/finding.md", res.stdout)
        tpl.write_bytes(saved)

        # corrupted template (not even frontmatter)
        tpl.write_text("garbage", encoding="utf-8")
        res = self.emkit("doctor", self.repo)
        self.assertEqual(res.returncode, 1)
        self.assertIn("template unreadable or has no frontmatter", res.stdout)
        tpl.write_bytes(saved)

        # corrupted kernel (same recorded version)
        kernel = self.repo / ".study" / "kernel.py"
        good = kernel.read_bytes()
        kernel.write_text("raise SystemExit(7)\n", encoding="utf-8")
        res = self.emkit("doctor", self.repo)
        self.assertEqual(res.returncode, 1)
        self.assertIn("does not match the packaged copy", res.stdout)
        self.assertIn("kernel.py --help failed", res.stdout)
        kernel.unlink()
        res = self.emkit("doctor", self.repo)
        self.assertEqual(res.returncode, 1)
        self.assertIn(".study/kernel.py is missing", res.stdout)
        kernel.write_bytes(good)
        self.assertEqual(self.emkit("doctor", self.repo).returncode, 0)

        # missing protocol / schema
        for rel in ("PROTOCOL.md", "schema.json"):
            p = self.repo / ".study" / rel
            data = p.read_bytes()
            p.unlink()
            self.assertEqual(self.emkit("doctor", self.repo).returncode, 1, rel)
            p.write_bytes(data)

        # an older or different recorded version downgrades differences to warnings
        (self.repo / ".study" / "VERSION").write_text("0.0.1\n", encoding="utf-8")
        (self.repo / ".study" / "PROTOCOL.md").write_text("older protocol\n", encoding="utf-8")
        res = self.emkit("doctor", self.repo)
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertIn("[warn] kit version 0.0.1 differs", res.stdout)

    def test_17_doctor_without_installation_or_directory(self):
        res = self.emkit("doctor", self.repo)
        self.assertEqual(res.returncode, 1)
        self.assertIn(".study/ not found", res.stdout)
        res = self.emkit("doctor", self.tmp / "nope")
        self.assertEqual(res.returncode, 1)
        self.assertIn("target directory does not exist", res.stdout)

    def test_18_doctor_ignores_preexisting_changes_and_never_modifies(self):
        self.make_git_repo()
        self.init()
        (self.repo / "src" / "app.py").write_text("changed\n", encoding="utf-8")
        (self.repo / "untracked.txt").write_text("u\n", encoding="utf-8")
        before = snapshot(self.tmp)
        res = self.emkit("doctor", self.repo)
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertNotIn("working-tree", res.stdout)
        self.assertIn("git repository, HEAD", res.stdout)
        self.assertEqual(snapshot(self.tmp), before)

    def test_19_kernel_check_problems_are_warnings_not_install_failures(self):
        self.init()
        (self.repo / ".study" / "systems" / "broken.md").write_text("not frontmatter\n", encoding="utf-8")
        res = self.emkit("doctor", self.repo)
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertIn("kernel.py check reports problems", res.stdout)


class TestCliSurface(unittest.TestCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = cli.main(argv)
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 1
        return rc, out.getvalue(), err.getvalue()

    def test_version_help_and_usage_errors(self):
        rc, out, _ = self.run_main(["--version"])
        self.assertEqual((rc, out.strip()), (0, "emkit " + __version__))
        rc, out, _ = self.run_main(["init", "--help"])
        self.assertEqual(rc, 0)
        self.assertIn("--force", out)
        self.assertIn("--dry-run", out)
        rc, _, err = self.run_main([])
        self.assertNotEqual(rc, 0)
        rc, _, err = self.run_main(["bogus"])
        self.assertNotEqual(rc, 0)

    def test_module_entry_point_matches_spec(self):
        text = (PKG / "__main__.py").read_text(encoding="utf-8")
        self.assertIn("from .cli import main", text)
        self.assertIn("raise SystemExit(main())", text)

    def test_cli_does_not_expose_study_operations(self):
        parser = cli.build_parser()
        choices = next(a for a in parser._actions if getattr(a, "choices", None)).choices
        self.assertEqual(set(choices), {"init", "doctor"})


@unittest.skipUnless(HAVE_UVX and HAVE_GIT, "uvx and git are required")
class TestUvx(unittest.TestCase):
    """The real target command, against a fresh clone and a git+file URL."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="emkit-uvx-"))
        origin = cls.tmp / "origin repo"
        shutil.copytree(str(ROOT), str(origin), ignore=shutil.ignore_patterns(
            ".git", "__pycache__", "build", "dist", "*.egg-info", ".venv", "venv", "*.zip"))
        git(origin, "init", "-q")
        git(origin, "add", "-A")
        git(origin, "commit", "-qm", "release")
        git(origin, "tag", "v%s" % __version__)
        cls.origin = origin
        cls.clone = cls.tmp / "fresh clone"
        subprocess.run(["git", "clone", "-q", str(origin), str(cls.clone)], check=True)
        cls.cache = cls.tmp / "uv-cache"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(str(cls.tmp), True)

    def uvx(self, source, target):
        env = child_env(UV_CACHE_DIR=str(self.cache))
        return subprocess.run(["uvx", "--from", source, "emkit", "init", str(target)], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                              timeout=600)

    def assert_installed(self, target):
        for m in cli.MANAGED:
            self.assertTrue((target / Path(*m.dest.split("/"))).is_file(), m.dest)
        self.assertEqual(sha(target / ".study" / "kernel.py"), sha(PKG / "kernel.py"))
        self.assertEqual(sha(target / ".study" / "templates" / "run.md"), sha(PKG / "resources" / "templates" / "run.md"))

    def test_20_uvx_from_fresh_clone_path(self):
        target = self.tmp / "test project"
        proc = self.uvx(str(self.clone), target)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assert_installed(target)
        # The uvx environment is disposable: wipe uv's cache and keep using the installed workspace.
        shutil.rmtree(str(self.cache), True)
        res = subprocess.run([sys.executable, str(target / ".study" / "kernel.py"), "status"], cwd=str(target),
                             env=child_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_21_uvx_from_git_file_url_and_tag(self):
        url = "git+" + self.origin.as_uri()
        for suffix in ("", "@v%s" % __version__):
            target = self.tmp / ("via-git" + suffix.replace("@", "-"))
            proc = self.uvx(url + suffix, target)
            self.assertEqual(proc.returncode, 0, (suffix, proc.stdout + proc.stderr))
            self.assert_installed(target)
            self.assertEqual((target / ".study" / "VERSION").read_text(encoding="utf-8"), __version__ + "\n")


if __name__ == "__main__":
    unittest.main()
