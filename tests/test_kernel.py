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


class TestKitHygiene(unittest.TestCase):
    def test_30_stdlib_only_and_python39_syntax(self):
        src = (PKG / "kernel.py").read_text(encoding="utf-8")
        tree = ast.parse(src, feature_version=(3, 9))
        allowed = {"__future__", "argparse", "contextlib", "dataclasses", "datetime", "hashlib", "json", "os",
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
