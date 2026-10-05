"""Acceptance tests for the Engineering Study Kit V0 kernel (unittest only).

Run from the kit root:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "src" / "emkit"
RES = PKG / "resources"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "auth.py"
sys.path.insert(0, str(ROOT / "src"))

from emkit import kernel  # noqa: E402

HAVE_GIT = shutil.which("git") is not None
needs_git = unittest.skipUnless(HAVE_GIT, "git executable not available")


def git(root, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(root)] + list(args),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)


class Result:
    def __init__(self, rc, out, err):
        self.rc, self.out, self.err = rc, out, err

    def json(self):
        return json.loads(self.out)


class KitCase(unittest.TestCase):
    """Base: a temporary Git repository (when git exists) with src/auth.py committed."""

    use_git = True
    env = None

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="studykit-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.root = self.tmp / "repo"
        (self.root / "src").mkdir(parents=True)
        shutil.copy(str(FIXTURE), str(self.root / "src" / "auth.py"))
        if HAVE_GIT and self.use_git:
            git(self.root, "init", "-q")
            git(self.root, "add", "-A")
            git(self.root, "commit", "-qm", "fixture")
        self.study = self.root / ".study"

    # -- helpers --
    def k(self, *args, ok=None):
        out, err = io.StringIO(), io.StringIO()
        argv = [str(a) for a in args] + ["--root", str(self.root), "--agent", "tester"]
        rc = kernel.main(argv, stdout=out, stderr=err)
        res = Result(rc, out.getvalue(), err.getvalue())
        if ok is True:
            self.assertEqual(rc, 0, "unexpected failure: %s %s" % (args, res.err))
        if ok is False:
            self.assertNotEqual(rc, 0, "unexpected success: %s" % (args,))
        return res

    def init(self):
        return self.k("init", ok=True)

    def drop_git(self):
        shutil.rmtree(str(self.root / ".git"))

    def start_run(self, goal="test run"):
        res = self.k("run", "start", "--goal", goal, ok=True)
        return res.out.strip().splitlines()[-1]

    def ready(self):
        """init + one open run + SYS-auth with one anchor."""
        self.init()
        run = self.start_run()
        self.k("new", "system", "auth", "--title", "Authentication", "--run", run, ok=True)
        self.k("anchor", "add", "SYS-auth", "src/auth.py", "--symbol", "verify_token",
               "--start-line", "4", "--end-line", "7", "--run", run, ok=True)
        return run

    def db_rows(self, table):
        conn = sqlite3.connect(str(self.study / "study.db"))
        try:
            return sorted(conn.execute("SELECT * FROM %s" % table).fetchall())
        finally:
            conn.close()

    def read(self, rel):
        return (self.study / rel).read_text(encoding="utf-8")

    def write(self, rel, text):
        (self.study / rel).write_text(text, encoding="utf-8")

    def problem_codes(self, res):
        return {p["code"] for p in res.json()["problems"]}

    def end_run(self, run, *extra):
        return self.k("run", "end", "--id", run, "--summary", "done", *extra)


class TestInstall(KitCase):
    def test_01_init_creates_exact_layout(self):
        self.init()
        found = set()
        for p in self.study.rglob("*"):
            rel = p.relative_to(self.study).as_posix()
            if rel.startswith("study.db-"):
                continue
            found.add(rel)
        expected = {
            "kernel.py", "PROTOCOL.md", "schema.json", "study.db", "templates",
            "templates/system.md", "templates/flow.md", "templates/finding.md", "templates/run.md",
            "systems", "flows", "findings", "runs", "scratch",
        }
        self.assertEqual(found, expected)

    def test_02_init_refuses_overwrite_without_force(self):
        self.init()
        res = self.k("init")
        self.assertEqual(res.rc, 1)
        self.assertIn("refusing to overwrite", res.err)
        self.write("PROTOCOL.md", "local edit\n")
        self.assertEqual(self.k("init").rc, 1)
        self.assertEqual(self.read("PROTOCOL.md"), "local edit\n")
        self.k("init", "--force", ok=True)
        self.assertNotEqual(self.read("PROTOCOL.md"), "local edit\n")

    @needs_git
    def test_03_init_preserves_git_exclude(self):
        exclude = self.root / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("# mine\n*.secret", encoding="utf-8")  # no trailing newline
        self.init()
        text = exclude.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# mine\n*.secret\n"))
        for pattern in kernel.GIT_EXCLUDE_ENTRIES:
            self.assertEqual(text.splitlines().count(pattern), 1, pattern)
        self.k("init", "--force", ok=True)
        again = exclude.read_text(encoding="utf-8")
        self.assertEqual(again, text)

    def test_04_database_deletion_and_rebuild_equivalent(self):
        run = self.ready()
        self.k("finding", "Prefix only", "--severity", "low", "--anchor", "ANC-0001", "--run", run, ok=True)
        self.k("evidence", "add", "ANC-0001", "--type", "source-inspection", "--result", "ok",
               "--run", run, ok=True)
        before = {t: self.db_rows(t) for t in ("docs", "links", "anchors", "evidence")}
        (self.study / "study.db").unlink()
        for suffix in ("-wal", "-shm"):
            p = self.study / ("study.db" + suffix)
            if p.exists():
                p.unlink()
        self.k("rebuild", ok=True)
        after = {t: self.db_rows(t) for t in before}
        self.assertEqual([r[0] for r in before["docs"]], [r[0] for r in after["docs"]])
        self.assertEqual(before["links"], after["links"])
        self.assertEqual(before["anchors"], after["anchors"])
        self.assertEqual(before["evidence"], after["evidence"])


class TestSafety(KitCase):
    def test_05_slugs_reject_bad_input(self):
        self.init()
        run = self.start_run()
        for bad in ("Auth", "a b", "../x", "", "a--b", "-a", "a/b", "a_b", "x" * 65):
            for kind in ("system", "flow"):
                res = self.k("new", kind, bad, "--title", "T", "--run", run)
                self.assertEqual(res.rc, 1, (kind, bad))
        self.assertEqual(list((self.study / "systems").iterdir()), [])
        self.assertEqual(list((self.study / "flows").iterdir()), [])
        self.k("new", "system", "ok-slug-1", "--title", "T", "--run", run, ok=True)

    def test_06_numbered_ids_unique_in_rapid_sequence(self):
        run = self.ready()
        ids = []
        for i in range(12):
            res = self.k("finding", "finding %d" % i, "--severity", "low", "--run", run, ok=True)
            ids.append(res.out.split()[1])
        self.assertEqual(ids, ["F-%04d" % n for n in range(1, 13)])
        for i in range(6):
            self.k("anchor", "add", "SYS-auth", "src/auth.py", "--start-line", i + 1, "--end-line", i + 2,
                   "--run", run, ok=True)
        ev = []
        for i in range(6):
            ev.append(self.k("evidence", "add", "SYS-auth", "--type", "source-inspection",
                             "--result", "r%d" % i, "--run", run, ok=True).out.strip())
        self.assertEqual(ev, ["EV-%04d" % n for n in range(1, 7)])
        self.assertEqual(self.start_run(), "RUN-0002")
        rec = kernel.load_records(self._ctx(), evaluate=False)
        anchor_ids = [a["id"] for a in rec.anchors]
        self.assertEqual(len(anchor_ids), len(set(anchor_ids)))
        self.assertEqual(len(anchor_ids), 7)

    def _ctx(self):
        args = kernel.build_parser().parse_args(["status", "--root", str(self.root)])
        return kernel.build_context(args, io.StringIO(), io.StringIO(), True)

    def test_07_writes_outside_study_are_rejected(self):
        self.init()
        fs = kernel.StudyFS(self.root, self.study)
        victim = self.root / "src" / "auth.py"
        before = victim.read_bytes()
        for op in (lambda: fs.atomic_write(victim, b"x"), lambda: fs.create_exclusive(self.root / "new.txt", b"x"),
                   lambda: fs.append_bytes(victim, b"x"), lambda: fs.mkdirs(self.root / "elsewhere"),
                   lambda: fs.check_write(self.tmp / "outside.txt")):
            with self.assertRaises(kernel.KitError):
                op()
        self.assertEqual(victim.read_bytes(), before)
        self.assertFalse((self.root / "new.txt").exists())
        res = self.k("status", "--study-dir", self.tmp / "elsewhere")
        self.assertEqual(res.rc, 1)
        res = self.k("init", "--study-dir", self.root)
        self.assertEqual(res.rc, 1)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_08_symlink_inside_study_cannot_escape(self):
        self.init()
        run = self.start_run()
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.study / "systems").rmdir()
        try:
            os.symlink(str(outside), str(self.study / "systems"))
        except OSError:
            self.skipTest("cannot create symlink")
        res = self.k("new", "system", "evil", "--title", "T", "--run", run)
        self.assertEqual(res.rc, 1)
        self.assertEqual(list(outside.iterdir()), [])
        fs = kernel.StudyFS(self.root, self.study)
        with self.assertRaises(kernel.KitError):
            fs.atomic_write(self.study / "systems" / "x.md", b"x")

    def test_09_anchor_rejects_absolute_and_traversal_paths(self):
        run = self.ready()
        secret = self.tmp / "secret.txt"
        secret.write_text("secret\n", encoding="utf-8")
        for bad in (str(secret), "/etc/passwd", "../secret.txt", "src/../../secret.txt", "src\\auth.py",
                    "C:/x.txt", "", ".git/config", ".study/PROTOCOL.md"):
            res = self.k("anchor", "add", "SYS-auth", bad, "--run", run)
            self.assertEqual(res.rc, 1, bad)
        self.assertEqual(self.read("systems/auth.md").count('"id":"ANC-') + self.read("systems/auth.md").count('"id": "ANC-'), 1)

    def test_29_no_command_writes_source_or_runs_shell(self):
        # Static: the only subprocess use is GitInspector, limited to read-only subcommands.
        src = (PKG / "kernel.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        self.assertEqual(kernel.ALLOWED_GIT_SUBCOMMANDS, ("rev-parse", "status"))
        self.assertNotIn("shell=True", src)
        self.assertNotIn("os.system", src)
        self.assertNotIn("os.popen", src)
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and isinstance(n.func.value, ast.Name) and n.func.value.id == "subprocess"]
        self.assertEqual(len(calls), 1)
        # Behavioral: run every command, then confirm source and Git state are unchanged.
        marker = self.tmp / "executed"
        digests = lambda: {p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in self.root.rglob("*") if p.is_file() and ".git" not in p.parts
                           and ".study" not in p.parts}
        before = digests()
        run = self.ready()
        self.k("new", "flow", "login", "--title", "Login", "--run", run, ok=True)
        self.k("finding", "x", "--severity", "high", "--anchor", "ANC-0001", "--run", run, ok=True)
        self.k("evidence", "add", "ANC-0001", "--type", "test-run", "--result", "ok", "--command",
               "touch %s" % marker, "--exit-code", "0", "--run", run, ok=True)
        self.k("claim", "add", "SYS-auth", "verify_token guards empty input", "--anchor", "ANC-0001",
               "--run", run, ok=True)
        self.k("set", "F-0001", "--status", "triaged", "--run", run, ok=True)
        for cmd in (["check"], ["status"], ["coverage"], ["list"], ["search", "authentication"],
                    ["show", "SYS-auth"], ["graph", "SYS-auth"], ["rebuild"], ["orient"]):
            self.k(*cmd)
        self.end_run(run, "--covered", "SYS-auth")
        self.assertFalse(marker.exists(), "--command text must never be executed")
        self.assertEqual(digests(), before)
        if HAVE_GIT:
            status = git(self.root, "status", "--porcelain", "--", ".", ":(exclude).study").stdout
            self.assertEqual(status, b"")


class TestAnchors(KitCase):
    def anchor_states(self):
        return self.k("check", "--json").json()["anchors"]

    @needs_git
    def test_10_missing_file_reported(self):
        self.ready()
        self.assertEqual(self.anchor_states()["ANC-0001"], "ok")
        (self.root / "src" / "auth.py").unlink()
        self.assertEqual(self.anchor_states()["ANC-0001"], "missing_file")
        res = self.k("check", "--json")
        self.assertIn("anchor-missing-file", self.problem_codes(res))
        self.assertEqual(res.rc, 0)  # freshness problems are warnings

    @needs_git
    def test_11_edit_makes_anchor_stale(self):
        self.ready()
        path = self.root / "src" / "auth.py"
        path.write_text(path.read_text().replace('"ok:"', '"OK:"'), encoding="utf-8")
        self.assertEqual(self.anchor_states()["ANC-0001"], "stale")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "edit")  # committed edit: still stale by fingerprint
        self.assertEqual(self.anchor_states()["ANC-0001"], "stale")

    @needs_git
    def test_12_moved_text_is_not_ok(self):
        self.ready()
        path = self.root / "src" / "auth.py"
        path.write_text("# new header\n# another\n" + path.read_text(), encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "shift")
        state = self.anchor_states()["ANC-0001"]
        self.assertIn(state, ("stale", "missing_symbol"))
        self.assertNotEqual(state, "ok")

    def test_12b_missing_symbol_and_no_git_fallback(self):
        if HAVE_GIT:
            self.drop_git()
        self.ready()
        self.assertEqual(self.anchor_states()["ANC-0001"], "ok")
        res = self.k("check", "--json")
        self.assertIn("freshness-limited", self.problem_codes(res))
        path = self.root / "src" / "auth.py"
        path.write_text(path.read_text().replace("verify_token", "check_token"), encoding="utf-8")
        self.assertEqual(self.anchor_states()["ANC-0001"], "missing_symbol")

    def test_12c_duplicate_anchor_skipped_and_ambiguity_warned(self):
        run = self.ready()
        res = self.k("anchor", "add", "SYS-auth", "src/auth.py", "--symbol", "verify_token",
                     "--start-line", "4", "--end-line", "7", "--run", run, ok=True)
        self.assertIn("duplicate skipped", res.out)
        self.assertEqual(len(self.db_rows("anchors")), 1)
        res = self.k("anchor", "add", "SYS-auth", "src/auth.py", "--symbol", "token", "--run", run, ok=True)
        self.assertIn("warning", res.err)


class TestRecords(KitCase):
    def test_13_malformed_frontmatter_fails_check_and_keeps_db(self):
        run = self.ready()
        before = self.db_rows("docs")
        self.write("systems/broken.md", "---\nid: SYS-broken\nkind: system\n  bad indent\n---\n")
        res = self.k("check", "--json")
        self.assertEqual(res.rc, 1)
        self.assertTrue(res.json()["errors"] >= 1)
        rb = self.k("rebuild")
        self.assertEqual(rb.rc, 1)
        self.assertEqual(self.db_rows("docs"), before)
        self.assertEqual(self.k("search", "authentication", ok=True).out.count("SYS-auth"), 1)
        self.assertTrue(run)

    def test_14_duplicate_id_reported_and_no_partial_rebuild(self):
        self.ready()
        run2 = self.start_run("second")
        self.k("run", "end", "--id", run2, "--summary", "x", ok=True)
        self.k("evidence", "add", "SYS-auth", "--type", "source-inspection", "--result", "a",
               "--run", self.start_run("third"), ok=True)
        before = self.db_rows("evidence")
        line = self.read("runs/RUN-0003/evidence.jsonl")
        self.assertIn("EV-0001", line)
        with open(str(self.study / "runs" / "RUN-0002" / "evidence.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(line)
        res = self.k("check", "--json")
        self.assertEqual(res.rc, 1)
        self.assertIn("duplicate-evidence-id", self.problem_codes(res))
        self.assertEqual(self.k("rebuild").rc, 1)
        self.assertEqual(self.db_rows("evidence"), before)
        # duplicate anchor ID across documents
        self.k("new", "flow", "login", "--title", "Login", "--run", "RUN-0003", ok=True)
        auth = self.read("systems/auth.md")
        flow = self.read("flows/login.md")
        block = auth[auth.index("<!-- study:anchors:begin"):auth.index("study:anchors:end -->") + len("study:anchors:end -->")]
        self.write("flows/login.md", flow.replace("anchors: []", "anchors: [ANC-0001]") + "\n" + block + "\n")
        res = self.k("check", "--json")
        self.assertIn("duplicate-id", self.problem_codes(res))

    def test_15_link_to_unknown_id_reported(self):
        self.ready()
        text = self.read("systems/auth.md").replace("links: []", "links: [SYS-nope, F-0099]")
        self.write("systems/auth.md", text)
        res = self.k("check", "--json")
        self.assertEqual(res.rc, 1)
        messages = " ".join(p["message"] for p in res.json()["problems"] if p["code"] == "link-unknown")
        self.assertIn("SYS-nope", messages)
        self.assertIn("F-0099", messages)

    def test_16_malformed_evidence_jsonl_reported_prior_lines_readable(self):
        run = self.ready()
        self.k("evidence", "add", "SYS-auth", "--type", "source-inspection", "--result", "first", "--run", run, ok=True)
        with open(str(self.study / "runs" / run / "evidence.jsonl"), "a", encoding="utf-8") as fh:
            fh.write("{not json\n")
            fh.write(json.dumps({"schema": "engineering-study-evidence/v1", "id": "EV-0002"}) + "\n")
        res = self.k("check", "--json")
        codes = self.problem_codes(res)
        self.assertIn("jsonl-malformed", codes)
        self.assertIn("evidence-invalid", codes)
        self.assertEqual(res.rc, 1)
        self.assertGreaterEqual(res.json()["errors"], 2)
        self.k("rebuild", ok=True)
        shown = self.k("show", "EV-0001", "--json", ok=True).json()
        self.assertEqual(shown["record"]["result"], "first")

    def test_17_evidence_append_does_not_rewrite_prior_bytes(self):
        run = self.ready()
        path = self.study / "runs" / run / "evidence.jsonl"
        self.k("evidence", "add", "SYS-auth", "--type", "source-inspection", "--result", "one", "--run", run, ok=True)
        first = path.read_bytes()
        self.k("evidence", "add", "SYS-auth", "--type", "test-run", "--result", "two", "--exit-code", "0",
               "--command", "pytest -q", "--limitation", "unit tests only", "--run", run, ok=True)
        second = path.read_bytes()
        self.assertTrue(second.startswith(first))
        self.assertGreater(len(second), len(first))
        self.assertEqual(len(second.strip().splitlines()), 2)

    def test_18_search_finds_title_and_body_but_not_events(self):
        run = self.ready()
        self.k("evidence", "add", "SYS-auth", "--type", "test-run", "--result", "ok",
               "--command", "zzeventonlyword --flag", "--run", run, ok=True)
        text = self.read("systems/auth.md") + "\nThe quokkaword subsystem rejects empty input.\n"
        self.write("systems/auth.md", text)
        self.k("rebuild", ok=True)
        self.assertIn("SYS-auth", self.k("search", "quokkaword", ok=True).out)
        self.assertIn("SYS-auth", self.k("search", "authentication", ok=True).out)
        self.assertIn("SYS-auth", self.k("search", "quokkaword", "rejects", ok=True).out)  # AND semantics
        self.assertNotIn("SYS-auth", self.k("search", "quokkaword", "nonexistentterm", ok=True).out)
        self.assertNotIn("SYS-auth", self.k("search", "zzeventonlyword", ok=True).out)
        events = (self.study / "runs" / run / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("zzeventonlyword", events)
        # template instructions are comments and must not be indexed
        self.assertNotIn("SYS-auth", self.k("search", "AGENT", "INSTRUCTIONS", ok=True).out)

    def test_19_show_events_limit(self):
        run = self.ready()
        for i in range(4):
            self.k("evidence", "add", "SYS-auth", "--type", "source-inspection", "--result", "r%d" % i,
                   "--run", run, ok=True)
        one = self.k("show", run, "--events", "--limit", "1", "--json", ok=True).json()
        self.assertEqual(len(one["events"]), 1)
        three = self.k("show", run, "--events", "--limit", "3", "--json", ok=True).json()
        self.assertEqual(len(three["events"]), 3)
        plain = self.k("show", run, "--json", ok=True).json()
        self.assertNotIn("events", plain)
        self.assertEqual(self.k("show", run, "--events", "--limit", "0").rc, 1)
        self.assertEqual(self.k("show", "SYS-auth", "--events").rc, 1)

    def test_20_graph_terminates_on_cycles(self):
        run = self.ready()
        self.k("new", "flow", "login", "--title", "Login", "--run", run, ok=True)
        self.write("systems/auth.md", self.read("systems/auth.md").replace("links: []", "links: [FLOW-login]"))
        self.write("flows/login.md", self.read("flows/login.md").replace("links: []", "links: [SYS-auth]"))
        self.k("rebuild", ok=True)
        res = self.k("graph", "SYS-auth", "--depth", "10", "--json", ok=True)
        data = res.json()
        self.assertTrue(json.dumps(data))
        self.assertIn("FLOW-login", res.out)
        self.assertEqual(self.k("graph", "SYS-auth", "--depth", "11").rc, 1)

    def test_20b_claims_are_indexed_and_unique(self):
        run = self.ready()
        text = self.read("systems/auth.md").replace(
            "## Claims\n\n_(none recorded)_",
            "## Claims\n\n- CLM-0001: verify_token rejects empty tokens. (ANC-0001)")
        self.write("systems/auth.md", text)
        self.k("rebuild", ok=True)
        self.k("evidence", "add", "CLM-0001", "--type", "source-inspection", "--result", "ok", "--run", run, ok=True)
        self.assertEqual(self.k("show", "CLM-0001", "--json", ok=True).json()["owner"], "SYS-auth")
        self.k("new", "flow", "login", "--title", "Login", "--run", run, ok=True)
        flow = self.read("flows/login.md").replace(
            "## Claims\n\n_(none recorded)_", "## Claims\n\n- CLM-0001: duplicate.")
        self.write("flows/login.md", flow)
        res = self.k("check", "--json")
        self.assertIn("claim-duplicate", self.problem_codes(res))
        self.assertEqual(res.rc, 1)

    def test_20c_verified_confidence_is_rejected(self):
        self.ready()
        self.write("systems/auth.md", self.read("systems/auth.md").replace("confidence: hypothesis",
                                                                        "confidence: verified"))
        res = self.k("check", "--json")
        self.assertEqual(res.rc, 1)
        self.assertIn("frontmatter-field", self.problem_codes(res))

    def test_20d_finding_confidence_and_status_rules(self):
        run = self.ready()
        self.k("finding", "no anchor", "--severity", "low", "--run", run, ok=True)
        self.k("finding", "anchored", "--severity", "high", "--anchor", "ANC-0001", "--run", run, ok=True)
        self.assertIn("confidence: hypothesis", self.read("findings/F-0001.md"))
        self.assertIn("confidence: observed", self.read("findings/F-0002.md"))
        self.assertIn("status: open", self.read("findings/F-0001.md"))
        self.assertEqual(self.k("finding", "bad", "--severity", "urgent", "--run", run).rc, 1)
        self.assertEqual(self.k("finding", "bad", "--severity", "low", "--anchor", "ANC-0099", "--run", run).rc, 1)
        self.write("findings/F-0001.md", self.read("findings/F-0001.md").replace("status: open", "status: fixed"))
        self.assertEqual(self.k("check").rc, 1)


class TestCommands(KitCase):
    def test_21_coverage_excludes_directories(self):
        self.ready()
        for d in ("node_modules", "__pycache__", "venv", ".venv", "dist", "build", "target"):
            (self.root / d).mkdir()
            (self.root / d / "x.js").write_text("x\n", encoding="utf-8")
        (self.root / "src" / "other.py").write_text("x = 1\n", encoding="utf-8")
        data = self.k("coverage", "--json", ok=True).json()
        listed = [f for g in data["groups"] for f in g["unanchored_files"]]
        self.assertEqual(listed, ["src/other.py"])
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["anchored"], 1)
        self.assertIn("descriptive", data["note"])

    @needs_git
    def test_22_run_end_succeeds_when_source_unchanged(self):
        run = self.ready()
        res = self.end_run(run, "--covered", "SYS-auth")
        self.assertEqual(res.rc, 0, res.err)
        self.assertIn("status: ended", self.read("runs/%s/summary.md" % run))
        self.assertEqual(self.k("run", "end", "--id", run, "--summary", "again").rc, 1)  # already ended

    @needs_git
    def test_23_run_end_fails_and_marks_source_changed(self):
        run = self.ready()
        (self.root / "src" / "auth.py").write_text("# tampered\n", encoding="utf-8")
        res = self.end_run(run)
        self.assertEqual(res.rc, 1)
        self.assertIn("source state changed", res.err)
        summary = self.read("runs/%s/summary.md" % run)
        self.assertIn("status: source_changed", summary)
        self.assertIn("SOURCE STATE CHANGED", summary)
        self.assertEqual(self.k("new", "system", "x", "--title", "T", "--run", run).rc, 1)  # run no longer open
        (self.root / "extra.txt").write_text("new untracked file\n", encoding="utf-8")
        run2 = self.start_run("second")
        (self.root / "extra.txt").unlink()
        self.assertEqual(self.end_run(run2).rc, 1)

    @needs_git
    def test_24_preexisting_dirty_state_is_tolerated(self):
        self.init()
        (self.root / "src" / "auth.py").write_text("# dirty before run\n", encoding="utf-8")
        (self.root / "scratch.txt").write_text("untracked\n", encoding="utf-8")
        run = self.start_run()
        res = self.end_run(run)
        self.assertEqual(res.rc, 0, res.err)
        self.assertIn("status: ended", self.read("runs/%s/summary.md" % run))

    def test_24b_no_git_run_guard_is_unavailable(self):
        if HAVE_GIT:
            self.drop_git()
        self.init()
        res = self.k("run", "start", "--goal", "g", ok=True)
        self.assertIn("not a Git repository", res.err)
        end = self.end_run("RUN-0001")
        self.assertEqual(end.rc, 0, end.err)
        self.assertIn("unavailable", end.out)

    def test_25_cli_errors_use_stderr_and_nonzero_status(self):
        self.init()
        cases = (["show", "F-9999"], ["show", "bogus"], ["run", "end", "--id", "RUN-0009", "--summary", "x"],
                 ["new", "system", "x", "--title", "T", "--run", "RUN-0001"], ["search"],
                 ["finding", "t", "--severity", "low"], ["nonexistent"], ["list", "--kind", "bogus"])
        for argv in cases:
            res = self.k(*argv)
            self.assertNotEqual(res.rc, 0, argv)
            self.assertEqual(res.out, "", argv)
            self.assertIn("error:", res.err, argv)
        out, err = io.StringIO(), io.StringIO()
        rc = kernel.main(["status", "--root", str(self.tmp / "norepo")], stdout=out, stderr=err)
        self.assertEqual(rc, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("error:", err.getvalue())

    def test_26_json_output_parses_and_diagnostics_stay_on_stderr(self):
        run = self.ready()
        self.write("systems/broken.md", "not frontmatter\n")
        for cmd in (["status"], ["check"], ["coverage"], ["list"], ["search", "authentication"],
                    ["show", "SYS-auth"], ["graph", "SYS-auth"], ["rebuild"]):
            res = self.k(*cmd, "--json")
            if cmd == ["rebuild"]:  # the broken document makes rebuild fail; nothing may reach stdout
                self.assertEqual((res.rc, res.out), (1, ""))
                continue
            try:
                json.loads(res.out)
            except ValueError:
                self.fail("stdout is not JSON for %s: %r" % (cmd, res.out[:200]))
        res = self.k("coverage", "--json", ok=True)
        self.assertIn("warning:", res.err)
        self.assertNotIn("warning:", res.out)
        self.assertTrue(run)

    def test_27_fts_absence_uses_fallback(self):
        with mock.patch.dict(os.environ, {"STUDY_DISABLE_FTS": "1"}):
            run = self.ready()
            self.assertEqual(self.k("rebuild", "--json", ok=True).json()["backend"], "like")
            self.assertIn("SYS-auth", self.k("search", "authentication", ok=True).out)
            self.assertNotIn("SYS-auth", self.k("search", "authentication", "nonexistentterm", ok=True).out)
            self.assertIn("like", self.k("status", ok=True).out)
            self.assertTrue(run)
        conn = sqlite3.connect(str(self.study / "study.db"))
        try:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        finally:
            conn.close()
        self.assertNotIn("docs_fts", names)

    def test_28_rebuild_rollback_keeps_previous_database(self):
        self.ready()
        before = {t: self.db_rows(t) for t in ("docs", "anchors", "links")}

        def boom(conn, rec, use_fts):
            conn.execute("DELETE FROM docs")  # partial work that must be rolled back
            raise sqlite3.OperationalError("injected failure")

        with mock.patch.object(kernel.Index, "_insert", staticmethod(boom)):
            res = self.k("rebuild")
        self.assertEqual(res.rc, 1)
        self.assertIn("previous index kept", res.err)
        self.assertEqual({t: self.db_rows(t) for t in before}, before)
        self.assertIn("SYS-auth", self.k("search", "authentication", ok=True).out)

    def test_28b_status_list_show_basics(self):
        run = self.ready()
        self.k("finding", "Prefix only", "--severity", "medium", "--anchor", "ANC-0001", "--run", run, ok=True)
        status = self.k("status", "--json", ok=True).json()
        self.assertTrue(json.dumps(status))
        listed = self.k("list", "--kind", "finding", "--status", "open", "--json", ok=True).json()
        self.assertTrue(json.dumps(listed))
        self.assertIn("F-0001", self.k("list", "--kind", "finding", ok=True).out)
        self.assertNotIn("SYS-auth", self.k("list", "--kind", "finding", ok=True).out)
        shown = self.k("show", "SYS-auth", "--json", ok=True).json()
        self.assertEqual(shown["id"], "SYS-auth")
        self.assertEqual(len(shown["anchors"]), 1)
        anc = self.k("show", "ANC-0001", "--json", ok=True).json()
        self.assertEqual(anc["owner"], "SYS-auth")


class TestClaims(KitCase):
    def claim(self, *args, run, doc="SYS-auth"):
        return self.k("claim", "add", doc, *args, "--run", run)

    def test_31_claim_add_writes_numbered_indexed_line(self):
        run = self.ready()
        self.k("evidence", "add", "ANC-0001", "--type", "source-inspection", "--result", "ok", "--run", run, ok=True)
        res = self.claim("verify_token rejects empty tokens", "--anchor", "ANC-0001", "--evidence", "EV-0001",
                         run=run)
        self.assertEqual((res.rc, res.out.strip()), (0, "CLM-0001"), res.err)
        res = self.claim("refresh is single use", "--anchor", "ANC-0001", "--inference", run=run)
        self.assertEqual(res.out.strip(), "CLM-0002")
        text = self.read("systems/auth.md")
        self.assertIn("- CLM-0001: verify_token rejects empty tokens (ANC-0001, EV-0001)", text)
        self.assertIn("- CLM-0002: Inference: refresh is single use (ANC-0001)", text)
        self.assertNotIn("_(none recorded)_\n\n- CLM", text)  # placeholder replaced
        shown = self.k("show", "CLM-0002", "--json", ok=True).json()
        self.assertEqual(shown["owner"], "SYS-auth")
        self.assertIn("SYS-auth", self.k("search", "single", "use", ok=True).out)
        self.k("evidence", "add", "CLM-0001", "--type", "test-run", "--result", "ok", "--run", run, ok=True)
        self.assertEqual(self.k("check").rc, 0)

    def test_32_claim_numbers_unique_across_documents(self):
        run = self.ready()
        self.k("new", "flow", "login", "--title", "Login", "--run", run, ok=True)
        ids = []
        for doc in ("SYS-auth", "FLOW-login", "SYS-auth", "FLOW-login"):
            ids.append(self.claim("statement for %s" % doc, "--anchor", "ANC-0001", run=run, doc=doc).out.strip())
        self.assertEqual(ids, ["CLM-0001", "CLM-0002", "CLM-0003", "CLM-0004"])
        self.assertEqual(self.k("check").rc, 0)

    def test_33_claim_requires_grounding_and_known_ids(self):
        run = self.ready()
        for args in (("no refs",), ("bad anchor", "--anchor", "ANC-0099"), ("bad anchor id", "--anchor", "nope"),
                     ("bad evidence", "--evidence", "EV-0099"), ("bad evidence id", "--evidence", "x"),
                     ("two\nlines", "--anchor", "ANC-0001"), ("", "--anchor", "ANC-0001")):
            res = self.claim(*args, run=run)
            self.assertEqual(res.rc, 1, args)
            self.assertEqual(res.out, "")
        self.assertNotIn("CLM-", kernel.strip_comments(self.read("systems/auth.md")))
        self.assertEqual(self.claim("x", "--anchor", "ANC-0001", run="RUN-0042").rc, 1)  # unknown run

    def test_34_claim_only_on_system_or_flow(self):
        run = self.ready()
        self.k("finding", "f", "--severity", "low", "--run", run, ok=True)
        for doc in ("F-0001", run, "SYS-missing", "ANC-0001", "bogus"):
            self.assertEqual(self.claim("x", "--anchor", "ANC-0001", run=run, doc=doc).rc, 1, doc)

    def test_35_claim_text_cannot_inject_structure(self):
        run = self.ready()
        self.claim("# Heading <!-- hidden --> text", "--anchor", "ANC-0001", run=run)
        text = self.read("systems/auth.md")
        self.assertNotIn("<!-- hidden", text)
        headings = [l for l in text.splitlines() if l.startswith("## ")]
        self.assertEqual(len(headings), len(set(headings)))
        self.assertEqual(self.k("check").rc, 0)
        self.assertIn("CLM-0001", [c for c, _ in kernel.load_records(self._ctx(), evaluate=False).claims])

    def test_36_claim_survives_anchor_block_and_later_anchor_adds(self):
        run = self.ready()
        self.claim("first", "--anchor", "ANC-0001", run=run)
        self.k("anchor", "add", "SYS-auth", "src/auth.py", "--start-line", "10", "--end-line", "12", "--run", run, ok=True)
        self.claim("second", "--anchor", "ANC-0002", run=run)
        text = self.read("systems/auth.md")
        self.assertEqual(text.count("study:anchors:begin"), 1)
        self.assertEqual(text.count('"id": "ANC-'), 2)
        self.assertTrue(text.rstrip().endswith("study:anchors:end -->"))
        self.assertEqual(self.k("check").rc, 0)

    def _ctx(self):
        args = kernel.build_parser().parse_args(["status", "--root", str(self.root)])
        return kernel.build_context(args, io.StringIO(), io.StringIO(), True)


class TestSet(KitCase):
    def setup_finding(self):
        run = self.ready()
        self.k("finding", "Prefix only", "--severity", "medium", "--anchor", "ANC-0001", "--run", run, ok=True)
        return run

    def test_37_set_finding_status_with_log(self):
        run = self.setup_finding()
        res = self.k("set", "F-0001", "--status", "triaged", "--note", "read the code", "--run", run)
        self.assertEqual(res.rc, 0, res.err)
        text = self.read("findings/F-0001.md")
        self.assertIn("status: triaged", text)
        self.assertIn("## Status log", text)
        self.assertRegex(text, r"- \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ tester: status open -> triaged \(RUN-0001\) - read the code")
        self.assertEqual(self.k("list", "--kind", "finding", "--status", "triaged", ok=True).out.count("F-0001"), 1)
        self.k("set", "F-0001", "--status", "dismissed", "--note", "intended", "--run", run, ok=True)
        text = self.read("findings/F-0001.md")
        self.assertEqual(text.count("tester: status"), 2)
        self.assertEqual(text.count("## Status log"), 1)
        self.assertEqual(self.k("check").rc, 0)
        status = self.k("status", "--json", ok=True).json()
        self.assertEqual(status["open_findings"]["medium"], 0)

    def test_38_set_rules(self):
        run = self.setup_finding()
        cases = [
            ("F-0001", "--status", "fixed"),                     # not a V0 status
            ("F-0001", "--status", "reviewed"),                  # system status on a finding
            ("F-0001", "--status", "dismissed"),                 # note required
            ("SYS-auth", "--status", "open"),                    # finding status on a system
            ("SYS-auth", "--status", "deprecated"),              # note required
            ("SYS-auth", "--confidence", "verified"),            # reserved
            ("SYS-auth", "--confidence", "executed"),            # reserved
            ("SYS-auth", "--confidence", "bogus"),
            ("SYS-auth",),                                       # nothing to change
            (run, "--status", "ended"),                          # runs are not settable
            ("ANC-0001", "--status", "draft"),
            ("SYS-missing", "--status", "draft"),
        ]
        before = (self.read("findings/F-0001.md"), self.read("systems/auth.md"), self.read("runs/%s/summary.md" % run))
        for args in cases:
            res = self.k("set", *args, "--run", run)
            self.assertEqual(res.rc, 1, args)
        after = (self.read("findings/F-0001.md"), self.read("systems/auth.md"), self.read("runs/%s/summary.md" % run))
        self.assertEqual(before, after)

    def test_39_set_system_status_and_confidence(self):
        run = self.ready()
        res = self.k("set", "SYS-auth", "--status", "reviewed", "--confidence", "observed", "--run", run)
        self.assertEqual(res.rc, 0, res.err)
        text = self.read("systems/auth.md")
        self.assertIn("status: reviewed", text)
        self.assertIn("confidence: observed", text)
        self.k("set", "SYS-auth", "--confidence", "inferred", "--run", run, ok=True)  # may also lower
        self.assertIn("confidence: inferred", self.read("systems/auth.md"))
        res = self.k("set", "SYS-auth", "--status", "reviewed", "--run", run, ok=True)
        self.assertIn("unchanged", res.out)
        self.k("set", "SYS-auth", "--status", "deprecated", "--note", "superseded", "--run", run, ok=True)
        self.assertEqual(self.k("check").rc, 0)

    def test_40_set_preserves_anchor_block_and_later_anchors(self):
        run = self.setup_finding()
        self.k("set", "F-0001", "--status", "triaged", "--run", run, ok=True)
        self.k("anchor", "add", "F-0001", "src/auth.py", "--start-line", "1", "--end-line", "2", "--run", run, ok=True)
        self.k("set", "F-0001", "--status", "open", "--run", run, ok=True)
        text = self.read("findings/F-0001.md")
        self.assertEqual(text.count("study:anchors:begin"), 1)
        self.assertEqual(text.count("## Status log"), 1)
        self.assertEqual(text.count("tester: status"), 2)
        self.assertEqual(self.k("check").rc, 0)

    def test_41_set_requires_open_run(self):
        run = self.setup_finding()
        self.end_run(run)
        res = self.k("set", "F-0001", "--status", "triaged", "--run", run)
        self.assertEqual(res.rc, 1)
        self.assertIn("status: open", self.read("findings/F-0001.md"))


class TestFindingLifecycle(KitCase):
    """Findings go out of date: closed by observation (resolved, obsolete, dismissed), never by inference."""

    def setUp(self):
        super().setUp()
        self.run1 = self.ready()  # RUN-0001 open, SYS-auth, ANC-0001 on src/auth.py lines 4-7
        self.k("finding", "No expiry check", "--severity", "high", "--anchor", "ANC-0001", "--run", self.run1, ok=True)

    def reinspect(self, run, note="re-read the code"):
        res = self.k("evidence", "add", "F-0001", "--type", "source-inspection", "--result", note,
                     "--anchor", "ANC-0001", "--run", run, ok=True)
        return res.out.strip().splitlines()[-1]

    def new_run(self):
        self.k("run", "end", "--id", self.run1, "--summary", "first pass", ok=True)
        return self.start_run("recheck")

    def test_f01_vocabulary(self):
        self.assertEqual(kernel.KIND_STATUSES["finding"], ("open", "triaged", "resolved", "obsolete", "dismissed"))
        self.k("set", "F-0001", "--status", "fixed", "--note", "x", "--run", self.run1, ok=False)
        schema = json.loads((RES / "schema.json").read_text(encoding="utf-8"))
        self.assertTrue(set(kernel.KIND_STATUSES["finding"]) <= set(schema["$defs"]["status"]["enum"]))

    def test_f02_resolved_needs_note_and_evidence(self):
        ev = self.reinspect(self.run1)
        res = self.k("set", "F-0001", "--status", "resolved", "--evidence", ev, "--run", self.run1, ok=False)
        self.assertIn("--note", res.err)
        res = self.k("set", "F-0001", "--status", "resolved", "--note", "gone", "--run", self.run1, ok=False)
        self.assertIn("--evidence", res.err)
        self.assertIn("evidence add F-0001", res.err)  # tells the agent what to do
        self.assertIn("status: open", self.read("findings/F-0001.md"))
        for status in ("resolved", "obsolete"):
            self.k("set", "F-0001", "--status", status, "--note", "n", "--evidence", "EV-0099", "--run", self.run1,
                   ok=False)
        self.k("set", "F-0001", "--status", "resolved", "--note", "n", "--evidence", "bogus", "--run", self.run1,
               ok=False)

    def test_f03_evidence_must_be_fresh_and_about_this_finding(self):
        ev_old = self.reinspect(self.run1)
        run2 = self.new_run()
        res = self.k("set", "F-0001", "--status", "resolved", "--note", "n", "--evidence", ev_old, "--run", run2,
                     ok=False)
        self.assertIn("must be recorded in this run", res.err)
        other = self.k("evidence", "add", "SYS-auth", "--type", "source-inspection", "--result", "r",
                       "--anchor", "ANC-0001", "--run", run2, ok=True).out.strip().splitlines()[-1]
        res = self.k("set", "F-0001", "--status", "obsolete", "--note", "n", "--evidence", other, "--run", run2,
                     ok=False)
        self.assertIn("subject F-0001", res.err)
        ev_new = self.reinspect(run2)
        self.k("set", "F-0001", "--status", "resolved", "--note", "n", "--evidence", ev_new, "--run", run2, ok=True)

    def test_f04_status_log_records_evidence_and_commit(self):
        ev = self.reinspect(self.run1)
        self.k("set", "F-0001", "--status", "resolved", "--note", "expiry now checked", "--evidence", ev,
               "--run", self.run1, ok=True)
        text = self.read("findings/F-0001.md")
        self.assertIn("status: resolved", text)
        line = [l for l in text.splitlines() if "open -> resolved" in l][0]
        self.assertIn("expiry now checked", line)
        self.assertIn("evidence " + ev, line)
        if HAVE_GIT:
            head = git(self.root, "rev-parse", "HEAD").stdout.decode().strip()
            self.assertIn("source as of HEAD " + head[:12], line)
        events = self.read("runs/%s/events.jsonl" % self.run1)
        self.assertIn('"evidence": ["%s"]' % ev, events)
        self.assertEqual(self.k("check").rc, 0)

    def test_f05_obsolete_and_dismissed(self):
        ev = self.reinspect(self.run1)
        self.k("set", "F-0001", "--status", "obsolete", "--note", "module deleted", "--evidence", ev,
               "--run", self.run1, ok=True)
        self.assertIn("status: obsolete", self.read("findings/F-0001.md"))
        self.k("set", "F-0001", "--status", "open", "--note", "restored", "--run", self.run1, ok=True)
        self.k("set", "F-0001", "--status", "dismissed", "--run", self.run1, ok=False)  # note required
        self.k("set", "F-0001", "--status", "dismissed", "--note", "intended", "--run", self.run1, ok=True)

    def test_f06_transitions(self):
        self.k("set", "F-0001", "--status", "triaged", "--run", self.run1, ok=True)
        self.k("set", "F-0001", "--status", "dismissed", "--note", "intended", "--run", self.run1, ok=True)
        res = self.k("set", "F-0001", "--status", "triaged", "--run", self.run1, ok=False)
        self.assertIn("reopen it first", res.err)
        self.k("set", "F-0001", "--status", "open", "--run", self.run1, ok=False)  # reopening needs a note
        self.k("set", "F-0001", "--status", "open", "--note", "it is back", "--run", self.run1, ok=True)
        self.k("set", "F-0001", "--status", "triaged", "--run", self.run1, ok=True)
        log = [l for l in self.read("findings/F-0001.md").splitlines() if "tester: status" in l]
        self.assertEqual(len(log), 4)

    def test_f07_evidence_flag_only_on_finding_status_changes(self):
        ev = self.reinspect(self.run1)
        self.k("set", "SYS-auth", "--status", "reviewed", "--evidence", ev, "--run", self.run1, ok=False)
        self.k("set", "F-0001", "--confidence", "observed", "--evidence", ev, "--run", self.run1, ok=False)

    def test_f08_drift_flags_open_findings_for_recheck_and_closing_quiets_them(self):
        self.k("run", "end", "--id", self.run1, "--summary", "s", ok=True)
        text = (self.root / "src" / "auth.py").read_text(encoding="utf-8")
        (self.root / "src" / "auth.py").write_text("# moved\n" + text, encoding="utf-8")
        res = self.k("check", "--json", ok=True)
        self.assertIn("finding-needs-recheck", self.problem_codes(res))
        status = self.k("status", "--json", ok=True).json()
        self.assertTrue(any("F-0001 may be out of date" in a for a in status["attention"]))
        self.assertEqual(self.k("show", "F-0001", ok=True).rc, 0)
        self.assertIn("status: open", self.read("findings/F-0001.md"))  # the kernel never closes by itself
        run2 = self.start_run("recheck")
        ev = self.reinspect(run2)
        self.k("set", "F-0001", "--status", "resolved", "--note", "n", "--evidence", ev, "--run", run2, ok=True)
        codes = self.problem_codes(self.k("check", "--json", ok=True))
        self.assertNotIn("finding-needs-recheck", codes)
        self.assertIn("anchor-stale", codes)  # SYS-auth still owns that anchor and still drifts

    def test_f09_anchors_owned_by_closed_findings_and_deprecated_docs_are_quiet(self):
        self.k("new", "system", "old", "--title", "Old", "--run", self.run1, ok=True)
        self.k("anchor", "add", "SYS-old", "src/auth.py", "--symbol", "verify_token", "--start-line", "4",
               "--end-line", "7", "--run", self.run1, ok=True)
        self.k("anchor", "add", "F-0001", "src/auth.py", "--start-line", "1", "--end-line", "3", "--run", self.run1,
               ok=True)
        self.k("set", "SYS-old", "--status", "deprecated", "--note", "removed", "--run", self.run1, ok=True)
        ev = self.reinspect(self.run1)
        self.k("set", "F-0001", "--status", "resolved", "--note", "n", "--evidence", ev, "--run", self.run1, ok=True)
        self.k("run", "end", "--id", self.run1, "--summary", "s", ok=True)
        text = (self.root / "src" / "auth.py").read_text(encoding="utf-8")
        (self.root / "src" / "auth.py").write_text("# moved\n" + text, encoding="utf-8")
        res = self.k("check", "--json", ok=True)
        stale = [p["message"] for p in res.json()["problems"] if p["code"] == "anchor-stale"]
        self.assertEqual(len(stale), 1, stale)  # only SYS-auth's ANC-0001 is reported
        self.assertIn("ANC-0001", stale[0])

    def test_f10_closed_findings_stay_searchable_and_listed(self):
        ev = self.reinspect(self.run1)
        self.k("set", "F-0001", "--status", "resolved", "--note", "n", "--evidence", ev, "--run", self.run1, ok=True)
        self.assertIn("F-0001", self.k("search", "expiry", ok=True).out)
        self.assertIn("F-0001", self.k("list", "--kind", "finding", "--status", "resolved", ok=True).out)
        status = self.k("status", "--json", ok=True).json()
        self.assertEqual(status["finding_status"]["resolved"], 1)
        self.assertEqual(status["open_findings"]["high"], 0)


EXPECTED_TOOLS = (
    "study_run_start", "study_run_end", "study_new_system", "study_new_flow", "study_finding", "study_claim_add",
    "study_set", "study_orient", "study_anchor_add", "study_codebase_add", "study_codebase_remove",
    "study_codebase_list", "study_codebase_scan", "study_evidence_add", "study_show", "study_list",
    "study_search", "study_graph", "study_coverage", "study_check", "study_rebuild", "study_status")


class TestTools(KitCase):
    """Commands exported as agent tool definitions; `tools call` runs one from JSON without a shell."""

    def call(self, name, arguments=None, raw=None):
        res = self.k("tools", "call", name, "--args", raw if raw is not None else json.dumps(arguments or {}))
        env = res.json()
        self.assertEqual(set(env), {"ok", "exit_code", "output", "error"})
        self.assertEqual(res.rc, env["exit_code"])
        self.assertEqual(env["ok"], env["exit_code"] == 0)
        return env

    def test_t01_list_needs_no_study_directory_and_covers_every_command(self):
        res = self.k("tools", "list", ok=True)
        self.assertFalse(self.study.exists())
        tools = json.loads(res.out)
        self.assertEqual(tuple(t["name"] for t in tools), EXPECTED_TOOLS)
        for t in tools:
            self.assertEqual(set(t), {"name", "description", "input_schema"})
            self.assertTrue(t["description"].endswith("."), t["name"])
            schema = t["input_schema"]
            self.assertEqual(schema["type"], "object")
            self.assertFalse(schema["additionalProperties"])
            self.assertTrue(set(schema["required"]) <= set(schema["properties"]))
            for prop in schema["properties"].values():
                self.assertIn(prop["type"], ("string", "integer", "boolean", "array"))
        self.assertNotIn("study_init", EXPECTED_TOOLS)
        self.assertFalse(any(n.startswith("study_tools") for n in EXPECTED_TOOLS))

    def test_t02_schemas_match_the_cli(self):
        tools = {t["name"]: t["input_schema"] for t in json.loads(self.k("tools", "list", ok=True).out)}
        finding = tools["study_finding"]
        self.assertEqual(set(finding["required"]), {"title", "severity", "run"})
        self.assertEqual(finding["properties"]["severity"]["enum"], list(kernel.SEVERITIES))
        self.assertEqual(finding["properties"]["anchor"]["type"], "array")
        self.assertEqual(tools["study_search"]["properties"]["limit"]["type"], "integer")
        self.assertEqual(tools["study_search"]["properties"]["limit"]["default"], 20)
        self.assertEqual(tools["study_claim_add"]["properties"]["inference"]["type"], "boolean")
        self.assertEqual(set(tools["study_set"]["required"]), {"id", "run"})
        self.assertIn("resolved", tools["study_set"]["properties"]["status"]["description"])
        self.assertEqual(tools["study_status"]["properties"], {})
        for name, schema in tools.items():
            for key in schema["properties"]:
                self.assertNotIn(key, ("root", "study_dir", "agent", "json_out", "max_event_bytes"), name)

    def test_t03_openai_format(self):
        anthropic = json.loads(self.k("tools", "list", ok=True).out)
        openai = json.loads(self.k("tools", "list", "--format", "openai", ok=True).out)
        self.assertEqual(len(openai), len(anthropic))
        for a, o in zip(anthropic, openai):
            self.assertEqual(o["type"], "function")
            self.assertEqual(o["function"], {"name": a["name"], "description": a["description"],
                                             "parameters": a["input_schema"]})

    def test_t04_call_runs_a_full_study_loop(self):
        self.init()
        env = self.call("study_run_start", {"goal": "Map auth"})
        self.assertTrue(env["ok"], env)
        run = env["output"]["id"]
        self.assertEqual(run, "RUN-0001")
        self.assertTrue(self.call("study_new_system", {"slug": "auth", "title": "Auth", "run": run})["ok"])
        env = self.call("study_anchor_add", {"document_id": "SYS-auth", "path": "src/auth.py",
                                             "symbol": "verify_token", "start_line": 4, "end_line": 7, "run": run})
        self.assertTrue(env["ok"], env)
        env = self.call("study_evidence_add", {"subject": "SYS-auth", "type": "source-inspection",
                                               "result": "read verify_token", "anchor": ["ANC-0001"],
                                               "limitation": ["callers not read", "tests not read"], "run": run})
        self.assertTrue(env["ok"], env)
        env = self.call("study_claim_add", {"document_id": "SYS-auth", "text": "verify_token rejects empty input",
                                            "anchor": ["ANC-0001"], "evidence": ["EV-0001"], "inference": True,
                                            "run": run})
        self.assertTrue(env["ok"], env)
        self.assertIn("Inference: verify_token rejects empty input", self.read("systems/auth.md"))
        env = self.call("study_finding", {"title": "No expiry", "severity": "high", "anchor": ["ANC-0001"],
                                          "run": run})
        self.assertTrue(env["ok"], env)
        self.assertTrue(self.call("study_set", {"id": "F-0001", "status": "triaged", "run": run})["ok"])
        env = self.call("study_check")
        self.assertTrue(env["ok"], env)
        env = self.call("study_run_end", {"id": run, "summary": "done", "next": "trace refresh",
                                          "open_question": ["who calls it?", "is there a cache?"]})
        self.assertTrue(env["ok"], env)
        self.assertIn("status: triaged", self.read("findings/F-0001.md"))
        self.assertEqual(self.k("check").rc, 0)

    def test_t05_output_matches_the_cli_json(self):
        self.ready()
        env = self.call("study_list", {"kind": "system"})
        self.assertEqual(env["output"], self.k("list", "--kind", "system", "--json", ok=True).json())
        env = self.call("study_search", {"words": ["Authentication"], "limit": 5})
        self.assertEqual(env["output"], self.k("search", "Authentication", "--limit", "5", "--json", ok=True).json())
        env = self.call("study_status")
        self.assertIn("finding_status", env["output"])

    def test_t06_values_are_never_parsed_as_options_or_shell(self):
        run = self.ready()
        nasty = "--not-a-flag; $(touch pwned) `id` \"quoted\" and\nnewline"
        env = self.call("study_claim_add", {"document_id": "SYS-auth", "text": "-- leading dashes ok",
                                            "anchor": ["ANC-0001"], "run": run})
        self.assertTrue(env["ok"], env)
        self.assertIn("-- leading dashes ok", self.read("systems/auth.md"))
        env = self.call("study_finding", {"title": "- starts with a dash", "severity": "low", "run": run})
        self.assertTrue(env["ok"], env)
        self.assertIn("- starts with a dash", self.read("findings/F-0001.md"))
        env = self.call("study_run_end", {"id": run, "summary": nasty})
        self.assertFalse((self.root / "pwned").exists())
        self.assertFalse(Path("pwned").exists())

    def test_t07_bad_calls_return_an_envelope_and_change_nothing(self):
        run = self.ready()
        before = {p.relative_to(self.study).as_posix(): p.read_bytes()
                  for p in self.study.rglob("*") if p.is_file() and p.suffix in (".md", ".jsonl")}
        cases = [
            ("study_nope", {}, "unknown tool"),
            ("study_init", {}, "unknown tool"),
            ("study_finding", {"title": "x", "run": run}, "needs argument 'severity'"),
            ("study_finding", {"title": "x", "severity": "urgent", "run": run}, "must be one of"),
            ("study_finding", {"title": "x", "severity": "low", "run": run, "bogus": 1}, "no argument 'bogus'"),
            ("study_finding", {"title": 5, "severity": "low", "run": run}, "must be a string"),
            ("study_finding", {"title": "x", "severity": "low", "run": run, "anchor": "ANC-0001"},
             "list of strings"),
            ("study_finding", {"title": "x", "severity": "low", "run": run, "anchor": []}, "at least one"),
            ("study_search", {"words": ["a"], "limit": "3"}, "must be an integer"),
            ("study_search", {"words": ["a"], "limit": True}, "must be an integer"),
            ("study_claim_add", {"document_id": "SYS-auth", "text": "t", "run": run, "inference": "yes"},
             "true or false"),
        ]
        for name, arguments, expect in cases:
            env = self.call(name, arguments)
            self.assertFalse(env["ok"], (name, arguments))
            self.assertEqual(env["exit_code"], 1)
            self.assertIsNone(env["output"])
            self.assertIn(expect, env["error"], (name, arguments))
        for raw, expect in (("not json", "not valid JSON"), ("[]", "JSON object"), ('"x"', "JSON object")):
            env = self.call("study_status", raw=raw)
            self.assertFalse(env["ok"])
            self.assertIn(expect, env["error"])
        after = {p.relative_to(self.study).as_posix(): p.read_bytes()
                 for p in self.study.rglob("*") if p.is_file() and p.suffix in (".md", ".jsonl")}
        self.assertEqual(before.keys(), after.keys())
        for key in before:
            if not key.endswith("events.jsonl"):
                self.assertEqual(before[key], after[key], key)

    def test_t08_kernel_errors_are_reported_in_the_envelope(self):
        self.ready()
        env = self.call("study_set", {"id": "F-0099", "status": "triaged", "run": "RUN-0001"})
        self.assertFalse(env["ok"])
        self.assertEqual(env["exit_code"], 1)
        self.assertIn("unknown document", env["error"])
        env = self.call("study_run_end", {"id": "RUN-0042", "summary": "s"})
        self.assertFalse(env["ok"])

    def test_t09_global_options_are_not_tool_arguments_but_pass_through(self):
        run = self.ready()
        env = self.call("study_status", {"root": str(self.root)})
        self.assertFalse(env["ok"])  # a model cannot redirect the kernel
        self.call("study_new_flow", {"slug": "login", "title": "Login", "run": run})
        self.assertIn("updated_by: tester", self.read("flows/login.md"))  # --agent given to tools call is honored

    def test_t10_args_from_stdin(self):
        self.ready()
        saved = sys.stdin
        sys.stdin = io.StringIO(json.dumps({"words": ["Authentication"]}))
        try:
            res = self.k("tools", "call", "study_search", "--args", "-", ok=True)
        finally:
            sys.stdin = saved
        self.assertEqual(res.json()["output"][0]["id"], "SYS-auth")

    def test_t11_workspace_tools(self):
        self.init()
        env = self.call("study_codebase_scan")
        self.assertTrue(env["ok"], env)
        env = self.call("study_codebase_list")
        self.assertTrue(env["ok"], env)
        self.assertIsInstance(env["output"], (dict, list))
        env = self.call("study_codebase_remove", {"path": "nowhere"})
        self.assertFalse(env["ok"])
        self.assertTrue(env["error"])


class TestLineEndings(KitCase):
    """Records, templates and index files must survive CRLF checkouts (Windows, core.autocrlf)."""

    def to_crlf(self, *globs, bom=False):
        for pattern in globs:
            for path in self.study.glob(pattern):
                data = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
                path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + data)

    def populate(self):
        run = self.ready()
        self.k("evidence", "add", "SYS-auth", "--type", "source-inspection", "--result", "r",
               "--anchor", "ANC-0001", "--run", run, ok=True)
        self.k("claim", "add", "SYS-auth", "verify_token rejects empty input", "--anchor", "ANC-0001", "--run", run,
               ok=True)
        self.k("finding", "No expiry", "--severity", "high", "--anchor", "ANC-0001", "--run", run, ok=True)
        self.k("new", "flow", "login", "--title", "Login", "--run", run, ok=True)
        self.end_run(run)

    def test_l01_crlf_records_are_read_edited_and_indexed(self):
        self.populate()
        self.to_crlf("systems/*.md", "flows/*.md", "findings/*.md", "runs/*/summary.md", "runs/*/*.jsonl")
        self.assertEqual(self.k("check").rc, 0, self.k("check").err)
        self.k("rebuild", ok=True)
        self.assertIn("SYS-auth", self.k("search", "Authentication", ok=True).out)
        self.assertEqual(len(self.k("list", "--json", ok=True).json()), 4)
        run = self.start_run("second")
        self.k("claim", "add", "SYS-auth", "a second claim", "--anchor", "ANC-0001", "--run", run, ok=True)
        self.k("set", "F-0001", "--status", "triaged", "--run", run, ok=True)
        self.k("anchor", "add", "SYS-auth", "src/auth.py", "--start-line", "1", "--end-line", "3", "--run", run, ok=True)
        self.k("evidence", "add", "F-0001", "--type", "source-inspection", "--result", "r2", "--run", run, ok=True)
        self.end_run(run)
        self.assertEqual(self.k("check").rc, 0, self.k("check").err)
        self.assertIn("CLM-0002", self.read("systems/auth.md"))

    def test_l02_bom_and_lone_cr_documents(self):
        self.populate()
        self.to_crlf("systems/*.md", "findings/*.md", bom=True)
        self.assertEqual(self.k("check").rc, 0, self.k("check").err)
        path = self.study / "flows" / "login.md"
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r"))  # classic Mac line endings
        self.assertEqual(self.k("check").rc, 0, self.k("check").err)

    def test_l03_crlf_templates_render(self):
        self.init()
        self.to_crlf("templates/*.md")
        run = self.start_run()
        self.k("new", "system", "auth", "--title", "Auth", "--run", run, ok=True)
        self.k("new", "flow", "login", "--title", "Login", "--run", run, ok=True)
        self.k("finding", "x", "--severity", "low", "--run", run, ok=True)
        self.end_run(run)
        self.assertEqual(self.k("check").rc, 0, self.k("check").err)

    def test_l04_failed_run_start_leaves_no_stub(self):
        self.init()
        self.write("templates/run.md", "not a template\n")
        self.k("run", "start", "--goal", "g", ok=False)
        self.assertEqual(sorted(p.name for p in (self.study / "runs").iterdir()), [])
        self.assertEqual(self.k("check").rc, 0, self.k("check").err)


class TestCommandReference(KitCase):
    """One place to learn the syntax, so agents do not probe each command with --help."""

    def test_c01_commands_lists_every_command_without_a_study_dir(self):
        res = self.k("commands", ok=True)
        self.assertFalse(self.study.exists())
        for needle in ("run start --goal GOAL", "run end --id RUN-NNNN --summary SUMMARY",
                       "anchor add DOCUMENT_ID PATH", "finding TITLE --severity {low|medium|high|critical}",
                       "claim add DOCUMENT_ID TEXT", "set ID", "evidence add SUBJECT --type {source-inspection",
                       "codebase add [PATH ...]", "search WORD ... [--limit LIMIT]", "tools call TOOL [--args ARGS]",
                       "status", "check", "commands"):
            self.assertIn(needle, res.out)
        for path, parser in kernel._parser_leaves(kernel.build_parser()):
            self.assertIn("  " + " ".join(path), res.out, path)  # every leaf command appears
        self.assertIn("resolved", res.out)  # the set line names the finding statuses
        self.assertIn("tools list", res.out)

    def test_c02_help_ends_with_the_same_table(self):
        out = io.StringIO()
        with self.assertRaises(SystemExit):
            with mock.patch("sys.stdout", out):
                kernel.build_parser().parse_args(["--help"])
        self.assertIn(kernel.command_reference(kernel.build_parser()), out.getvalue())

    def test_c03_errors_show_the_right_usage(self):
        self.init()
        res = self.k("anchor", "add", ok=False)
        self.assertTrue(res.err.startswith("error: the following arguments are required"))
        self.assertIn("usage: python .study/kernel.py anchor add DOCUMENT_ID PATH", res.err)
        self.assertIn("--run RUN-NNNN", res.err)
        res = self.k("run", ok=False)
        self.assertIn("usage: python .study/kernel.py run {start|end}", res.err)
        self.assertIn("commands)", res.err)
        res = self.k("bogus", ok=False)
        self.assertIn("invalid choice", res.err)
        self.assertIn("python .study/kernel.py commands", res.err)

    def test_c04_tool_errors_carry_the_usage_too(self):
        self.init()
        env = self.k("tools", "call", "study_search", "--args", "{}", ok=False).json()
        self.assertIn("needs argument 'words'", env["error"])  # validated before argparse; no usage needed
        res = self.k("search", ok=False)
        self.assertIn("usage: python .study/kernel.py search WORD ...", res.err)

    def test_c05_commands_is_not_a_tool(self):
        names = [t["name"] for t in json.loads(self.k("tools", "list", ok=True).out)]
        self.assertNotIn("study_commands", names)
        self.assertEqual(len(names), 22)


class TestOrient(KitCase):
    def populate(self):
        files = {
            "package.json": '{"name": "x", "main": "index.js", "bin": {"tool": "cli.js"}, "scripts": {"test": "jest"}}',
            "Makefile": "all:\n", "README.md": "# hi\n", "src/main.py": "print(1)\n", "src/util.go": "package x\n",
            "cmd/tool/main.go": "package main\n", "tests/test_a.py": "def test_a(): pass\n",
            "web/app.test.ts": "test('x', () => {})\n", ".github/workflows/ci.yml": "on: push\n",
            "node_modules/dep/index.js": "x\n", "venv/lib/x.py": "x\n", "build/out.c": "x\n",
        }
        for rel, text in files.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def test_42_orient_works_before_init_and_writes_nothing(self):
        self.populate()
        before = sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if ".git" not in p.parts)
        res = self.k("orient")
        self.assertEqual(res.rc, 0, res.err)
        self.assertFalse(self.study.exists())
        after = sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if ".git" not in p.parts)
        self.assertEqual(before, after)
        self.assertIn("candidates", res.out)
        self.assertEqual(self.k("status").rc, 1)  # other commands still need init

    def test_43_orient_json_contents(self):
        self.populate()
        self.init()
        data = self.k("orient", "--json", ok=True).json()
        langs = {x["language"]: x["files"] for x in data["languages"]}
        self.assertEqual(langs["Python"], 3)  # src/auth.py, src/main.py, tests/test_a.py
        self.assertEqual(langs["Go"], 2)
        self.assertEqual(langs["TypeScript"], 1)
        self.assertNotIn("C", langs)  # build/ is excluded
        tools = {x["tool"] for x in data["build_tools"]}
        self.assertTrue({"make", "npm/Node"} <= tools)
        self.assertEqual({x["path"] for x in data["test_locations"]}, {"tests"})
        self.assertEqual(data["test_file_matches"], 2)
        eps = data["entry_point_candidates"]
        self.assertIn("src/main.py", eps)
        self.assertIn("cmd/tool/main.go", eps)
        self.assertTrue(any("package.json main: index.js" in h for h in data["manifest_hints"]))
        self.assertTrue(any("scripts: test" in h for h in data["manifest_hints"]))
        self.assertEqual(data["docs"], ["README.md"])
        self.assertEqual(data["ci"], [".github/workflows/ci.yml"])
        self.assertNotIn("node_modules", json.dumps(data))
        self.assertIn("not verified", data["note"])
        self.assertEqual(data["study_documents"], {"system": 0, "flow": 0, "finding": 0, "run": 0})

    def test_44_orient_reflects_study_state_and_bounds_output(self):
        self.ready()
        data = self.k("orient", "--json", ok=True).json()
        self.assertEqual(data["study_documents"]["system"], 1)
        self.assertEqual(data["anchored_files"], 1)
        for i in range(40):
            d = self.root / ("pkg%02d" % i)
            d.mkdir()
            (d / "main.py").write_text("x = 1\n", encoding="utf-8")
        text = self.k("orient", ok=True).out
        self.assertIn("... and", text)
        self.assertLess(len(text.splitlines()), 70)

    def test_45_orient_tolerates_hostile_package_json(self):
        (self.root / "package.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.k("orient", "--json", ok=True).json()["manifest_hints"], [])
        (self.root / "package.json").write_text('{"main": 5, "bin": [1], "scripts": "x"}', encoding="utf-8")
        self.assertEqual(self.k("orient", "--json", ok=True).json()["manifest_hints"], [])


class TestSectionHelper(unittest.TestCase):
    def test_append_to_section_cases(self):
        body = "# T\n\n## Claims\n\n_(none recorded)_\n\n## Other\n\ntext\n"
        out = kernel.append_to_section(body, "Claims", "- one")
        self.assertIn("## Claims\n\n- one\n\n## Other", out)
        out2 = kernel.append_to_section(out, "Claims", "- two")
        self.assertIn("## Claims\n\n- one\n- two\n\n## Other\n\ntext", out2)
        block = "<!-- study:anchors:begin\n{}\nstudy:anchors:end -->\n"
        last = "# T\n\n## Log\n\n- a\n\n" + block
        out3 = kernel.append_to_section(last, "Log", "- b")
        self.assertIn("- a\n- b\n\n<!-- study:anchors:begin", out3)
        self.assertEqual(out3.count("study:anchors:begin"), 1)
        out4 = kernel.append_to_section("# T\n\n" + block, "New", "- x")
        self.assertTrue(out4.index("## New") < out4.index("study:anchors:begin"))
        out5 = kernel.append_to_section("# T\n", "New", "- x")
        self.assertTrue(out5.endswith("## New\n\n- x\n"))


@needs_git
class TestWorkspaceMode(KitCase):
    """Several repositories under one study root: a registry of codebases, per-codebase Git state."""

    use_git = False

    def setUp(self):
        super().setUp()
        # self.root is now a plain folder (no Git) that holds two repositories and a non-repo folder.
        shutil.rmtree(str(self.root / "src"))
        for name in ("api", "web"):
            (self.root / name / "src").mkdir(parents=True)
            shutil.copy(str(FIXTURE), str(self.root / name / "src" / "auth.py"))
            git(self.root / name, "init", "-q")
            git(self.root / name, "add", "-A")
            git(self.root / name, "commit", "-qm", "fixture " + name)
        (self.root / "notes").mkdir()
        (self.root / "notes" / "todo.txt").write_text("not a repo\n", encoding="utf-8")

    def head(self, name):
        return git(self.root / name, "rev-parse", "HEAD").stdout.decode().strip()

    def workspace(self):
        self.init()
        self.k("codebase", "add", "api", "web", ok=True)
        run = self.start_run()
        self.k("new", "system", "auth", "--title", "Authentication", "--run", run, ok=True)
        return run

    def test_w01_scan_finds_repos_without_registering(self):
        self.init()
        res = self.k("codebase", "scan", "--json", ok=True).json()
        self.assertEqual([c["path"] for c in res["candidates"]], ["api", "web"])
        self.assertFalse(any(c["registered"] for c in res["candidates"]))
        self.assertFalse((self.study / "codebases.json").exists())
        self.assertIn("single-repository mode", self.k("codebase", "list", ok=True).out)

    def test_w02_add_remove_list_roundtrip(self):
        self.init()
        res = self.k("codebase", "add", "--detected", ok=True)
        self.assertIn("registered api", res.out)
        self.assertIn("already registered api", self.k("codebase", "add", "api", ok=True).out)
        listed = self.k("codebase", "list", "--json", ok=True).json()
        self.assertEqual(listed["mode"], "workspace")
        self.assertEqual([c["path"] for c in listed["codebases"]], ["api", "web"])
        self.assertEqual(listed["codebases"][0]["head"], self.head("api"))
        removed = self.k("codebase", "remove", "web", ok=True)
        self.assertIn("no files or records were deleted", removed.out)
        self.assertTrue((self.root / "web" / "src" / "auth.py").is_file())
        self.k("codebase", "remove", "web", ok=False)
        self.k("codebase", "remove", "api", ok=True)
        self.assertEqual(self.k("codebase", "list", "--json", ok=True).json()["mode"], "single")
        obj = json.loads((self.study / "codebases.json").read_text(encoding="utf-8"))
        self.assertEqual(obj["codebases"], [])

    def test_w03_add_validation(self):
        self.init()
        self.k("codebase", "add", "notes", ok=False)  # not a Git work tree
        self.assertIn("not a Git work tree", self.k("codebase", "add", "notes").err)
        self.k("codebase", "add", "missing", ok=False)
        self.k("codebase", "add", "../elsewhere", ok=False)
        self.k("codebase", "add", ".study", ok=False)
        self.k("codebase", "add", ok=False)
        self.k("codebase", "add", str(self.root / "api"), ok=True)  # absolute path inside the root is fine
        self.k("codebase", "add", "notes", "--no-git", ok=True)
        rows = self.k("codebase", "list", "--json", ok=True).json()["codebases"]
        self.assertEqual({r["path"]: r["git"] for r in rows}, {"api": True, "notes": False})

    def test_w04_anchor_records_its_codebase_and_commit(self):
        run = self.workspace()
        self.k("anchor", "add", "SYS-auth", "api/src/auth.py", "--symbol", "verify_token",
               "--start-line", "4", "--end-line", "7", "--run", run, ok=True)
        self.k("anchor", "add", "SYS-auth", "web/src/auth.py", "--symbol", "verify_token",
               "--start-line", "4", "--end-line", "7", "--run", run, ok=True)
        anchors = {a["id"]: a for a in kernel.load_records(self._ctx(), evaluate=False).anchors}
        self.assertEqual((anchors["ANC-0001"]["repository"], anchors["ANC-0001"]["commit"]), ("api", self.head("api")))
        self.assertEqual((anchors["ANC-0002"]["repository"], anchors["ANC-0002"]["commit"]), ("web", self.head("web")))
        self.assertEqual(self.problem_codes(self.k("check", "--json", ok=True)), {"run-open"})

    def _ctx(self):
        ns = kernel.build_parser().parse_args(["status", "--root", str(self.root)])
        return kernel.build_context(ns, io.StringIO(), io.StringIO(), True)

    def test_w05_anchor_outside_every_codebase_is_refused(self):
        run = self.workspace()
        res = self.k("anchor", "add", "SYS-auth", "notes/todo.txt", "--run", run, ok=False)
        self.assertIn("not inside a registered codebase", res.err)
        self.assertIn("codebase add", res.err)

    def test_w06_dirty_file_only_marks_anchors_in_that_codebase(self):
        run = self.workspace()
        for name in ("api", "web"):
            self.k("anchor", "add", "SYS-auth", name + "/src/auth.py", "--run", run, ok=True)
        self.k("run", "end", "--id", run, "--summary", "x", ok=True)
        # api/src/auth.py is edited after the run: only the api anchor is stale.
        with open(str(self.root / "api" / "src" / "auth.py"), "a", encoding="utf-8") as fh:
            fh.write("\n# edit\n")
        states = {a["id"]: a["state"] for a in self.k("show", "SYS-auth", "--json", ok=True).json().get("anchors", [])}
        self.assertEqual(states.get("ANC-0001"), "stale")
        self.assertEqual(states.get("ANC-0002"), "ok")

    def test_w07_source_change_guard_covers_each_codebase(self):
        run = self.workspace()
        (self.root / "web" / "src" / "new.py").write_text("x = 1\n", encoding="utf-8")  # untracked file
        res = self.k("run", "end", "--id", run, "--summary", "x", ok=False)
        self.assertIn("source state changed", res.err)
        self.assertIn("web:", res.out + res.err)
        self.assertNotIn("api:", res.err)
        self.assertIn("source_changed", self.read("runs/%s/summary.md" % run))

    def test_w08_clean_run_ends_cleanly_and_snapshot_lists_codebases(self):
        run = self.workspace()
        self.assertIn("Codebases: 2", self.read("runs/%s/summary.md" % run))
        self.k("run", "end", "--id", run, "--summary", "x", ok=True)
        self.assertIn("status: ended", self.read("runs/%s/summary.md" % run))

    def test_w09_changing_the_map_during_a_run_is_refused(self):
        run = self.workspace()
        res = self.k("codebase", "remove", "web", ok=False)
        self.assertIn(run, res.err)
        self.k("codebase", "add", "--no-git", "notes", ok=False)
        self.k("run", "end", "--id", run, "--summary", "x", ok=True)
        self.k("codebase", "remove", "web", ok=True)

    def test_w10_removed_or_missing_codebase_is_reported_not_fatal(self):
        run = self.workspace()
        self.k("anchor", "add", "SYS-auth", "web/src/auth.py", "--run", run, ok=True)
        self.k("run", "end", "--id", run, "--summary", "x", ok=True)
        self.k("codebase", "remove", "web", ok=True)
        res = self.k("check", "--json", ok=True)
        self.assertIn("anchor-codebase-unregistered", self.problem_codes(res))
        self.k("codebase", "add", "web", ok=True)
        self.assertNotIn("anchor-codebase-unregistered", self.problem_codes(self.k("check", "--json", ok=True)))
        shutil.rmtree(str(self.root / "web"))
        res = self.k("check", "--json", ok=True)  # warnings only
        self.assertIn("codebase-unavailable", self.problem_codes(res))
        self.assertIn("anchor-missing-file", self.problem_codes(res))
        status = self.k("status", "--json", ok=True).json()
        self.assertTrue(any("web" in a and "not a Git work tree" in a for a in status["attention"]))

    def test_w11_evidence_commit_follows_the_cited_codebase(self):
        run = self.workspace()
        self.k("anchor", "add", "SYS-auth", "api/src/auth.py", "--run", run, ok=True)
        self.k("anchor", "add", "SYS-auth", "web/src/auth.py", "--run", run, ok=True)
        self.k("evidence", "add", "SYS-auth", "--type", "source-inspection", "--result", "a",
               "--anchor", "ANC-0001", "--run", run, ok=True)
        self.k("evidence", "add", "SYS-auth", "--type", "source-inspection", "--result", "b",
               "--anchor", "ANC-0001", "ANC-0002", "--run", run, ok=True)
        self.k("evidence", "add", "SYS-auth", "--type", "source-inspection", "--result", "c", "--run", run, ok=True)
        lines = [json.loads(l) for l in self.read("runs/%s/evidence.jsonl" % run).splitlines()]
        self.assertEqual([l["repository_commit"] for l in lines], [self.head("api"), None, None])

    def test_w11b_closing_a_finding_records_each_codebase_head(self):
        run = self.workspace()
        for name in ("api", "web"):
            self.k("anchor", "add", "SYS-auth", name + "/src/auth.py", "--run", run, ok=True)
        self.k("finding", "x", "--severity", "low", "--anchor", "ANC-0001", "ANC-0002", "--run", run, ok=True)
        self.k("evidence", "add", "F-0001", "--type", "source-inspection", "--result", "r", "--run", run, ok=True)
        self.k("set", "F-0001", "--status", "resolved", "--note", "n", "--evidence", "EV-0001", "--run", run, ok=True)
        log = [l for l in (self.study / "findings" / "F-0001.md").read_text(encoding="utf-8").splitlines()
               if "open -> resolved" in l][0]
        self.assertIn("api %s" % self.head("api")[:12], log)
        self.assertIn("web %s" % self.head("web")[:12], log)

    def test_w12_coverage_and_orient_are_limited_to_registered_codebases(self):
        self.init()
        self.k("codebase", "add", "api", ok=True)
        cov = self.k("coverage", "--json", ok=True).json()
        self.assertEqual(cov["total"], 1)  # only api/src/auth.py; web and notes are ignored
        orient = self.k("orient", "--json", ok=True).json()
        self.assertEqual(orient["files"], 1)
        self.assertEqual([c["path"] for c in orient["codebases"]], ["api"])

    def test_w13_malformed_registry_is_a_clear_error(self):
        self.init()
        self.write("codebases.json", "{not json")
        res = self.k("status", ok=False)
        self.assertIn("codebases.json", res.err)
        self.write("codebases.json", json.dumps({"schema": "wrong", "codebases": []}))
        self.assertIn("schema", self.k("status", ok=False).err)
        self.write("codebases.json", json.dumps({"schema": kernel.CODEBASES_SCHEMA,
                                                  "codebases": [{"path": "../x"}]}))
        self.k("status", ok=False)

    def test_w14_workspace_root_may_itself_be_a_repo_with_nested_repos(self):
        git(self.root, "init", "-q")
        self.init()
        self.k("codebase", "add", ".", "api", ok=True)
        ctx = self._ctx()
        self.assertEqual(ctx.git.repository_for("api/src/auth.py"), "api")
        self.assertEqual(ctx.git.repository_for("notes/todo.txt"), ".")
        self.assertEqual(ctx.git.repository_for("apix/file"), ".")

    def test_w15_single_repo_behaviour_is_unchanged(self):
        # A normal repository with no registry must keep recording repository "." and its own HEAD.
        other = self.tmp / "single"
        (other / "src").mkdir(parents=True)
        shutil.copy(str(FIXTURE), str(other / "src" / "auth.py"))
        git(other, "init", "-q")
        git(other, "add", "-A")
        git(other, "commit", "-qm", "x")
        out, err = io.StringIO(), io.StringIO()
        def kk(*a):
            return kernel.main([str(x) for x in a] + ["--root", str(other)], stdout=out, stderr=err)
        self.assertEqual(kk("init"), 0)
        self.assertEqual(kk("run", "start", "--goal", "g"), 0)
        self.assertEqual(kk("new", "system", "auth", "--title", "A", "--run", "RUN-0001"), 0)
        self.assertEqual(kk("anchor", "add", "SYS-auth", "src/auth.py", "--run", "RUN-0001"), 0)
        text = (other / ".study" / "systems" / "auth.md").read_text(encoding="utf-8")
        self.assertIn('"repository": "."', text)
        self.assertIn(git(other, "rev-parse", "HEAD").stdout.decode().strip(), text)
        self.assertNotIn("codebases", (other / ".study" / "runs" / "RUN-0001" / "summary.md").read_text(encoding="utf-8"))


class TestKitHygiene(unittest.TestCase):
    def test_30_stdlib_only_and_python39_syntax(self):
        src = (PKG / "kernel.py").read_text(encoding="utf-8")
        tree = ast.parse(src, feature_version=(3, 9))
        allowed = {"__future__", "argparse", "contextlib", "dataclasses", "datetime", "hashlib", "io", "json", "os",
                   "re", "sqlite3", "subprocess", "sys", "tempfile", "pathlib", "typing"}
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertLessEqual(imported, allowed, imported - allowed)
        stdlib = getattr(sys, "stdlib_module_names", None)
        if stdlib:
            self.assertLessEqual(imported, set(stdlib))

    def test_30b_kit_files_are_consistent(self):
        self.assertTrue((PKG / "kernel.py").is_file())
        for name in kernel.KIT_FILES[1:] + tuple("templates/" + n for n in kernel.TEMPLATE_NAMES):
            self.assertTrue((RES / name).is_file(), name)
        for name in ("README.md", "examples/minimal/README.md", "pyproject.toml"):
            self.assertTrue((ROOT / name).is_file(), name)
        self.assertTrue((RES / "AGENTS.md").is_file())
        for stale in ("kernel.py", "PROTOCOL.md", "schema.json", "AGENTS.md", "templates"):
            self.assertFalse((ROOT / stale).exists(), "duplicate resource outside the package: " + stale)
        schema = json.loads((RES / "schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        defs = schema["$defs"]
        self.assertEqual(tuple(defs["confidence"]["enum"]), kernel.CONFIDENCE_LEVELS)
        self.assertEqual(tuple(defs["severity"]["enum"]), kernel.SEVERITIES)
        self.assertEqual(tuple(defs["evidenceType"]["enum"]), kernel.EVIDENCE_TYPES)
        self.assertEqual(tuple(defs["anchorState"]["enum"]), kernel.ANCHOR_STATES)
        self.assertEqual(set(defs["evidenceRecord"]["required"]), set(kernel.EVIDENCE_KEYS))
        self.assertEqual(set(defs["anchorRecord"]["required"]), set(kernel.ANCHOR_KEYS))
        self.assertLess(len((RES / "PROTOCOL.md").read_text(encoding="utf-8").split()), 2500)

    def test_30c_templates_render_valid_documents(self):
        values = {"id": "X", "title": "T", "area": "", "updated": "2026-01-01T00:00:00Z", "updated_by": "a",
                  "severity": "low", "confidence": "hypothesis", "anchors": [], "anchor_refs": "Anchors: none",
                  "goal": "g", "agent": "a", "started": "2026-01-01T00:00:00Z",
                  "initial_state_text": "-", "initial_state_json": "{}"}
        ids = {"system": "SYS-x", "flow": "FLOW-x", "finding": "F-0001", "run": "RUN-0001"}
        for kind, doc_id in ids.items():
            text = (RES / "templates" / (kind + ".md")).read_text(encoding="utf-8")
            values["id"] = doc_id
            rendered = kernel.render_template(text, values)
            kernel.check_rendered(rendered, kind, doc_id)
            body = kernel.strip_comments(rendered)
            self.assertEqual(kernel.CLAIM_LINE_RE.findall(body), [], kind)
            lowered = rendered.lower()
            self.assertIn("inference", lowered)
            self.assertIn("not inspected", lowered)

    def test_30d_required_template_sections(self):
        required = {
            "system": ["Purpose", "Boundaries", "Entry points", "Dependencies", "Observed behavior", "Claims",
                       "Invariants", "Open questions", "Not inspected", "Related flows", "Related findings"],
            "flow": ["Trigger", "Preconditions", "Steps", "Data and state", "External interactions",
                     "Failure paths", "Timeout/retry/concurrency", "Recovery", "Claims", "Open questions",
                     "Not inspected", "Related systems", "Related findings"],
            "finding": ["Summary", "Evidence", "Reasoning or reproduction", "Potential impact",
                        "Suggested investigation", "Limitations", "Not inspected", "Related systems and flows"],
            "run": ["Goal", "Initial source state", "Scope", "Activity summary", "Artifacts created or updated",
                    "Conclusions", "Limitations", "Not inspected", "Open questions", "Next steps",
                    "Final source state"],
        }
        for kind, sections in required.items():
            text = (RES / "templates" / (kind + ".md")).read_text(encoding="utf-8")
            headings = [l[3:].strip() for l in text.splitlines() if l.startswith("## ")]
            self.assertEqual(headings, sections, kind)


if __name__ == "__main__":
    unittest.main()
