#!/usr/bin/env python3
"""Engineering Study Kit V0: a read-only-first codebase study kernel.

Study mode only. Python 3.9+, standard library only.

The kernel never modifies the target source tree. Every write it performs is
confined to the study directory (default: <root>/.study). The single,
brief-mandated exception is init appending four ignore patterns to
.git/info/exclude.

Sections in this file:
  1. Constants and errors
  2. Small utilities
  3. Filesystem safety (StudyFS)
  4. Git inspection (GitInspector)
  5. Frontmatter parsing and validation
  6. Documents, anchors, evidence, templates
  7. Record loading and analysis
  8. SQLite index
  9. Context and shared command helpers
 10. Commands
 11. CLI parser and dispatch
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

VERSION = "0.1.0"

# =============================================================================
# 1. Constants and errors
# =============================================================================

CONFIDENCE_LEVELS = ("hypothesis", "inferred", "observed", "corroborated", "executed", "verified")
V0_CREATABLE_CONFIDENCE = ("hypothesis", "inferred", "observed")
SEVERITIES = ("low", "medium", "high", "critical")
EVIDENCE_TYPES = (
    "source-inspection",
    "configuration-inspection",
    "test-run",
    "static-analysis",
    "runtime-observation",
    "manual-reproduction",
)
ANCHOR_STATES = ("ok", "stale", "missing_file", "missing_symbol", "unchecked")
KINDS = ("system", "flow", "finding", "run")
KIND_STATUSES = {
    "system": ("draft", "reviewed", "deprecated"),
    "flow": ("draft", "reviewed", "deprecated"),
    "finding": ("open", "triaged", "resolved", "obsolete", "dismissed"),
    "run": ("open", "ended", "source_changed"),
}
# Finding lifecycle. A finding is closed in one of three ways, each meaning something different:
#   resolved  the problem was real and the source no longer has it (fixed by anyone, anywhere)
#   obsolete  the code was removed or rewritten, so the finding no longer applies
#   dismissed it was never a problem, or it is intended
# Study mode never changes source, so "resolved" and "obsolete" record a re-inspection (evidence from the
# current run), never an inference from a commit message. A closed finding can only be reopened.
FINDING_CLOSED = ("resolved", "obsolete", "dismissed")
FINDING_NEEDS_EVIDENCE = ("resolved", "obsolete")
FINDING_TRANSITIONS = {
    "open": ("triaged", "resolved", "obsolete", "dismissed"),
    "triaged": ("open", "resolved", "obsolete", "dismissed"),
    "resolved": ("open",),
    "obsolete": ("open",),
    "dismissed": ("open",),
}
QUIET_STATUSES = FINDING_CLOSED + ("deprecated",)  # drift in their anchors is expected, not a warning
KIND_DIRS = {"system": "systems", "flow": "flows", "finding": "findings"}
KIND_ID_PREFIX = {"system": "SYS-", "flow": "FLOW-"}

EVIDENCE_SCHEMA = "engineering-study-evidence/v1"
EVENT_SCHEMA = "engineering-study-event/v1"
DEFAULT_EVENT_MAX_BYTES = 16 * 1024
TEMPLATE_NAMES = ("system.md", "flow.md", "finding.md", "run.md")
KIT_FILES = ("kernel.py", "PROTOCOL.md", "schema.json")

CODEBASES_FILE = "codebases.json"
CODEBASES_SCHEMA = "engineering-study-codebases/v1"
SCAN_MAX_DEPTH = 3

COVERAGE_EXCLUDED_DIRS = (
    ".git", ".study", "node_modules", "__pycache__", "venv", ".venv", "dist", "build", "target",
)
GIT_EXCLUDE_ENTRIES = (
    ".study/study.db",
    ".study/study.db-wal",
    ".study/study.db-shm",
    ".study/scratch/",
)
ALLOWED_GIT_SUBCOMMANDS = ("rev-parse", "status")  # read-only inspection only

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
ID_RE_BY_KIND = {
    "system": re.compile(r"^SYS-[a-z0-9]+(?:-[a-z0-9]+)*$"),
    "flow": re.compile(r"^FLOW-[a-z0-9]+(?:-[a-z0-9]+)*$"),
    "finding": re.compile(r"^F-\d{4,}$"),
    "run": re.compile(r"^RUN-\d{4,}$"),
}
RUN_ID_RE = re.compile(r"^RUN-\d{4,}$")
FINDING_ID_RE = re.compile(r"^F-\d{4,}$")
ANC_ID_RE = re.compile(r"^ANC-\d{4,}$")
EV_ID_RE = re.compile(r"^EV-\d{4,}$")
CLM_ID_RE = re.compile(r"^CLM-\d{4,}$")
_ID_FRAGMENTS = (
    r"SYS-[a-z0-9]+(?:-[a-z0-9]+)*",
    r"FLOW-[a-z0-9]+(?:-[a-z0-9]+)*",
    r"F-\d{4,}",
    r"RUN-\d{4,}",
    r"ANC-\d{4,}",
    r"CLM-\d{4,}",
    r"EV-\d{4,}",
)
ANY_ID_RE = re.compile("^(?:" + "|".join(_ID_FRAGMENTS) + ")$")
CLAIM_LINE_RE = re.compile(r"^[ \t]*[-*][ \t]+(CLM-\d{4,})\b", re.M)
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
ANCHOR_BLOCK_RE = re.compile(r"<!-- study:anchors:begin\n(.*?)study:anchors:end -->", re.S)
PLACEHOLDER_RE = re.compile(r"\{\{([a-z_]+)\}\}")


class KitError(Exception):
    """A user-facing error. Rendered as 'error: <message>' on stderr, exit 1."""


@dataclasses.dataclass(frozen=True)
class Problem:
    severity: str  # "error" or "warning"
    code: str
    message: str
    path: Optional[str] = None
    fatal: bool = False  # fatal problems abort rebuild

    def to_dict(self) -> Dict[str, Any]:
        return {"severity": self.severity, "code": self.code, "message": self.message, "path": self.path}

    def format(self) -> str:
        where = "%s: " % self.path if self.path else ""
        return "%s [%s] %s%s" % (self.severity.upper(), self.code, where, self.message)


def problem_sort_key(p: Problem) -> Tuple[int, str, str, str]:
    return (0 if p.severity == "error" else 1, p.path or "", p.code, p.message)


# =============================================================================
# 2. Small utilities
# =============================================================================


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not TS_RE.match(value):
        return False
    try:
        datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


def encode_text(text: str) -> bytes:
    return text.replace("\r\n", "\n").encode("utf-8")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def split_lines(text: str) -> List[str]:
    """Split on \\n after newline normalization; a trailing newline adds no line."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def fingerprint_lines(lines: Sequence[str], start: int, end: int) -> str:
    """SHA-256 over selected lines: UTF-8, \\n joins, trailing whitespace stripped per line."""
    selected = [line.rstrip() for line in lines[start - 1:end]]
    return sha256_text("\n".join(selected))


def clean_line(value: Optional[str], what: str, max_len: int = 500) -> str:
    if value is None:
        raise KitError("%s is required" % what)
    text = value.strip()
    if not text:
        raise KitError("%s must not be empty" % what)
    if any(ch in text for ch in ("\r", "\n", "\x00")):
        raise KitError("%s must be a single line" % what)
    if len(text) > max_len:
        raise KitError("%s is too long (max %d characters)" % (what, max_len))
    return text


def validate_slug(slug: str) -> str:
    if not isinstance(slug, str) or not slug:
        raise KitError("slug must not be empty")
    if len(slug) > 64 or not SLUG_RE.match(slug):
        raise KitError(
            "invalid slug %r: use lowercase letters, digits and single hyphens (max 64 characters)" % slug
        )
    return slug


def truncate_utf8(text: str, max_bytes: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    marker = "...[truncated]"
    keep = max(0, max_bytes - len(marker.encode("utf-8")))
    return data[:keep].decode("utf-8", errors="ignore") + marker


def truncate_detail(value: Any, max_bytes: int) -> Any:
    if isinstance(value, str):
        return truncate_utf8(value, max_bytes)
    if isinstance(value, list):
        return [truncate_detail(v, max_bytes) for v in value]
    if isinstance(value, dict):
        return {str(k): truncate_detail(v, max_bytes) for k, v in value.items()}
    return value


def md_safe(text: str) -> str:
    """Keep user text from creating headings or comments inside generated Markdown."""
    text = text.replace("<!--", "&lt;!--")
    return "\n".join(("\\" + line) if line.lstrip().startswith("#") else line for line in text.split("\n"))


def comment_safe_json(obj: Any) -> str:
    """Single-line JSON that can never contain '-->' or '<!--'."""
    return json.dumps(obj, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")


def next_number(names: Sequence[str], prefix: str) -> int:
    pattern = re.compile(r"^%s-(\d+)" % re.escape(prefix))
    best = 0
    for name in names:
        m = pattern.match(name)
        if m:
            best = max(best, int(m.group(1)))
    return best + 1


def normalize_repo_relpath(raw: Any, what: str = "path") -> str:
    """Validate a repository-relative POSIX path string; return it normalized."""
    if not isinstance(raw, str) or raw == "":
        raise KitError("%s must be a non-empty string" % what)
    if "\x00" in raw:
        raise KitError("%s contains a NUL byte" % what)
    if "\\" in raw:
        raise KitError("%s must use POSIX separators ('/'): %r" % (what, raw))
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise KitError("%s must be repository-relative, not absolute: %r" % (what, raw))
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts:
        raise KitError("%s resolves to the repository root: %r" % (what, raw))
    if ".." in parts:
        raise KitError("%s must not contain '..': %r" % (what, raw))
    return "/".join(parts)


def resolve_inside(base: Path, rel: str) -> Path:
    """Resolve symlinks of base/rel and require the result to stay inside base."""
    base_real = Path(os.path.realpath(str(base)))
    target = Path(os.path.realpath(str(base_real.joinpath(*rel.split("/")))))
    if target != base_real and not is_relative_to(target, base_real):
        raise KitError("path escapes the repository: %s" % rel)
    return target


def normalize_area(raw: Optional[str]) -> str:
    if raw is None or raw == "":
        return ""
    return normalize_repo_relpath(raw, "area")


def _fsync(fh: Any) -> None:
    try:
        os.fsync(fh.fileno())
    except OSError:
        pass


# =============================================================================
# 3. Filesystem safety
# =============================================================================


class StudyFS:
    """All kernel writes go through here; each path is canonicalized first."""

    def __init__(self, root: Path, study: Path) -> None:
        self.root = Path(os.path.realpath(str(root)))
        self.study = Path(os.path.realpath(str(study)))
        if self.study == self.root or not is_relative_to(self.study, self.root):
            raise KitError("study directory must resolve inside the repository root: %s" % self.study)

    def _check(self, path: Any, action: str) -> Path:
        real = Path(os.path.realpath(str(path)))
        if real != self.study and not is_relative_to(real, self.study):
            raise KitError("refusing to %s outside the study directory: %s" % (action, path))
        return real

    def check_write(self, path: Any) -> Path:
        return self._check(path, "write")

    def check_read(self, path: Any) -> Path:
        return self._check(path, "read")

    def read_bytes(self, path: Any) -> bytes:
        return self.check_read(path).read_bytes()

    def mkdirs(self, path: Any) -> None:
        self.check_write(path).mkdir(parents=True, exist_ok=True)

    def mkdir_exclusive(self, path: Any) -> None:
        self.check_write(path)
        os.mkdir(str(path))  # FileExistsError propagates for collision retry

    def create_exclusive(self, path: Any, data: bytes) -> None:
        self.check_write(path)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        fd = os.open(str(path), flags, 0o644)  # FileExistsError propagates
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                _fsync(fh)
        except BaseException:
            try:
                os.unlink(str(path))
            except OSError:
                pass
            raise

    def atomic_write(self, path: Any, data: bytes) -> None:
        real = self.check_write(path)
        fd, tmp = tempfile.mkstemp(dir=str(real.parent), prefix=".tmp-", suffix=".part")
        try:
            self.check_write(tmp)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                _fsync(fh)
            try:
                os.chmod(tmp, 0o644)
            except OSError:
                pass
            os.replace(tmp, str(real))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def append_bytes(self, path: Any, data: bytes) -> None:
        """Append only. Never rewrites preceding bytes."""
        real = self.check_write(path)
        prefix = b""
        try:
            size = real.stat().st_size
        except FileNotFoundError:
            size = 0
        if size > 0:
            with open(str(real), "rb") as fh:
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    prefix = b"\n"
        with open(str(real), "ab") as fh:
            fh.write(prefix + data)
            fh.flush()
            _fsync(fh)


# =============================================================================
# 4. Git inspection (read-only)
# =============================================================================


class GitInspector:
    """Runs only `git rev-parse` and `git status`, with optional locks disabled
    so the Git index is never refreshed or rewritten."""

    multi = False

    def __init__(self, root: Path, study_rel: Optional[str], git_exe: str = "git") -> None:
        self.root = root
        self.study_rel = study_rel
        self.git_exe = git_exe
        self._available: Optional[bool] = None

    def _run(self, args: Sequence[str], timeout: int = 120) -> Optional[Tuple[int, bytes]]:
        if not args or args[0] not in ALLOWED_GIT_SUBCOMMANDS:
            raise KitError("internal error: git subcommand not allowed: %r" % (list(args)[:1],))
        cmd = [self.git_exe, "-c", "core.fsmonitor=false", "--no-optional-locks", "-C", str(self.root)]
        cmd.extend(args)
        env = dict(os.environ)
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            env.pop(key, None)
        env["GIT_OPTIONAL_LOCKS"] = "0"
        env["LC_ALL"] = "C"
        try:
            proc = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=timeout, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.returncode, proc.stdout

    def available(self) -> bool:
        if self._available is None:
            result = self._run(["rev-parse", "--is-inside-work-tree"])
            self._available = bool(result and result[0] == 0 and result[1].strip() == b"true")
        return self._available

    def head(self) -> Optional[str]:
        if not self.available():
            return None
        result = self._run(["rev-parse", "--verify", "--quiet", "HEAD"])
        if result and result[0] == 0:
            sha = result[1].decode("ascii", errors="replace").strip()
            if SHA_RE.match(sha):
                return sha
        return None

    def prefix(self) -> str:
        result = self._run(["rev-parse", "--show-prefix"])
        if result and result[0] == 0:
            return result[1].decode("utf-8", errors="replace").strip()
        return ""

    def _status_entries(self) -> List[str]:
        args = ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", "."]
        if self.study_rel:
            args.append(":(exclude)%s" % self.study_rel)
        result = self._run(args)
        if result is None or result[0] != 0:
            raise KitError("git status failed; cannot inspect working-tree state")
        return [t.decode("utf-8", errors="replace") for t in result[1].split(b"\0") if t]

    def status_tokens(self) -> List[str]:
        return sorted(self._status_entries())

    def dirty_paths(self) -> Set[str]:
        """Root-relative POSIX paths with working-tree or index modifications."""
        prefix = self.prefix()
        entries = self._status_entries()
        dirty: Set[str] = set()
        i = 0
        while i < len(entries):
            entry = entries[i]
            i += 1
            if len(entry) < 4:
                continue
            xy, path = entry[:2], entry[3:]
            paths = [path]
            if ("R" in xy or "C" in xy) and i < len(entries):
                paths.append(entries[i])
                i += 1
            for p in paths:
                if prefix:
                    if not p.startswith(prefix):
                        continue
                    p = p[len(prefix):]
                dirty.add(p)
        return dirty

    def snapshot(self) -> Dict[str, Any]:
        if not self.available():
            return {"git": False, "head": None, "porcelain": []}
        return {"git": True, "head": self.head(), "porcelain": self.status_tokens()}

    def exclude_file(self) -> Optional[Path]:
        if not self.available():
            return None
        result = self._run(["rev-parse", "--git-path", "info/exclude"])
        if not result or result[0] != 0:
            return None
        raw = result[1].decode("utf-8", errors="replace").strip()
        if not raw:
            return None
        path = Path(raw)
        if not path.is_absolute():
            path = self.root / path
        return path

    # Codebase-aware interface shared with MultiGit. A single repository is the codebase ".".
    def repository_for(self, rel: str) -> Optional[str]:
        return "."

    def head_for(self, repo: str) -> Optional[str]:
        return self.head()

    def available_for(self, repo: str) -> bool:
        return self.available()

    def codebase_rows(self) -> List[Dict[str, Any]]:
        return []


class MultiGit:
    """Workspace mode: the study root holds several codebases (usually Git repositories).

    Each registered codebase is inspected on its own, read-only. Paths stay relative to the
    study root; every anchor records the codebase it belongs to in its `repository` field."""

    multi = True

    def __init__(self, root: Path, study: Path, paths: Sequence[str], git_exe: str = "git") -> None:
        self.root = root
        self.paths = sorted(paths)
        try:
            root_study_rel: Optional[str] = study.relative_to(root).as_posix()
        except ValueError:
            root_study_rel = None
        self.root_git = GitInspector(root, root_study_rel, git_exe)
        self.members: Dict[str, GitInspector] = {}
        for p in self.paths:
            base = root if p == "." else root.joinpath(*p.split("/"))
            study_rel: Optional[str] = None
            if study == base or is_relative_to(study, base):
                study_rel = study.relative_to(base).as_posix()
            self.members[p] = GitInspector(base, study_rel, git_exe)

    def available(self) -> bool:
        return any(g.available() for g in self.members.values())

    def head(self) -> Optional[str]:
        return None  # there is no single HEAD; see codebase_rows()

    def repository_for(self, rel: str) -> Optional[str]:
        best: Optional[str] = None
        for p in self.paths:
            if p == "." or rel == p or rel.startswith(p + "/"):
                if best is None or len(p) > len(best) or best == ".":
                    best = p
        return best

    def head_for(self, repo: str) -> Optional[str]:
        g = self.members.get(repo)
        return g.head() if g else None

    def available_for(self, repo: str) -> bool:
        g = self.members.get(repo)
        return bool(g and g.available())

    def dirty_paths(self) -> Set[str]:
        out: Set[str] = set()
        for p, g in self.members.items():
            if not g.available():
                continue
            for d in g.dirty_paths():
                out.add(d if p == "." else p + "/" + d)
        return out

    def snapshot(self) -> Dict[str, Any]:
        subs = {p: g.snapshot() for p, g in self.members.items()}
        return {"git": any(x["git"] for x in subs.values()), "head": None, "porcelain": [], "codebases": subs}

    def exclude_file(self) -> Optional[Path]:
        return self.root_git.exclude_file()

    def codebase_rows(self) -> List[Dict[str, Any]]:
        rows = []
        for p, g in self.members.items():
            ok = g.available()
            rows.append({"path": p, "git": ok, "head": g.head() if ok else None,
                         "changed_entries": len(g.status_tokens()) if ok else None})
        return rows


def diff_snapshots(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    """Describe the difference between two source-state snapshots (no file contents)."""
    if "codebases" in before or "codebases" in after:
        empty = {"git": False, "head": None, "porcelain": []}
        bs, as_ = before.get("codebases", {}), after.get("codebases", {})
        changed, reasons = False, []
        added: List[str] = []
        removed: List[str] = []
        for name in sorted(set(bs) | set(as_)):
            sub = diff_snapshots(bs.get(name, empty), as_.get(name, empty))
            if sub["changed"]:
                changed = True
                reasons.append("%s: %s" % (name, sub["reason"] or "state differs"))
                added.extend("%s: %s" % (name, x) for x in sub["added"])
                removed.extend("%s: %s" % (name, x) for x in sub["removed"])
        return {"changed": changed, "reason": "; ".join(reasons), "head_before": None, "head_after": None,
                "added": added, "removed": removed}
    if bool(before.get("git")) != bool(after.get("git")):
        return {"changed": True, "reason": "git availability changed", "head_before": before.get("head"),
                "head_after": after.get("head"), "added": [], "removed": []}
    b = set(before.get("porcelain", []))
    a = set(after.get("porcelain", []))
    added, removed = sorted(a - b), sorted(b - a)
    head_changed = before.get("head") != after.get("head")
    return {
        "changed": bool(added or removed or head_changed),
        "reason": "head changed" if head_changed else ("working tree changed" if (added or removed) else ""),
        "head_before": before.get("head"),
        "head_after": after.get("head"),
        "added": added,
        "removed": removed,
    }


# =============================================================================
# 5. Frontmatter parsing and validation
# =============================================================================

FM_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
FM_ORDER = ("id", "kind", "title", "status", "confidence", "severity", "area",
            "updated", "updated_by", "anchors", "links")
LIST_KEYS = ("anchors", "links")
COMMON_REQUIRED = ("id", "kind", "title", "status", "updated", "updated_by")
KIND_REQUIRED = {
    "system": ("confidence",),
    "flow": ("confidence",),
    "finding": ("confidence", "severity"),
    "run": ("goal", "agent", "started"),
}


def parse_scalar(raw: str) -> str:
    raw = raw.strip()
    if raw == "":
        return ""
    if raw[0] == "{":
        raise KitError("nested maps are not supported in frontmatter")
    if raw in ("|", ">", "|-", ">-", "|+", ">+"):
        raise KitError("multiline values are not supported in frontmatter")
    if raw[0] == '"':
        if len(raw) < 2 or raw[-1] != '"':
            raise KitError("unterminated quoted string in frontmatter")
        try:
            value = json.loads(raw)
        except ValueError:
            raise KitError("malformed quoted string in frontmatter: %s" % raw)
        if not isinstance(value, str):
            raise KitError("malformed quoted string in frontmatter: %s" % raw)
        return value
    return raw


def parse_inline_list(raw: str) -> List[str]:
    raw = raw.strip()
    if not raw.startswith("[") or not raw.endswith("]") or len(raw) < 2:
        raise KitError("malformed inline list in frontmatter: %s" % raw)
    inner = raw[1:-1].strip()
    if inner == "":
        return []
    items: List[str] = []
    buf: List[str] = []
    in_quote = False
    escaped = False
    for ch in inner:
        if in_quote:
            buf.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_quote = False
        elif ch == '"':
            in_quote = True
            buf.append(ch)
        elif ch == ",":
            items.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if in_quote:
        raise KitError("unterminated quote in frontmatter list")
    items.append("".join(buf))
    out: List[str] = []
    for item in items:
        item = item.strip()
        if item == "":
            raise KitError("empty item in frontmatter list")
        if item[0] in "[{":
            raise KitError("nested structures are not supported in frontmatter lists")
        if item[0] != '"' and any(c in item for c in "[]{}"):
            raise KitError("malformed frontmatter list item: %s" % item)
        out.append(parse_scalar(item))
    return out


def parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """Parse the supported YAML-like subset. Returns (fields, body)."""
    if text.startswith("\ufeff"):
        text = text[1:]
    text = text.replace("\r\n", "\n")
    lines = text.split("\n")
    if not lines or lines[0] != "---":
        raise KitError("missing opening '---' frontmatter delimiter on line 1")
    end = None
    for i in range(1, len(lines)):
        if lines[i] == "---":
            end = i
            break
    if end is None:
        raise KitError("unterminated frontmatter: closing '---' delimiter not found")
    fm: Dict[str, Any] = {}
    for offset, line in enumerate(lines[1:end], start=2):
        if line.strip() == "":
            continue
        if line[0] in (" ", "\t"):
            raise KitError("line %d: indented line (nested or multiline values are not supported)" % offset)
        idx = line.find(":")
        if idx <= 0:
            raise KitError("line %d: expected 'key: value'" % offset)
        key, rest = line[:idx], line[idx + 1:]
        if not FM_KEY_RE.match(key):
            raise KitError("line %d: invalid key %r" % (offset, key))
        if rest != "" and not rest.startswith(" "):
            raise KitError("line %d: expected a space after ':'" % offset)
        if key in fm:
            raise KitError("line %d: duplicate key %r" % (offset, key))
        value = rest.strip()
        try:
            fm[key] = parse_inline_list(value) if value.startswith("[") else parse_scalar(value)
        except KitError as exc:
            raise KitError("line %d: %s" % (offset, exc))
    return fm, "\n".join(lines[end + 1:])


def format_scalar(value: str) -> str:
    if not isinstance(value, str):
        raise KitError("frontmatter value must be a string")
    if "\n" in value or "\r" in value:
        raise KitError("frontmatter values must be a single line")
    needs_quote = value == "" or value != value.strip() or value[0] in '["{|>' or value.startswith("'")
    return json.dumps(value, ensure_ascii=False) if needs_quote else value


def format_list(values: Sequence[str]) -> str:
    out = []
    for item in values:
        if "\n" in item or "\r" in item:
            raise KitError("frontmatter list items must be a single line")
        needs_quote = item == "" or item != item.strip() or any(c in item for c in ',[]{}"')
        out.append(json.dumps(item, ensure_ascii=False) if needs_quote else item)
    return "[" + ", ".join(out) + "]"


def format_frontmatter(fm: Dict[str, Any]) -> str:
    keys = [k for k in FM_ORDER if k in fm] + sorted(k for k in fm if k not in FM_ORDER)
    lines = ["---"]
    for key in keys:
        value = fm[key]
        rendered = format_list(value) if isinstance(value, list) else format_scalar(value)
        lines.append("%s: %s" % (key, rendered) if rendered != "" else "%s:" % key)
    lines.append("---")
    return "\n".join(lines) + "\n"


def validate_frontmatter(fm: Dict[str, Any]) -> List[str]:
    kind = fm.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        return ["unknown or missing kind: %r (expected one of %s)" % (kind, ", ".join(KINDS))]
    problems: List[str] = []
    for key in COMMON_REQUIRED + KIND_REQUIRED[kind]:
        if key not in fm:
            problems.append("missing required field: %s" % key)
    for key, value in fm.items():
        if key in LIST_KEYS:
            if not isinstance(value, list):
                problems.append("field '%s' must be an inline list" % key)
        elif not isinstance(value, str):
            problems.append("field '%s' must be a scalar string" % key)
    if problems:
        return problems
    if not ID_RE_BY_KIND[kind].match(fm["id"]):
        problems.append("id %r is not valid for kind %r" % (fm["id"], kind))
    if fm["status"] not in KIND_STATUSES[kind]:
        problems.append("status %r is not allowed for kind %r (allowed: %s)"
                        % (fm["status"], kind, ", ".join(KIND_STATUSES[kind])))
    if not fm["title"].strip():
        problems.append("title must not be empty")
    if "confidence" in fm:
        if fm["confidence"] not in CONFIDENCE_LEVELS:
            problems.append("confidence %r is not in the vocabulary (%s)"
                            % (fm["confidence"], ", ".join(CONFIDENCE_LEVELS)))
        elif fm["confidence"] == "verified":
            problems.append("confidence 'verified' is reserved for a future verification extension")
    if "severity" in fm and fm["severity"] not in SEVERITIES:
        problems.append("severity %r is not one of %s" % (fm["severity"], ", ".join(SEVERITIES)))
    if not valid_timestamp(fm["updated"]):
        problems.append("updated must be an RFC 3339 UTC timestamp (YYYY-MM-DDTHH:MM:SSZ)")
    if kind == "run":
        if not valid_timestamp(fm["started"]):
            problems.append("started must be an RFC 3339 UTC timestamp")
        if fm.get("ended", "") != "" and not valid_timestamp(fm["ended"]):
            problems.append("ended must be empty or an RFC 3339 UTC timestamp")
    if fm.get("area", "") != "":
        try:
            normalize_repo_relpath(fm["area"], "area")
        except KitError as exc:
            problems.append(str(exc))
    for item in fm.get("anchors", []):
        if not ANC_ID_RE.match(item):
            problems.append("anchors list contains an invalid anchor ID: %r" % item)
    for item in fm.get("links", []):
        if not ANY_ID_RE.match(item):
            problems.append("links list contains an invalid ID: %r" % item)
    return problems


# =============================================================================
# 6. Documents, anchors, evidence, templates
# =============================================================================

ANCHOR_KEYS = ("id", "repository", "commit", "path", "symbol", "start_line", "end_line",
               "fingerprint", "created_at")
EVIDENCE_KEYS = ("schema", "id", "subject", "type", "repository_commit", "anchors", "producer",
                 "timestamp", "result", "limitations", "command", "exit_code")


def strip_comments(body: str) -> str:
    return COMMENT_RE.sub("", body)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_anchor_record(rec: Any) -> List[str]:
    if not isinstance(rec, dict):
        return ["anchor record must be a JSON object"]
    problems = ["missing anchor field: %s" % k for k in ANCHOR_KEYS if k not in rec]
    problems += ["unknown anchor field: %s" % k for k in sorted(rec) if k not in ANCHOR_KEYS]
    if problems:
        return problems
    if not (isinstance(rec["id"], str) and ANC_ID_RE.match(rec["id"])):
        problems.append("invalid anchor id: %r" % (rec["id"],))
    if not isinstance(rec["repository"], str):
        problems.append("anchor repository must be a string")
    if rec["commit"] is not None and not (isinstance(rec["commit"], str) and SHA_RE.match(rec["commit"])):
        problems.append("anchor commit must be a full Git SHA or null")
    try:
        normalize_repo_relpath(rec["path"], "anchor path")
    except KitError as exc:
        problems.append(str(exc))
    if rec["symbol"] is not None and not isinstance(rec["symbol"], str):
        problems.append("anchor symbol must be a string or null")
    if not (_is_int(rec["start_line"]) and _is_int(rec["end_line"])
            and 1 <= rec["start_line"] <= rec["end_line"]):
        problems.append("anchor lines must be integers with 1 <= start_line <= end_line")
    if not (isinstance(rec["fingerprint"], str) and re.match(r"^[0-9a-f]{64}$", rec["fingerprint"])):
        problems.append("anchor fingerprint must be a SHA-256 hex digest")
    if not valid_timestamp(rec["created_at"]):
        problems.append("anchor created_at must be an RFC 3339 UTC timestamp")
    return problems


def parse_anchor_block(body: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    blocks = ANCHOR_BLOCK_RE.findall(body)
    if not blocks:
        return [], []
    if len(blocks) > 1:
        return [], ["multiple anchor record blocks found"]
    records: List[Dict[str, Any]] = []
    problems: List[str] = []
    for n, line in enumerate(blocks[0].split("\n"), start=1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            problems.append("anchor block line %d is not valid JSON" % n)
            continue
        msgs = validate_anchor_record(rec)
        if msgs:
            problems.extend("anchor block line %d: %s" % (n, m) for m in msgs)
        else:
            records.append(rec)
    return records, problems


def render_anchor_block(records: Sequence[Dict[str, Any]]) -> str:
    return "<!-- study:anchors:begin\n" + "".join(comment_safe_json(r) + "\n" for r in records) \
        + "study:anchors:end -->"


def set_anchor_block(body: str, records: Sequence[Dict[str, Any]]) -> str:
    block = render_anchor_block(records)
    if ANCHOR_BLOCK_RE.search(body):
        return ANCHOR_BLOCK_RE.sub(lambda m: block, body, count=1)
    return body.rstrip("\n") + "\n\n" + block + "\n"


def replace_section(body: str, heading: str, content: str) -> str:
    pattern = re.compile(r"(^## " + re.escape(heading) + r"[ \t]*\n)(.*?)(?=^## |\Z)", re.S | re.M)
    match = pattern.search(body)
    new_content = content.strip("\n")
    if not match:
        return body.rstrip("\n") + "\n\n## %s\n\n%s\n" % (heading, new_content)
    return body[:match.start()] + match.group(1) + "\n" + new_content + "\n\n" + body[match.end():]


_ANCHOR_BLOCK_START = "<!-- study:anchors:begin"
_PLACEHOLDER_LINE_RE = re.compile(r"^_\(.*\)_$")


def append_to_section(body: str, heading: str, line: str) -> str:
    """Append one line to a '## heading' section, dropping its '_(...)_' placeholder.

    The section ends at the next '## ' heading, at the study:anchors block, or at the end of the
    body, so a trailing anchor block is never swallowed. A missing section is created.
    """
    anchor_at = body.find(_ANCHOR_BLOCK_START)
    match = re.search(r"^## " + re.escape(heading) + r"[ \t]*\n", body, re.M)
    if not match:
        insert_at = anchor_at if anchor_at != -1 else len(body)
        head, tail = body[:insert_at].rstrip("\n"), body[insert_at:]
        sep = "\n\n" if head else ""
        return head + sep + "## %s\n\n%s\n" % (heading, line) + ("\n" + tail if tail else "")
    start = match.end()
    nxt = re.search(r"^## ", body[start:], re.M)
    end = start + nxt.start() if nxt else len(body)
    if anchor_at != -1 and start <= anchor_at < end:
        end = anchor_at
    kept = [ln for ln in body[start:end].split("\n") if not _PLACEHOLDER_LINE_RE.match(ln.strip())]
    content = "\n".join(kept).strip("\n")
    content = (content + "\n" + line) if content else line
    rest = body[end:]
    return body[:start] + "\n" + content + "\n" + ("\n" + rest if rest else "")


def validate_evidence_record(rec: Any) -> List[str]:
    if not isinstance(rec, dict):
        return ["evidence record must be a JSON object"]
    problems = ["missing evidence field: %s" % k for k in EVIDENCE_KEYS if k not in rec]
    problems += ["unknown evidence field: %s" % k for k in sorted(rec) if k not in EVIDENCE_KEYS]
    if problems:
        return problems
    if rec["schema"] != EVIDENCE_SCHEMA:
        problems.append("schema must be %r" % EVIDENCE_SCHEMA)
    if not (isinstance(rec["id"], str) and EV_ID_RE.match(rec["id"])):
        problems.append("invalid evidence id: %r" % (rec["id"],))
    if not (isinstance(rec["subject"], str) and ANY_ID_RE.match(rec["subject"])):
        problems.append("invalid evidence subject: %r" % (rec["subject"],))
    if rec["type"] not in EVIDENCE_TYPES:
        problems.append("invalid evidence type: %r" % (rec["type"],))
    if rec["repository_commit"] is not None and not (
            isinstance(rec["repository_commit"], str) and SHA_RE.match(rec["repository_commit"])):
        problems.append("repository_commit must be a full Git SHA or null")
    if not (isinstance(rec["anchors"], list) and all(isinstance(a, str) and ANC_ID_RE.match(a)
                                                     for a in rec["anchors"])):
        problems.append("anchors must be a list of anchor IDs")
    if not (isinstance(rec["producer"], str) and rec["producer"].strip()):
        problems.append("producer must be a non-empty string")
    if not valid_timestamp(rec["timestamp"]):
        problems.append("timestamp must be an RFC 3339 UTC timestamp")
    if not (isinstance(rec["result"], str) and rec["result"].strip()):
        problems.append("result must be a non-empty string")
    if not (isinstance(rec["limitations"], list) and all(isinstance(x, str) for x in rec["limitations"])):
        problems.append("limitations must be a list of strings")
    if rec["command"] is not None and not isinstance(rec["command"], str):
        problems.append("command must be a string or null")
    if rec["exit_code"] is not None and not _is_int(rec["exit_code"]):
        problems.append("exit_code must be an integer or null")
    return problems


# ---- symbol heuristics (conservative text matching, not compiler-accurate) ----

_DEF_KEYWORDS = r"(?:def|class|function|func|fn|struct|enum|trait|interface|type|impl|sub|proc)"


def symbol_tail(symbol: str) -> str:
    tail = re.split(r"::|\.|#", symbol)[-1]
    if not tail:
        raise KitError("invalid symbol: %r" % symbol)
    return tail


def validate_symbol(symbol: str) -> str:
    if not symbol or len(symbol) > 200 or re.search(r"\s", symbol):
        raise KitError("invalid symbol %r: must be non-empty, without whitespace, at most 200 characters" % symbol)
    symbol_tail(symbol)
    return symbol


def locate_symbol(lines: Sequence[str], symbol: str) -> Tuple[str, List[int]]:
    """Return ('definition'|'textual'|'none', 1-based line numbers). Heuristic only."""
    name = re.escape(symbol_tail(symbol))
    word = re.compile(r"(?<![A-Za-z0-9_$])" + name + r"(?![A-Za-z0-9_$])")
    definition = re.compile(
        r"^\s*(?:[A-Za-z_]+\s+){0,3}" + _DEF_KEYWORDS + r"\s+(?:\([^)]*\)\s*)?" + name + r"(?![A-Za-z0-9_$])"
    )
    assignment = re.compile(
        r"^\s*(?:(?:export|const|let|var)\s+)*" + name + r"\s*[:=]\s*(?:async\s+)?(?:function\b|\()"
    )
    defs = [i + 1 for i, line in enumerate(lines) if definition.match(line) or assignment.match(line)]
    if defs:
        return "definition", defs
    anys = [i + 1 for i, line in enumerate(lines) if word.search(line)]
    if anys:
        return "textual", anys
    return "none", []


def evaluate_anchor(ctx: "Context", rec: Dict[str, Any], dirty: Set[str]) -> str:
    """Mechanical anchor state. 'ok' never means semantically correct."""
    try:
        rel = normalize_repo_relpath(rec["path"], "anchor path")
        if rel.split("/")[0] == ".git":
            return "unchecked"
        real = resolve_inside(ctx.root, rel)
        if real == ctx.study or is_relative_to(real, ctx.study):
            return "unchecked"
    except KitError:
        return "unchecked"
    if not real.is_file():
        return "missing_file"
    try:
        text = real.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return "unchecked"
    lines = split_lines(text)
    symbol = rec.get("symbol")
    if symbol:
        try:
            kind, _hits = locate_symbol(lines, symbol)
        except KitError:
            return "unchecked"
        if kind == "none":
            return "missing_symbol"
    start, end = rec["start_line"], rec["end_line"]
    if start < 1 or start > end or end > len(lines):
        return "stale"
    if fingerprint_lines(lines, start, end) != rec["fingerprint"]:
        return "stale"
    if rel in dirty:
        return "stale"  # working-tree modification of an anchored file
    return "ok"


# ---- templates ----

def load_template(ctx: "Context", name: str) -> str:
    path = ctx.study / "templates" / name
    try:
        return ctx.fs.read_bytes(path).decode("utf-8")
    except (OSError, KitError):
        raise KitError("template missing or unreadable: %s (run init --force to restore)" % ctx.display(path))


def render_template(text: str, values: Dict[str, Any]) -> str:
    lines = text.split("\n")
    if not lines or lines[0] != "---":
        raise KitError("template has no frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise KitError("template frontmatter is not terminated")
    head = "\n".join(lines[:end + 1])
    body = "\n".join(lines[end + 1:])

    def lookup(name: str) -> Any:
        if name not in values:
            raise KitError("template placeholder not provided: %s" % name)
        return values[name]

    def sub_head(m: "re.Match[str]") -> str:
        v = lookup(m.group(1))
        return format_list(v) if isinstance(v, list) else format_scalar(v)

    def sub_body(m: "re.Match[str]") -> str:
        v = lookup(m.group(1))
        return md_safe(", ".join(v) if isinstance(v, list) else v)

    return PLACEHOLDER_RE.sub(sub_head, head) + "\n" + PLACEHOLDER_RE.sub(sub_body, body)


def check_rendered(text: str, kind: str, expected_id: str) -> None:
    try:
        fm, _ = parse_frontmatter(text)
    except KitError as exc:
        raise KitError("internal template error: %s" % exc)
    problems = validate_frontmatter(fm)
    if problems or fm.get("id") != expected_id or fm.get("kind") != kind:
        raise KitError("internal template error: %s" % (problems[0] if problems else "id/kind mismatch"))


# =============================================================================
# 7. Record loading and analysis
# =============================================================================


@dataclasses.dataclass
class Records:
    docs: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    anchors: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    links: List[Tuple[str, str, str]] = dataclasses.field(default_factory=list)
    runs: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    evidence: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    claims: List[Tuple[str, str]] = dataclasses.field(default_factory=list)
    run_meta: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    problems: List[Problem] = dataclasses.field(default_factory=list)

    def fatal_problems(self) -> List[Problem]:
        return [p for p in self.problems if p.fatal]


def list_dir_sorted(path: Path) -> List[Path]:
    try:
        return sorted(path.iterdir(), key=lambda p: p.name)
    except (FileNotFoundError, NotADirectoryError):
        return []


def doc_path(ctx: "Context", doc_id: str) -> Path:
    if ID_RE_BY_KIND["system"].match(doc_id):
        return ctx.study / "systems" / (doc_id[len("SYS-"):] + ".md")
    if ID_RE_BY_KIND["flow"].match(doc_id):
        return ctx.study / "flows" / (doc_id[len("FLOW-"):] + ".md")
    if FINDING_ID_RE.match(doc_id):
        return ctx.study / "findings" / (doc_id + ".md")
    if RUN_ID_RE.match(doc_id):
        return ctx.study / "runs" / doc_id / "summary.md"
    raise KitError("not a document ID (expected SYS-*, FLOW-*, F-NNNN or RUN-NNNN): %s" % doc_id)


def read_doc_file(ctx: "Context", path: Path) -> Tuple[Dict[str, Any], str]:
    where = ctx.display(path)
    try:
        text = ctx.fs.read_bytes(path).decode("utf-8")
    except UnicodeDecodeError:
        raise KitError("%s: not valid UTF-8" % where)
    except OSError as exc:
        raise KitError("cannot read %s: %s" % (where, exc))
    try:
        fm, body = parse_frontmatter(text)
    except KitError as exc:
        raise KitError("%s: %s" % (where, exc))
    problems = validate_frontmatter(fm)
    if problems:
        raise KitError("%s: %s" % (where, problems[0]))
    return fm, body


def write_doc_file(ctx: "Context", path: Path, fm: Dict[str, Any], body: str) -> None:
    ctx.fs.atomic_write(path, encode_text(format_frontmatter(fm) + body))


def _expected_doc_id(kind: str, path: Path) -> Optional[str]:
    if kind == "run":
        return path.parent.name if RUN_ID_RE.match(path.parent.name) else None
    stem = path.stem
    if kind == "finding":
        return stem if FINDING_ID_RE.match(stem) else None
    return KIND_ID_PREFIX[kind] + stem if SLUG_RE.match(stem) else None


def _parse_doc(ctx: "Context", path: Path, kind: str, rec: Records) -> Optional[Dict[str, Any]]:
    rel = path.relative_to(ctx.study).as_posix()

    def fatal(code: str, message: str) -> None:
        rec.problems.append(Problem("error", code, message, rel, True))

    try:
        raw = ctx.fs.read_bytes(path)
    except (KitError, OSError) as exc:
        fatal("io", str(exc))
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        fatal("encoding", "not valid UTF-8")
        return None
    try:
        fm, body = parse_frontmatter(text)
    except KitError as exc:
        fatal("frontmatter", str(exc))
        return None
    msgs = validate_frontmatter(fm)
    if msgs:
        for m in msgs:
            fatal("frontmatter-field", m)
        return None
    if fm["kind"] != kind:
        fatal("kind-mismatch", "kind %r does not match its location (%s)" % (fm["kind"], kind))
        return None
    expected = _expected_doc_id(kind, path)
    if expected is None:
        fatal("filename", "file or directory name is not valid for kind %r" % kind)
        return None
    if fm["id"] != expected:
        fatal("id-mismatch", "id %r does not match its filename (expected %r)" % (fm["id"], expected))
        return None
    anchors, aprobs = parse_anchor_block(body)
    if aprobs:
        for m in aprobs:
            fatal("anchor-block", m)
        return None
    stripped = strip_comments(body)
    return {
        "id": fm["id"], "kind": kind, "title": fm["title"], "status": fm["status"],
        "confidence": fm.get("confidence"), "area": fm.get("area", ""), "path": rel,
        "updated": fm["updated"], "body": stripped.strip("\n") + "\n", "fm": fm,
        "owned_anchors": anchors, "claims": CLAIM_LINE_RE.findall(stripped),
        "has_initial_state": "<!-- study:initial-state " in body,
    }


def _split_jsonl(raw: bytes) -> List[Tuple[int, bytes]]:
    out = []
    for n, line in enumerate(raw.split(b"\n"), start=1):
        if line.strip():
            out.append((n, line))
    return out


def scan_event_meta(ctx: "Context", path: Path) -> Tuple[Optional[Dict[str, Any]], int]:
    """Stream a cold events log for metadata only (identity, time range, actions, refs)."""
    real = ctx.fs.check_read(path)
    if not real.is_file():
        return None, 0
    meta: Dict[str, Any] = {"first": None, "last": None, "count": 0, "actions": set(), "refs": set()}
    malformed = 0
    with open(str(real), "rb") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                malformed += 1
                continue
            if not isinstance(event, dict):
                malformed += 1
                continue
            meta["count"] += 1
            ts = event.get("timestamp")
            if isinstance(ts, str):
                meta["first"] = ts if meta["first"] is None or ts < meta["first"] else meta["first"]
                meta["last"] = ts if meta["last"] is None or ts > meta["last"] else meta["last"]
            if isinstance(event.get("action"), str):
                meta["actions"].add(event["action"])
            refs = event.get("refs")
            if isinstance(refs, list) and len(meta["refs"]) < 500:
                meta["refs"].update(r for r in refs if isinstance(r, str))
    return meta, malformed


def load_records(ctx: "Context", evaluate: bool = True) -> Records:
    rec = Records()
    seen_docs: Dict[str, str] = {}
    seen_anchors: Dict[str, str] = {}
    seen_evidence: Dict[str, str] = {}

    def add_doc(doc: Dict[str, Any]) -> bool:
        if doc["id"] in seen_docs:
            rec.problems.append(Problem(
                "error", "duplicate-id",
                "duplicate document ID %s (also defined in %s)" % (doc["id"], seen_docs[doc["id"]]),
                doc["path"], True))
            return False
        seen_docs[doc["id"]] = doc["path"]
        rec.docs.append(doc)
        listed = list(doc["fm"].get("anchors", []))
        owned_ids = []
        for a in doc["owned_anchors"]:
            if a["id"] in seen_anchors:
                rec.problems.append(Problem(
                    "error", "duplicate-id",
                    "duplicate anchor ID %s (also defined in %s)" % (a["id"], seen_anchors[a["id"]]),
                    doc["path"], True))
                continue
            seen_anchors[a["id"]] = doc["path"]
            a = dict(a)
            a["doc_id"] = doc["id"]
            a["state"] = "unchecked"
            rec.anchors.append(a)
            owned_ids.append(a["id"])
            rec.links.append((doc["id"], a["id"], "anchor"))
            if a["id"] not in listed:
                rec.problems.append(Problem(
                    "error", "anchor-unlisted",
                    "anchor %s is recorded in the document but missing from its frontmatter anchors list"
                    % a["id"], doc["path"]))
        for dst in doc["fm"].get("links", []):
            rec.links.append((doc["id"], dst, "links"))
        for dst in listed:
            if dst not in owned_ids:
                rec.links.append((doc["id"], dst, "anchor-ref"))
        for claim in doc["claims"]:
            rec.claims.append((claim, doc["id"]))
            rec.links.append((doc["id"], claim, "claim"))
        return True

    for kind, dirname in KIND_DIRS.items():
        for path in list_dir_sorted(ctx.study / dirname):
            if path.name.startswith(".tmp-"):
                continue
            if path.suffix != ".md" or path.is_dir():
                rec.problems.append(Problem("warning", "unexpected-file", "unexpected entry in %s/" % dirname,
                                            path.relative_to(ctx.study).as_posix()))
                continue
            doc = _parse_doc(ctx, path, kind, rec)
            if doc:
                add_doc(doc)

    for run_dir in list_dir_sorted(ctx.study / "runs"):
        rel_dir = run_dir.relative_to(ctx.study).as_posix()
        if not run_dir.is_dir() or not RUN_ID_RE.match(run_dir.name):
            if not run_dir.name.startswith("."):
                rec.problems.append(Problem("warning", "unexpected-file", "unexpected entry in runs/", rel_dir))
            continue
        summary = run_dir / "summary.md"
        if not summary.is_file():
            rec.problems.append(Problem("error", "run-summary-missing", "run directory has no summary.md",
                                        rel_dir, True))
            continue
        doc = _parse_doc(ctx, summary, "run", rec)
        if doc and add_doc(doc):
            fm = doc["fm"]
            rec.runs.append({
                "id": doc["id"], "status": fm["status"], "goal": fm.get("goal"), "agent": fm.get("agent"),
                "started": fm.get("started"), "ended": fm.get("ended") or None,
                "summary_path": doc["path"], "has_initial_state": doc["has_initial_state"],
            })
        _load_run_logs(ctx, run_dir, doc["id"] if doc else run_dir.name, rec, seen_evidence)

    if evaluate and rec.anchors:
        dirty: Set[str] = set()
        if ctx.git.available():
            try:
                dirty = ctx.git.dirty_paths()
            except KitError as exc:
                rec.problems.append(Problem("warning", "git-status", str(exc)))
        for a in rec.anchors:
            a["state"] = evaluate_anchor(ctx, a, dirty)
    return rec


def _load_run_logs(ctx: "Context", run_dir: Path, run_id: str, rec: Records,
                   seen_evidence: Dict[str, str]) -> None:
    ev_path = run_dir / "evidence.jsonl"
    ev_rel = ev_path.relative_to(ctx.study).as_posix()
    if not ev_path.is_file():
        rec.problems.append(Problem("error", "evidence-file-missing", "evidence.jsonl is missing", ev_rel))
    else:
        try:
            raw = ctx.fs.read_bytes(ev_path)
        except (KitError, OSError) as exc:
            rec.problems.append(Problem("error", "io", str(exc), ev_rel))
            raw = b""
        for lineno, line in _split_jsonl(raw):
            where = "%s:%d" % (ev_rel, lineno)
            try:
                obj = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                rec.problems.append(Problem("error", "jsonl-malformed", "line is not valid JSON", where))
                continue
            msgs = validate_evidence_record(obj)
            if msgs:
                for m in msgs:
                    rec.problems.append(Problem("error", "evidence-invalid", m, where))
                continue
            if obj["id"] in seen_evidence:
                rec.problems.append(Problem(
                    "error", "duplicate-evidence-id",
                    "duplicate evidence ID %s (also at %s)" % (obj["id"], seen_evidence[obj["id"]]), where, True))
                continue
            seen_evidence[obj["id"]] = where
            rec.evidence.append({
                "id": obj["id"], "run_id": run_id, "subject": obj["subject"], "type": obj["type"],
                "timestamp": obj["timestamp"], "producer": obj["producer"],
                "repository_commit": obj["repository_commit"], "result": obj["result"],
                "raw_json": line.decode("utf-8").strip(), "anchors": list(obj["anchors"]),
            })
    events_path = run_dir / "events.jsonl"
    meta, malformed = scan_event_meta(ctx, events_path)
    ev_rel = events_path.relative_to(ctx.study).as_posix()
    if meta is None:
        rec.problems.append(Problem("warning", "events-file-missing", "events.jsonl is missing", ev_rel))
        return
    if malformed:
        rec.problems.append(Problem("warning", "events-malformed",
                                    "%d malformed line(s) in cold event log" % malformed, ev_rel))
    rec.run_meta.append({
        "run_id": run_id, "first_ts": meta["first"], "last_ts": meta["last"], "event_count": meta["count"],
        "actions": ",".join(sorted(meta["actions"])), "refs": ",".join(sorted(meta["refs"])),
    })


def analyze(ctx: "Context", rec: Records, evaluate: bool = True) -> List[Problem]:
    out: List[Problem] = []
    known: Set[str] = {d["id"] for d in rec.docs}
    known |= {a["id"] for a in rec.anchors}
    known |= {c for c, _ in rec.claims}
    known |= {e["id"] for e in rec.evidence}
    doc_path_by_id = {d["id"]: d["path"] for d in rec.docs}
    for src, dst, relation in rec.links:
        if relation in ("links", "anchor-ref") and dst not in known:
            out.append(Problem("error", "link-unknown", "%s links to unknown ID %s" % (src, dst),
                               doc_path_by_id.get(src)))
    counts: Dict[str, List[str]] = {}
    for claim, doc_id in rec.claims:
        counts.setdefault(claim, []).append(doc_id)
    for claim, owners in sorted(counts.items()):
        if len(owners) > 1:
            out.append(Problem("error", "claim-duplicate",
                               "claim %s appears %d times (%s)" % (claim, len(owners), ", ".join(owners))))
    if evaluate:
        quiet = {d["id"] for d in rec.docs if d["status"] in QUIET_STATUSES}
        for a in rec.anchors:
            if a["doc_id"] in quiet:
                continue
            where = doc_path_by_id.get(a["doc_id"])
            if a["state"] == "unchecked":
                out.append(Problem("error", "anchor-unchecked",
                                   "anchor %s path is unsafe or unreadable: %s" % (a["id"], a["path"]), where))
            elif a["state"] != "ok":
                out.append(Problem("warning", "anchor-" + a["state"].replace("_", "-"),
                                   "anchor %s is %s (%s)" % (a["id"], a["state"], a["path"]), where))
        state_by_anchor = {a["id"]: a["state"] for a in rec.anchors}
        for d in rec.docs:
            if d["kind"] != "finding" or d["status"] not in ("open", "triaged"):
                continue
            drifted = [(i, state_by_anchor[i]) for i in d["fm"].get("anchors", [])
                       if state_by_anchor.get(i) in ("stale", "missing_file", "missing_symbol")]
            if drifted:
                out.append(Problem("warning", "finding-needs-recheck",
                                   "finding %s may be out of date (%s); re-inspect it, then close it with "
                                   "set --status resolved|obsolete|dismissed, or leave it open"
                                   % (d["id"], ", ".join("%s is %s" % x for x in drifted)), d["path"]))
        registered = set(ctx.codebases) if ctx.codebases else {"."}
        for a in rec.anchors:
            if a["doc_id"] in quiet:
                continue
            if a["repository"] not in registered:
                out.append(Problem("warning", "anchor-codebase-unregistered",
                                   "anchor %s belongs to codebase %s, which is not registered (%s codebase add %s)"
                                   % (a["id"], a["repository"], ctx.kernel_cmd(), a["repository"]),
                                   doc_path_by_id.get(a["doc_id"])))
        for cb in ctx.codebases:
            if not ctx.git.available_for(cb):
                out.append(Problem("warning", "codebase-unavailable",
                                   "registered codebase %s is missing or not a Git work tree; its freshness "
                                   "and source-change guard are unavailable" % cb))
        if rec.anchors and not ctx.git.available():
            out.append(Problem("warning", "freshness-limited",
                               "Git is unavailable; anchor freshness is limited to fingerprint comparison"))
    anchor_ids = {a["id"] for a in rec.anchors}
    for e in rec.evidence:
        if e["subject"] not in known:
            out.append(Problem("warning", "evidence-subject-unknown",
                               "evidence %s refers to unknown subject %s" % (e["id"], e["subject"])))
        for anc in e["anchors"]:
            if anc not in anchor_ids:
                out.append(Problem("warning", "evidence-anchor-unknown",
                                   "evidence %s refers to unknown anchor %s" % (e["id"], anc)))
    for r in rec.runs:
        if r["status"] == "open":
            out.append(Problem("warning", "run-open", "run %s is still open" % r["id"], r["summary_path"]))
            if not r["has_initial_state"] or not r["goal"] or not r["agent"] or not r["started"]:
                out.append(Problem("error", "run-metadata",
                                   "open run %s lacks start metadata (goal, agent, started, initial state)"
                                   % r["id"], r["summary_path"]))
    return out


def gather(ctx: "Context", evaluate: bool = True) -> Tuple[Records, List[Problem]]:
    rec = load_records(ctx, evaluate=evaluate)
    problems = sorted(rec.problems + analyze(ctx, rec, evaluate), key=problem_sort_key)
    return rec, problems


# =============================================================================
# 8. SQLite index (disposable; rebuilt from durable records)
# =============================================================================

TABLE_DDL = (
    "CREATE TABLE docs(id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT, status TEXT, confidence TEXT,"
    " area TEXT, path TEXT NOT NULL, updated TEXT, body TEXT NOT NULL)",
    "CREATE TABLE anchors(id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, repository_commit TEXT, path TEXT NOT NULL,"
    " symbol TEXT, start_line INTEGER, end_line INTEGER, fingerprint TEXT, state TEXT NOT NULL)",
    "CREATE TABLE links(src TEXT NOT NULL, dst TEXT NOT NULL, relation TEXT NOT NULL, UNIQUE(src, dst, relation))",
    "CREATE TABLE runs(id TEXT PRIMARY KEY, status TEXT NOT NULL, goal TEXT, agent TEXT, started TEXT, ended TEXT,"
    " summary_path TEXT NOT NULL)",
    "CREATE TABLE evidence(id TEXT PRIMARY KEY, run_id TEXT NOT NULL, subject TEXT, type TEXT NOT NULL,"
    " timestamp TEXT NOT NULL, producer TEXT, repository_commit TEXT, result TEXT, raw_json TEXT NOT NULL)",
    "CREATE TABLE run_event_meta(run_id TEXT PRIMARY KEY, first_ts TEXT, last_ts TEXT,"
    " event_count INTEGER NOT NULL, actions TEXT, refs TEXT)",
    "CREATE INDEX links_dst ON links(dst)",
    "CREATE INDEX evidence_subject ON evidence(subject)",
)
DROP_TABLES = ("docs_fts", "docs", "anchors", "links", "runs", "evidence", "run_event_meta")
FTS_DDL = "CREATE VIRTUAL TABLE docs_fts USING fts5(id, title, body)"


def fts5_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.study_fts_probe USING fts5(x)")
        conn.execute("DROP TABLE temp.study_fts_probe")
        return True
    except sqlite3.Error:
        return False


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone()
    return row is not None


def _like_escape(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Index:
    def __init__(self, ctx: "Context") -> None:
        self.ctx = ctx
        self.db_path = ctx.study / "study.db"

    def _connect(self) -> sqlite3.Connection:
        self.ctx.fs.check_write(self.db_path)
        conn = sqlite3.connect(str(self.db_path), timeout=10, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        return conn

    def exists(self) -> bool:
        return self.db_path.is_file()

    @contextlib.contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        if not self.exists():
            raise KitError("index database not found: %s (run: kernel.py rebuild)" % self.ctx.display(self.db_path))
        try:
            conn = self._connect()
        except sqlite3.Error as exc:
            raise KitError("database error: %s (delete the database and run rebuild)" % exc)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        except sqlite3.Error as exc:
            raise KitError("database error: %s (run rebuild)" % exc)
        finally:
            conn.close()

    # ---- build ----

    @staticmethod
    def _create_schema(conn: sqlite3.Connection, use_fts: bool) -> None:
        for name in DROP_TABLES:
            try:
                conn.execute("DROP TABLE IF EXISTS %s" % name)
            except sqlite3.OperationalError:
                if name != "docs_fts":
                    raise
        for ddl in TABLE_DDL:
            conn.execute(ddl)
        if use_fts:
            conn.execute(FTS_DDL)

    @staticmethod
    def _insert(conn: sqlite3.Connection, rec: Records, use_fts: bool) -> None:
        docs = [(d["id"], d["kind"], d["title"], d["status"], d["confidence"], d["area"], d["path"],
                 d["updated"], d["body"]) for d in rec.docs]
        conn.executemany("INSERT INTO docs VALUES (?,?,?,?,?,?,?,?,?)", docs)
        if use_fts:
            conn.executemany("INSERT INTO docs_fts(id, title, body) VALUES (?,?,?)",
                             [(d["id"], d["title"], d["body"]) for d in rec.docs])
        conn.executemany(
            "INSERT INTO anchors VALUES (?,?,?,?,?,?,?,?,?)",
            [(a["id"], a["doc_id"], a["commit"], a["path"], a["symbol"], a["start_line"], a["end_line"],
              a["fingerprint"], a["state"]) for a in rec.anchors])
        conn.executemany("INSERT OR IGNORE INTO links VALUES (?,?,?)", rec.links)
        conn.executemany(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
            [(r["id"], r["status"], r["goal"], r["agent"], r["started"], r["ended"], r["summary_path"])
             for r in rec.runs])
        conn.executemany(
            "INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?,?)",
            [(e["id"], e["run_id"], e["subject"], e["type"], e["timestamp"], e["producer"],
              e["repository_commit"], e["result"], e["raw_json"]) for e in rec.evidence])
        conn.executemany(
            "INSERT INTO run_event_meta VALUES (?,?,?,?,?,?)",
            [(m["run_id"], m["first_ts"], m["last_ts"], m["event_count"], m["actions"], m["refs"])
             for m in rec.run_meta])

    def _populate(self, conn: sqlite3.Connection, rec: Records) -> str:
        use_fts = (not self.ctx.disable_fts) and fts5_available(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._create_schema(conn, use_fts)
            self._insert(conn, rec, use_fts)
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return "fts5" if use_fts else "like"

    def rebuild(self, rec: Records) -> Dict[str, Any]:
        """One transaction; a failure leaves the previous database untouched."""
        try:
            conn = self._connect()
        except sqlite3.Error as exc:
            raise KitError("database error: %s (delete the database and run rebuild)" % exc)
        try:
            backend = self._populate(conn, rec)
        except sqlite3.Error as exc:
            raise KitError("database error during rebuild: %s (previous index kept)" % exc)
        finally:
            conn.close()
        return {"documents": len(rec.docs), "anchors": len(rec.anchors), "links": len(rec.links),
                "runs": len(rec.runs), "evidence": len(rec.evidence), "backend": backend}

    def trial_build(self, rec: Records) -> str:
        """Prove rebuildability in memory without touching the real database."""
        conn = sqlite3.connect(":memory:", isolation_level=None)
        try:
            return self._populate(conn, rec)
        finally:
            conn.close()

    # ---- queries ----

    def backend(self) -> str:
        if not self.exists():
            return "unavailable"
        with self.read() as conn:
            has_fts = _has_table(conn, "docs_fts")
        return "fts5" if (has_fts and not self.ctx.disable_fts) else "like"

    def search(self, terms: Sequence[str], limit: int) -> List[Dict[str, Any]]:
        cols = "d.id, d.kind, d.title, d.status, d.confidence, d.path"
        with self.read() as conn:
            if _has_table(conn, "docs_fts") and not self.ctx.disable_fts:
                query = " ".join('"%s"' % t.replace('"', '""') for t in terms)
                try:
                    rows = conn.execute(
                        "SELECT " + cols + " FROM docs_fts JOIN docs d ON d.id = docs_fts.id"
                        " WHERE docs_fts MATCH ? ORDER BY docs_fts.rank, d.id LIMIT ?", (query, limit)).fetchall()
                    return [dict(r) for r in rows]
                except sqlite3.OperationalError:
                    pass  # fall through to LIKE
            clause = "(d.id LIKE ? ESCAPE '\\' OR d.title LIKE ? ESCAPE '\\' OR d.body LIKE ? ESCAPE '\\')"
            sql = "SELECT " + cols + " FROM docs d WHERE " + " AND ".join([clause] * len(terms)) \
                + " ORDER BY d.id LIMIT ?"
            params: List[Any] = []
            for t in terms:
                pat = "%" + _like_escape(t) + "%"
                params.extend([pat, pat, pat])
            params.append(limit)
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def list_docs(self, kind: Optional[str], status: Optional[str], area: Optional[str],
                  confidence: Optional[str]) -> List[Dict[str, Any]]:
        where, params = [], []  # type: List[str], List[Any]
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if status:
            where.append("status = ?")
            params.append(status)
        if confidence:
            where.append("confidence = ?")
            params.append(confidence)
        if area:
            where.append("(area = ? OR area LIKE ? ESCAPE '\\')")
            params.extend([area, _like_escape(area) + "/%"])
        sql = "SELECT id, kind, title, status, confidence, area, updated, path FROM docs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id"
        with self.read() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def get_row(self, table: str, row_id: str) -> Optional[Dict[str, Any]]:
        if table not in ("docs", "anchors", "evidence", "runs"):
            raise KitError("internal error: bad table")
        with self.read() as conn:
            row = conn.execute("SELECT * FROM %s WHERE id = ?" % table, (row_id,)).fetchone()
        return dict(row) if row else None

    def anchors_of(self, doc_id: str) -> List[Dict[str, Any]]:
        with self.read() as conn:
            rows = conn.execute("SELECT * FROM anchors WHERE doc_id = ? ORDER BY id", (doc_id,)).fetchall()
        return [dict(r) for r in rows]

    def links_out(self, node: str) -> List[Dict[str, str]]:
        with self.read() as conn:
            rows = conn.execute("SELECT dst, relation FROM links WHERE src = ? ORDER BY relation, dst",
                                (node,)).fetchall()
        return [dict(r) for r in rows]

    def backlinks(self, node: str) -> List[Dict[str, str]]:
        with self.read() as conn:
            rows = conn.execute("SELECT src, relation FROM links WHERE dst = ? ORDER BY relation, src",
                                (node,)).fetchall()
        return [dict(r) for r in rows]

    def claim_owner(self, claim_id: str) -> Optional[str]:
        with self.read() as conn:
            row = conn.execute("SELECT src FROM links WHERE dst = ? AND relation = 'claim' ORDER BY src",
                               (claim_id,)).fetchone()
        return row["src"] if row else None

    def evidence_for(self, subjects: Sequence[str]) -> List[Dict[str, Any]]:
        if not subjects:
            return []
        marks = ",".join("?" for _ in subjects)
        sql = ("SELECT id, run_id, subject, type, timestamp, producer, repository_commit, result"
               " FROM evidence WHERE subject IN (" + marks + ") ORDER BY id")
        with self.read() as conn:
            return [dict(r) for r in conn.execute(sql, list(subjects)).fetchall()]

    def evidence_for_run(self, run_id: str) -> List[Dict[str, Any]]:
        sql = ("SELECT id, run_id, subject, type, timestamp, producer, repository_commit, result"
               " FROM evidence WHERE run_id = ? ORDER BY id")
        with self.read() as conn:
            return [dict(r) for r in conn.execute(sql, (run_id,)).fetchall()]

    def graph(self, start: str, depth: int) -> Dict[str, Any]:
        with self.read() as conn:
            known = (
                conn.execute("SELECT 1 FROM docs WHERE id = ?", (start,)).fetchone()
                or conn.execute("SELECT 1 FROM anchors WHERE id = ?", (start,)).fetchone()
                or conn.execute("SELECT 1 FROM links WHERE src = ? OR dst = ?", (start, start)).fetchone()
                or conn.execute("SELECT 1 FROM evidence WHERE id = ?", (start,)).fetchone()
            )
            if not known:
                raise KitError("unknown ID: %s" % start)
            depths: Dict[str, int] = {start: 0}
            edges: Set[Tuple[str, str, str]] = set()
            frontier = [start]
            for level in range(1, depth + 1):
                nxt: List[str] = []
                for node in sorted(frontier):
                    rows = conn.execute("SELECT src, dst, relation FROM links WHERE src = ? OR dst = ?",
                                        (node, node)).fetchall()
                    for row in rows:
                        edges.add((row["src"], row["dst"], row["relation"]))
                        other = row["dst"] if row["src"] == node else row["src"]
                        if other not in depths:
                            depths[other] = level
                            nxt.append(other)
                frontier = nxt
                if not frontier:
                    break
            info: Dict[str, Dict[str, Any]] = {}
            for node in depths:
                row = conn.execute("SELECT kind, title FROM docs WHERE id = ?", (node,)).fetchone()
                info[node] = {"kind": row["kind"], "title": row["title"]} if row else {"kind": None, "title": None}
        nodes = [{"id": n, "depth": depths[n], "kind": info[n]["kind"], "title": info[n]["title"]}
                 for n in sorted(depths, key=lambda x: (depths[x], x))]
        edge_list = [{"src": s, "dst": d, "relation": r} for s, d, r in sorted(edges)]
        return {"start": start, "depth": depth, "nodes": nodes, "edges": edge_list}


# =============================================================================
# 9. Context and shared command helpers
# =============================================================================


@dataclasses.dataclass
class Context:
    root: Path
    study: Path
    agent: str
    json_mode: bool
    max_event_bytes: int
    disable_fts: bool
    fs: StudyFS
    git: Any  # GitInspector, or MultiGit when codebases are registered
    out: Any
    err: Any
    codebases: List[str] = dataclasses.field(default_factory=list)

    def say(self, text: str = "") -> None:
        print(text, file=self.out)

    def warn(self, text: str) -> None:
        print("warning: %s" % text, file=self.err)

    def emit_json(self, obj: Any) -> None:
        self.out.write(json.dumps(obj, indent=2, sort_keys=True) + "\n")

    def display(self, path: Any) -> str:
        try:
            return Path(path).relative_to(self.root).as_posix()
        except ValueError:
            return str(path)

    def kernel_cmd(self) -> str:
        return "python %s/kernel.py" % self.display(self.study)


def default_root() -> Path:
    here = Path(__file__).resolve().parent
    return here.parent if here.name == ".study" else Path.cwd()


def load_codebases(fs: StudyFS) -> List[str]:
    """Registered codebase paths (root-relative POSIX, '.' for the root itself); [] if none."""
    path = fs.study / CODEBASES_FILE
    if not path.is_file():
        return []
    try:
        obj = json.loads(fs.read_bytes(path).decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise KitError("cannot read %s: %s" % (CODEBASES_FILE, exc))
    if not isinstance(obj, dict) or obj.get("schema") != CODEBASES_SCHEMA:
        raise KitError("%s: unexpected or missing schema (expected %s)" % (CODEBASES_FILE, CODEBASES_SCHEMA))
    items = obj.get("codebases")
    if not isinstance(items, list):
        raise KitError("%s: 'codebases' must be a list" % CODEBASES_FILE)
    out: List[str] = []
    for item in items:
        raw = item.get("path") if isinstance(item, dict) else None
        if not isinstance(raw, str):
            raise KitError("%s: every entry needs a string 'path'" % CODEBASES_FILE)
        rel = "." if raw == "." else normalize_repo_relpath(raw, "codebase path")
        if rel != "." and rel.split("/")[0] in (".git", ".study"):
            raise KitError("%s: invalid codebase path %r" % (CODEBASES_FILE, raw))
        resolve_inside(fs.root, rel)
        if rel in out:
            raise KitError("%s: duplicate codebase %r" % (CODEBASES_FILE, rel))
        out.append(rel)
    return sorted(out)


def build_context(args: argparse.Namespace, out: Any, err: Any, require_study: bool) -> Context:
    raw_root = getattr(args, "root", None)
    root = Path(os.path.realpath(raw_root)) if raw_root else Path(os.path.realpath(str(default_root())))
    if not root.is_dir():
        raise KitError("repository root is not a directory: %s" % root)
    raw_study = getattr(args, "study_dir", None)
    study = Path(os.path.realpath(raw_study)) if raw_study else Path(os.path.realpath(str(root / ".study")))
    fs = StudyFS(root, study)
    agent = getattr(args, "agent", None) or os.environ.get("STUDY_AGENT") or "unknown"
    agent = clean_line(agent, "agent name", 200)
    raw_max = getattr(args, "max_event_bytes", None)
    if raw_max is None:
        env_max = os.environ.get("STUDY_EVENT_MAX_BYTES")
        try:
            raw_max = int(env_max) if env_max else DEFAULT_EVENT_MAX_BYTES
        except ValueError:
            raise KitError("STUDY_EVENT_MAX_BYTES must be an integer")
    if raw_max < 64:
        raise KitError("--max-event-bytes must be at least 64")
    if require_study and not fs.study.is_dir():
        raise KitError("study directory not found: %s (run: python kernel.py init --root %s)" % (fs.study, root))
    study_rel = fs.study.relative_to(fs.root).as_posix()
    codebases = load_codebases(fs) if fs.study.is_dir() else []
    git: Any = MultiGit(fs.root, fs.study, codebases) if codebases else GitInspector(fs.root, study_rel)
    return Context(
        root=fs.root, study=fs.study, agent=agent, json_mode=bool(getattr(args, "json_out", False)),
        max_event_bytes=raw_max, disable_fts=os.environ.get("STUDY_DISABLE_FTS") == "1",
        fs=fs, git=git, out=out, err=err, codebases=codebases,
    )


def do_rebuild(ctx: Context) -> Dict[str, Any]:
    rec = load_records(ctx)
    fatal = rec.fatal_problems()
    if fatal:
        first = fatal[0]
        raise KitError("rebuild aborted, index unchanged: %d structural error(s); first: %s%s (run: check)"
                       % (len(fatal), (first.path + ": ") if first.path else "", first.message))
    return Index(ctx).rebuild(rec)


def refresh_index(ctx: Context) -> None:
    try:
        do_rebuild(ctx)
    except KitError as exc:
        ctx.warn("index not updated: %s" % exc)


def require_open_run(ctx: Context, run_id: str) -> str:
    if not RUN_ID_RE.match(run_id or ""):
        raise KitError("invalid run ID %r (expected RUN-NNNN)" % run_id)
    path = doc_path(ctx, run_id)
    if not path.is_file():
        raise KitError("unknown run: %s" % run_id)
    fm, _ = read_doc_file(ctx, path)
    if fm["status"] != "open":
        raise KitError("run %s is not open (status: %s)" % (run_id, fm["status"]))
    return run_id


def append_event(ctx: Context, run_id: str, action: str, refs: Sequence[str] = (),
                 detail: Optional[Dict[str, Any]] = None) -> None:
    record = {
        "schema": EVENT_SCHEMA, "timestamp": utc_now(), "run": run_id, "action": action,
        "agent": ctx.agent, "refs": sorted(set(refs)),
        "detail": truncate_detail(detail or {}, ctx.max_event_bytes),
    }
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    ctx.fs.append_bytes(ctx.study / "runs" / run_id / "events.jsonl", line.encode("utf-8"))


def max_number_in_files(ctx: Context, paths: Sequence[Path], prefix: str) -> int:
    pattern = re.compile(rb"%s-(\d{4,})" % prefix.encode("ascii"))
    best = 0
    for path in paths:
        try:
            raw = ctx.fs.read_bytes(path)
        except (KitError, OSError):
            continue
        for m in pattern.finditer(raw):
            best = max(best, int(m.group(1)))
    return best


def all_doc_files(ctx: Context) -> List[Path]:
    files: List[Path] = []
    for dirname in KIND_DIRS.values():
        files.extend(p for p in list_dir_sorted(ctx.study / dirname) if p.suffix == ".md")
    for run_dir in list_dir_sorted(ctx.study / "runs"):
        files.append(run_dir / "summary.md")
    return files


def all_evidence_files(ctx: Context) -> List[Path]:
    return [d / "evidence.jsonl" for d in list_dir_sorted(ctx.study / "runs")]


def known_ids(ctx: Context) -> Set[str]:
    rec = load_records(ctx, evaluate=False)
    ids = {d["id"] for d in rec.docs} | {a["id"] for a in rec.anchors}
    ids |= {c for c, _ in rec.claims}
    ids |= {e["id"] for e in rec.evidence}
    return ids


def validate_anchor_refs(ctx: Context, refs: Optional[Sequence[str]]) -> List[str]:
    out: List[str] = []
    for ref in refs or []:
        if not ANC_ID_RE.match(ref):
            raise KitError("invalid anchor ID %r (expected ANC-NNNN)" % ref)
        if ref not in out:
            out.append(ref)
    if out:
        rec = load_records(ctx, evaluate=False)
        existing = {a["id"] for a in rec.anchors}
        for ref in out:
            if ref not in existing:
                raise KitError("unknown anchor: %s" % ref)
    return out


def format_state_lines(state: Dict[str, Any], limit: int = 20) -> str:
    if "codebases" in state:
        subs = state["codebases"]
        lines = ["- Codebases: %d" % len(subs)]
        for name in sorted(subs):
            sub = subs[name]
            if not sub.get("git"):
                lines.append("  - `%s`: not a Git work tree (source-change guard unavailable)" % name)
                continue
            entries = sub.get("porcelain", [])
            lines.append("  - `%s`: HEAD %s, working-tree entries: %d" % (name, sub.get("head") or "none", len(entries)))
            for entry in entries[:limit]:
                lines.append("    - `%s`" % entry.replace("`", "'"))
            if len(entries) > limit:
                lines.append("    - ... and %d more" % (len(entries) - limit))
        return "\n".join(lines)
    lines = ["- Git repository: %s" % ("yes" if state.get("git") else "no (source-change guard unavailable)"),
             "- HEAD: %s" % (state.get("head") or "none"),
             "- Working-tree entries (porcelain, study directory excluded): %d" % len(state.get("porcelain", []))]
    entries = state.get("porcelain", [])
    for entry in entries[:limit]:
        lines.append("  - `%s`" % entry.replace("`", "'"))
    if len(entries) > limit:
        lines.append("  - ... and %d more" % (len(entries) - limit))
    return "\n".join(lines)


def parse_state_marker(body: str, name: str) -> Optional[Dict[str, Any]]:
    m = re.search(r"<!-- study:" + name + r" (\{[^\n]*\}) -->", body)
    if not m:
        return None
    try:
        value = json.loads(m.group(1))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


# =============================================================================
# 10. Commands
# =============================================================================


def cmd_init(ctx: Context, args: argparse.Namespace) -> int:
    kit = Path(os.path.realpath(str(Path(__file__).resolve().parent)))
    # An installed .study/ keeps the support files next to kernel.py; the emkit package keeps them
    # in a resources/ directory beside it.
    res = kit / "resources"
    base = res if (res / "PROTOCOL.md").is_file() else kit
    plan: List[Tuple[str, Path]] = [(name, (kit if name == "kernel.py" else base) / name) for name in KIT_FILES]
    plan += [("templates/" + name, base / "templates" / name) for name in TEMPLATE_NAMES]
    for rel, src in plan:
        if not src.is_file():
            raise KitError("kit file missing: %s" % src)
    in_place = kit == ctx.study
    if not in_place and not args.force:
        conflicts = [rel for rel, _ in plan if os.path.lexists(str(ctx.study / rel))]
        if conflicts:
            raise KitError("refusing to overwrite existing files: %s (use --force to overwrite)"
                           % ", ".join(conflicts))
    for sub in ("", "templates", "systems", "flows", "findings", "runs", "scratch"):
        ctx.fs.mkdirs(ctx.study / sub if sub else ctx.study)
    if not in_place:
        for rel, src in plan:
            ctx.fs.atomic_write(ctx.study / rel, src.read_bytes())
    excluded = ensure_git_excludes(ctx)
    stats = do_rebuild(ctx)
    head = ctx.git.head()
    git_note = head if head else ("none (no commits yet)" if ctx.git.available() else "none (not a Git repository)")
    payload = {"root": str(ctx.root), "study_dir": str(ctx.study), "git_head": head,
               "search_backend": stats["backend"], "git_excludes_added": excluded,
               "next": "%s status" % ctx.kernel_cmd()}
    if ctx.json_mode:
        ctx.emit_json(payload)
        return 0
    ctx.say("root: %s" % ctx.root)
    ctx.say("study directory: %s" % ctx.study)
    ctx.say("git head: %s" % git_note)
    ctx.say("search backend: %s" % stats["backend"])
    ctx.say("next: %s status" % ctx.kernel_cmd())
    return 0


def ensure_git_excludes(ctx: Context) -> List[str]:
    """Append only the four disposable-state patterns to .git/info/exclude.

    This is the sole write the kernel makes outside the study directory.
    """
    path = ctx.git.exclude_file()
    if path is None:
        return []
    if path.name != "exclude" or path.parent.name != "info":
        raise KitError("unexpected Git exclude path: %s" % path)
    try:
        existing = path.read_bytes() if path.exists() else b""
    except OSError as exc:
        raise KitError("cannot read %s: %s" % (path, exc))
    present = {line.strip() for line in existing.decode("utf-8", errors="replace").splitlines()}
    missing = [e for e in GIT_EXCLUDE_ENTRIES if e not in present]
    if not missing:
        return []
    chunk = b""
    if existing and not existing.endswith(b"\n"):
        chunk += b"\n"
    chunk += ("\n".join(missing) + "\n").encode("utf-8")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(path), "ab") as fh:
            fh.write(chunk)
    except OSError as exc:
        raise KitError("cannot update %s: %s" % (path, exc))
    return missing


def cmd_rebuild(ctx: Context, args: argparse.Namespace) -> int:
    stats = do_rebuild(ctx)
    if ctx.json_mode:
        ctx.emit_json(stats)
    else:
        ctx.say("rebuilt index: %d documents, %d anchors, %d links, %d runs, %d evidence records"
                % (stats["documents"], stats["anchors"], stats["links"], stats["runs"], stats["evidence"]))
        ctx.say("search backend: %s" % stats["backend"])
    return 0


def cmd_run_start(ctx: Context, args: argparse.Namespace) -> int:
    goal = clean_line(args.goal, "goal", 1000)
    template = load_template(ctx, "run.md")
    snapshot = ctx.git.snapshot()
    runs_dir = ctx.study / "runs"
    ctx.fs.mkdirs(runs_dir)
    number = next_number([p.name for p in list_dir_sorted(runs_dir)], "RUN")
    run_dir = None
    run_id = ""
    for _ in range(1000):
        run_id = "RUN-%04d" % number
        try:
            ctx.fs.mkdir_exclusive(runs_dir / run_id)
            run_dir = runs_dir / run_id
            break
        except FileExistsError:
            number += 1
    if run_dir is None:
        raise KitError("could not allocate a run ID")
    started = utc_now()
    title = goal if len(goal) <= 120 else goal[:117] + "..."
    text = render_template(template, {
        "id": run_id, "title": title, "goal": goal, "agent": ctx.agent, "started": started,
        "updated": started, "updated_by": ctx.agent,
        "initial_state_text": format_state_lines(snapshot),
        "initial_state_json": comment_safe_json(snapshot),
    })
    check_rendered(text, "run", run_id)
    ctx.fs.create_exclusive(run_dir / "summary.md", encode_text(text))
    ctx.fs.create_exclusive(run_dir / "evidence.jsonl", b"")
    ctx.fs.create_exclusive(run_dir / "events.jsonl", b"")
    append_event(ctx, run_id, "run_start", [run_id], {
        "goal": goal, "git": snapshot["git"], "head": snapshot["head"],
        "porcelain_entries": len(snapshot["porcelain"])})
    if "codebases" in snapshot:
        for name, sub in sorted(snapshot["codebases"].items()):
            if not sub["git"]:
                ctx.warn("codebase %s is not a Git work tree; the source-change guard is unavailable for it in %s"
                         % (name, run_id))
    elif not snapshot["git"]:
        ctx.warn("not a Git repository; the source-change guard is unavailable for %s" % run_id)
    refresh_index(ctx)
    if ctx.json_mode:
        ctx.emit_json({"id": run_id, "status": "open", "head": snapshot["head"], "git": snapshot["git"]})
    else:
        ctx.say(run_id)
    return 0


def collect_run_artifacts(ctx: Context, run_id: str) -> List[str]:
    refs: Set[str] = set()
    run_dir = ctx.study / "runs" / run_id
    events = run_dir / "events.jsonl"
    if ctx.fs.check_read(events).is_file():
        with open(str(ctx.fs.check_read(events)), "rb") as fh:
            for line in fh:
                try:
                    ev = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    continue
                if isinstance(ev, dict) and ev.get("action") in ("new_artifact", "finding_create", "anchor_add"):
                    refs.update(r for r in ev.get("refs", []) if isinstance(r, str))
    evidence = run_dir / "evidence.jsonl"
    if ctx.fs.check_read(evidence).is_file():
        refs.update(m.decode("ascii") for m in re.findall(rb'"id":\s*"(EV-\d{4,})"', ctx.fs.read_bytes(evidence)))
    return sorted(refs)


def cmd_run_end(ctx: Context, args: argparse.Namespace) -> int:
    run_id = args.id
    if not RUN_ID_RE.match(run_id or ""):
        raise KitError("invalid run ID %r (expected RUN-NNNN)" % run_id)
    summary_text = (args.summary or "").strip()
    if not summary_text:
        raise KitError("summary must not be empty")
    path = doc_path(ctx, run_id)
    if not path.is_file():
        raise KitError("unknown run: %s" % run_id)
    fm, body = read_doc_file(ctx, path)
    if fm["status"] != "open":
        raise KitError("run %s is not open (status: %s)" % (run_id, fm["status"]))
    initial = parse_state_marker(body, "initial-state")
    if initial is None:
        raise KitError("run %s has no initial source-state snapshot" % run_id)
    covered: List[str] = []
    for cid in args.covered or []:
        if cid not in covered:
            doc_path(ctx, cid)  # raises for non-document IDs
            if not doc_path(ctx, cid).is_file():
                raise KitError("unknown covered document: %s" % cid)
            covered.append(cid)
    questions = [clean_line(q, "open question", 1000) for q in (args.open_question or [])]
    next_text = args.next.strip() if args.next else ""

    final = ctx.git.snapshot()
    diff = diff_snapshots(initial, final)
    ended = utc_now()
    status = "source_changed" if diff["changed"] else "ended"

    artifacts = collect_run_artifacts(ctx, run_id)
    body = replace_section(body, "Activity summary", md_safe(summary_text))
    if covered:
        body = replace_section(body, "Scope", "Covered: " + ", ".join(covered))
    body = replace_section(body, "Artifacts created or updated",
                           "\n".join("- %s" % a for a in artifacts) if artifacts else "- none recorded")
    if questions:
        body = replace_section(body, "Open questions", "\n".join("- %s" % md_safe(q) for q in questions))
    if next_text:
        body = replace_section(body, "Next steps", md_safe(next_text))
    final_lines = [format_state_lines(final)]
    if diff["changed"]:
        final_lines.append("")
        final_lines.append("**SOURCE STATE CHANGED DURING THIS RUN. This run did not complete successfully.**")
        final_lines.append("- Reason: %s" % (diff["reason"] or "state differs"))
        if diff["head_before"] != diff["head_after"]:
            final_lines.append("- HEAD before: %s" % (diff["head_before"] or "none"))
            final_lines.append("- HEAD after: %s" % (diff["head_after"] or "none"))
        for entry in diff["added"][:20]:
            final_lines.append("- new or changed entry: `%s`" % entry.replace("`", "'"))
        for entry in diff["removed"][:20]:
            final_lines.append("- entry no longer present: `%s`" % entry.replace("`", "'"))
    final_lines.append("")
    final_lines.append("<!-- study:final-state %s -->" % comment_safe_json(final))
    body = replace_section(body, "Final source state", "\n".join(final_lines))

    fm["status"] = status
    fm["ended"] = ended
    fm["updated"] = ended
    fm["updated_by"] = ctx.agent
    fm["links"] = sorted(set(fm.get("links", [])) | set(covered))
    write_doc_file(ctx, path, fm, body.rstrip("\n") + "\n")
    append_event(ctx, run_id, "run_end", [run_id] + covered, {
        "status": status, "head": final["head"], "added": diff["added"][:50], "removed": diff["removed"][:50]})
    refresh_index(ctx)

    if diff["changed"]:
        if ctx.json_mode:
            ctx.emit_json({"id": run_id, "status": status, "diff": diff})
        ctx.err.write("source state diff for %s (state only, no file contents):\n" % run_id)
        if diff["head_before"] != diff["head_after"]:
            ctx.err.write("  HEAD: %s -> %s\n" % (diff["head_before"], diff["head_after"]))
        for entry in diff["added"]:
            ctx.err.write("  + %s\n" % entry)
        for entry in diff["removed"]:
            ctx.err.write("  - %s\n" % entry)
        raise KitError("source state changed during %s; run marked source_changed" % run_id)

    _rec, problems = gather(ctx)
    shown = [p for p in problems if p.severity in ("error", "warning") and p.code != "run-open"]
    if ctx.json_mode:
        ctx.emit_json({"id": run_id, "status": status, "problems": [p.to_dict() for p in shown]})
        return 0
    ctx.say("ended %s" % run_id)
    if not ctx.git.available() and not ctx.codebases:
        ctx.say("note: not a Git repository; source-change guard was unavailable")
    if shown:
        ctx.say("unresolved problems (%d):" % len(shown))
        for p in shown[:20]:
            ctx.say("  " + p.format())
        if len(shown) > 20:
            ctx.say("  ... and %d more (run: check)" % (len(shown) - 20))
    else:
        ctx.say("no unresolved problems")
    return 0


def cmd_new(ctx: Context, args: argparse.Namespace) -> int:
    kind = args.new_kind
    prefix = KIND_ID_PREFIX[kind]
    dirname = KIND_DIRS[kind]
    slug = validate_slug(args.slug)
    title = clean_line(args.title, "title", 200)
    area = normalize_area(args.area)
    run_id = require_open_run(ctx, args.run)
    template = load_template(ctx, kind + ".md")
    doc_id = prefix + slug
    path = ctx.study / dirname / (slug + ".md")
    text = render_template(template, {"id": doc_id, "title": title, "area": area,
                                      "updated": utc_now(), "updated_by": ctx.agent})
    check_rendered(text, kind, doc_id)
    ctx.fs.mkdirs(ctx.study / dirname)
    try:
        ctx.fs.create_exclusive(path, encode_text(text))
    except FileExistsError:
        raise KitError("%s already exists" % doc_id)
    append_event(ctx, run_id, "new_artifact", [doc_id], {"kind": kind, "path": ctx.display(path)})
    refresh_index(ctx)
    ctx.say("created %s (%s)" % (doc_id, ctx.display(path)))
    return 0


def cmd_finding(ctx: Context, args: argparse.Namespace) -> int:
    title = clean_line(args.title, "title", 200)
    severity = args.severity
    if severity not in SEVERITIES:
        raise KitError("severity must be one of: %s" % ", ".join(SEVERITIES))
    area = normalize_area(args.area)
    run_id = require_open_run(ctx, args.run)
    anchors = validate_anchor_refs(ctx, args.anchor)
    confidence = "observed" if anchors else "hypothesis"
    template = load_template(ctx, "finding.md")
    findings_dir = ctx.study / "findings"
    ctx.fs.mkdirs(findings_dir)
    number = next_number([p.name for p in list_dir_sorted(findings_dir)], "F")
    for _ in range(1000):
        finding_id = "F-%04d" % number
        text = render_template(template, {
            "id": finding_id, "title": title, "severity": severity, "confidence": confidence, "area": area,
            "updated": utc_now(), "updated_by": ctx.agent, "anchors": anchors,
            "anchor_refs": ("Anchors: " + ", ".join(anchors)) if anchors
            else "Anchors: none supplied (confidence is hypothesis until anchored)",
        })
        check_rendered(text, "finding", finding_id)
        path = findings_dir / (finding_id + ".md")
        try:
            ctx.fs.create_exclusive(path, encode_text(text))
        except FileExistsError:
            number += 1
            continue
        append_event(ctx, run_id, "finding_create", [finding_id] + anchors,
                     {"severity": severity, "path": ctx.display(path)})
        refresh_index(ctx)
        ctx.say("created %s (%s)" % (finding_id, ctx.display(path)))
        return 0
    raise KitError("could not allocate a finding ID")


def cmd_anchor_add(ctx: Context, args: argparse.Namespace) -> int:
    run_id = require_open_run(ctx, args.run)
    doc_id = args.document_id
    if RUN_ID_RE.match(doc_id or ""):
        raise KitError("anchors can only be added to system, flow or finding documents")
    path = doc_path(ctx, doc_id)
    if not path.is_file():
        raise KitError("unknown document: %s" % doc_id)
    fm, body = read_doc_file(ctx, path)
    rel = normalize_repo_relpath(args.path, "anchor path")
    if rel.split("/")[0] == ".git":
        raise KitError("anchors cannot point into .git")
    real = resolve_inside(ctx.root, rel)
    if real == ctx.study or is_relative_to(real, ctx.study):
        raise KitError("anchors cannot point into the study directory")
    if not real.is_file():
        raise KitError("file not found: %s" % rel)
    try:
        text = real.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise KitError("file is not valid UTF-8: %s" % rel)
    lines = split_lines(text)
    if not lines:
        raise KitError("cannot anchor an empty file: %s" % rel)
    symbol = validate_symbol(args.symbol) if args.symbol else None
    start, end = args.start_line, args.end_line
    if (start is None) != (end is None):
        raise KitError("--start-line and --end-line must be given together")
    warnings: List[str] = []
    if start is None:
        if symbol:
            _kind, hits = locate_symbol(lines, symbol)
            if not hits:
                raise KitError("symbol %r not found in %s (heuristic text search)" % (symbol, rel))
            start = end = hits[0]
            if len(hits) > 1:
                warnings.append("ambiguous symbol %r: %d plausible matches at lines %s; using the first "
                                "(heuristic text match). Pass --start-line/--end-line to choose."
                                % (symbol, len(hits), ", ".join(str(h) for h in hits[:10])))
        else:
            start, end = 1, len(lines)
    else:
        if start < 1 or end < start or end > len(lines):
            raise KitError("line range %d-%d is out of bounds (file has %d lines)" % (start, end, len(lines)))
        if symbol:
            _kind, hits = locate_symbol(lines, symbol)
            if not hits:
                raise KitError("symbol %r not found in %s (heuristic text search)" % (symbol, rel))
            if not any(start <= h <= end for h in hits):
                warnings.append("symbol %r has no textual match within lines %d-%d" % (symbol, start, end))
    fingerprint = fingerprint_lines(lines, start, end)
    repo = ctx.git.repository_for(rel)
    if repo is None:
        raise KitError("%s is not inside a registered codebase; register it first: %s codebase add <path>"
                       % (rel, ctx.kernel_cmd()))
    commit = ctx.git.head_for(repo)
    if not ctx.git.available_for(repo):
        if repo == ".":
            warnings.append("not a Git repository; anchor freshness is limited (commit recorded as null)")
        else:
            warnings.append("codebase %s is not a Git work tree; anchor freshness is limited "
                            "(commit recorded as null)" % repo)
    elif commit is None:
        warnings.append("repository has no commits; anchor commit recorded as null")
    existing, aprobs = parse_anchor_block(body)
    if aprobs:
        raise KitError("%s has a malformed anchor block: %s" % (doc_id, aprobs[0]))
    key = (rel, symbol, start, end, fingerprint)
    for anchor in existing:
        if (anchor["path"], anchor["symbol"], anchor["start_line"], anchor["end_line"],
                anchor["fingerprint"]) == key:
            for w in warnings:
                ctx.warn(w)
            append_event(ctx, run_id, "anchor_skip_duplicate", [doc_id, anchor["id"]], {"path": rel})
            ctx.say("%s %s:%d-%d (duplicate skipped)" % (anchor["id"], rel, start, end))
            return 0
    number = max(max_number_in_files(ctx, all_doc_files(ctx), "ANC") + 1, 1)
    anchor_id = "ANC-%04d" % number
    record = {
        "id": anchor_id, "repository": repo, "commit": commit, "path": rel, "symbol": symbol,
        "start_line": start, "end_line": end, "fingerprint": fingerprint, "created_at": utc_now(),
    }
    fm["anchors"] = list(fm.get("anchors", [])) + [anchor_id]
    fm["updated"] = record["created_at"]
    fm["updated_by"] = ctx.agent
    write_doc_file(ctx, path, fm, set_anchor_block(body, existing + [record]))
    append_event(ctx, run_id, "anchor_add", [doc_id, anchor_id],
                 {"path": rel, "symbol": symbol, "start_line": start, "end_line": end})
    for w in warnings:
        ctx.warn(w)
    refresh_index(ctx)
    ctx.say("%s %s:%d-%d" % (anchor_id, rel, start, end))
    return 0


def evidence_commit(ctx: Context, anchor_ids: Sequence[str]) -> Optional[str]:
    """Commit recorded on an evidence record. In workspace mode it is the current HEAD of the single
    codebase the cited anchors belong to; null when there are no anchors or they span codebases."""
    if not ctx.codebases:
        return ctx.git.head()
    if not anchor_ids:
        return None
    by_id = {a["id"]: a for a in load_records(ctx, evaluate=False).anchors}
    repos = {by_id[i]["repository"] for i in anchor_ids if i in by_id}
    if len(repos) != 1:
        if len(repos) > 1:
            ctx.warn("cited anchors span several codebases (%s); evidence commit recorded as null"
                     % ", ".join(sorted(repos)))
        return None
    return ctx.git.head_for(next(iter(repos)))


def cmd_evidence_add(ctx: Context, args: argparse.Namespace) -> int:
    run_id = require_open_run(ctx, args.run)
    subject = args.subject
    if not ANY_ID_RE.match(subject or ""):
        raise KitError("invalid subject ID %r" % subject)
    if args.type not in EVIDENCE_TYPES:
        raise KitError("evidence type must be one of: %s" % ", ".join(EVIDENCE_TYPES))
    result = (args.result or "").strip()
    if not result:
        raise KitError("result must not be empty")
    anchors = validate_anchor_refs(ctx, args.anchor)
    limitations = [clean_line(x, "limitation", 2000) for x in (args.limitation or [])]
    if subject not in known_ids(ctx):
        raise KitError("unknown evidence subject: %s" % subject)
    number = max(max_number_in_files(ctx, all_evidence_files(ctx), "EV") + 1, 1)
    ev_id = "EV-%04d" % number
    record = {
        "schema": EVIDENCE_SCHEMA, "id": ev_id, "subject": subject, "type": args.type,
        "repository_commit": evidence_commit(ctx, anchors), "anchors": anchors, "producer": ctx.agent,
        "timestamp": utc_now(), "result": result, "limitations": limitations,
        "command": args.cmd_text, "exit_code": args.exit_code,
    }
    problems = validate_evidence_record(record)
    if problems:
        raise KitError("invalid evidence record: %s" % problems[0])
    line = json.dumps(record, ensure_ascii=False) + "\n"
    ctx.fs.append_bytes(ctx.study / "runs" / run_id / "evidence.jsonl", line.encode("utf-8"))
    # The command text is recorded only; the kernel never executes it.
    append_event(ctx, run_id, "evidence_add", [ev_id, subject],
                 {"type": args.type, "command": args.cmd_text, "exit_code": args.exit_code})
    refresh_index(ctx)
    ctx.say(ev_id)
    return 0


def _anchor_view(ctx: Context, rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    dirty: Set[str] = set()
    if rows and ctx.git.available():
        try:
            dirty = ctx.git.dirty_paths()
        except KitError:
            dirty = set()
    out = []
    for r in rows:
        state = evaluate_anchor(ctx, r, dirty)
        out.append({"id": r["id"], "path": r["path"], "symbol": r["symbol"], "start_line": r["start_line"],
                    "end_line": r["end_line"], "commit": r["repository_commit"],
                    "fingerprint": r["fingerprint"], "state": state})
    return out


def _read_events_bounded(ctx: Context, run_id: str, limit: int) -> List[Any]:
    path = ctx.study / "runs" / run_id / "events.jsonl"
    real = ctx.fs.check_read(path)
    if not real.is_file():
        return []
    events: List[Any] = []
    with open(str(real), "rb") as fh:
        for line in fh:
            if len(events) >= limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line.decode("utf-8")))
            except (UnicodeDecodeError, ValueError):
                events.append({"malformed": True, "line": truncate_utf8(line.decode("utf-8", "replace"), 200)})
    return events


def cmd_show(ctx: Context, args: argparse.Namespace) -> int:
    target = args.id
    if not ANY_ID_RE.match(target or ""):
        raise KitError("invalid ID: %r" % target)
    if args.events and not RUN_ID_RE.match(target):
        raise KitError("--events is only valid for RUN-NNNN IDs")
    if args.limit is not None and not args.events:
        raise KitError("--limit requires --events")
    limit = 50 if args.limit is None else args.limit
    if limit < 1 or limit > 1000:
        raise KitError("--limit must be between 1 and 1000")
    index = Index(ctx)
    doc = index.get_row("docs", target)
    payload: Dict[str, Any]
    if doc:
        content = ctx.fs.read_bytes(ctx.study / doc["path"]).decode("utf-8", errors="replace")
        anchors = _anchor_view(ctx, index.anchors_of(target))
        out_links = index.links_out(target)
        subjects = [target] + [l["dst"] for l in out_links if l["relation"] in ("anchor", "anchor-ref", "claim")]
        evidence = index.evidence_for_run(target) if doc["kind"] == "run" else index.evidence_for(subjects)
        payload = {
            "id": doc["id"], "kind": doc["kind"], "title": doc["title"], "status": doc["status"],
            "confidence": doc["confidence"], "area": doc["area"], "updated": doc["updated"],
            "path": doc["path"], "content": content, "anchors": anchors, "links_out": out_links,
            "backlinks": index.backlinks(target), "evidence": evidence,
        }
    elif ANC_ID_RE.match(target):
        row = index.get_row("anchors", target)
        if not row:
            raise KitError("unknown ID: %s" % target)
        payload = {"id": target, "kind": "anchor", "owner": row["doc_id"],
                   "anchor": _anchor_view(ctx, [row])[0], "backlinks": index.backlinks(target)}
    elif EV_ID_RE.match(target):
        row = index.get_row("evidence", target)
        if not row:
            raise KitError("unknown ID: %s" % target)
        payload = {"id": target, "kind": "evidence", "run": row["run_id"], "record": json.loads(row["raw_json"])}
    else:
        owner = index.claim_owner(target) if CLM_ID_RE.match(target) else None
        if not owner:
            raise KitError("unknown ID: %s" % target)
        payload = {"id": target, "kind": "claim", "owner": owner,
                   "evidence": index.evidence_for([target]), "backlinks": index.backlinks(target)}
    if args.events:
        payload["events"] = _read_events_bounded(ctx, target, limit)
        payload["events_limit"] = limit
    if ctx.json_mode:
        ctx.emit_json(payload)
        return 0
    _print_show(ctx, payload)
    return 0


def _print_show(ctx: Context, p: Dict[str, Any]) -> None:
    ctx.say("id: %s" % p["id"])
    ctx.say("kind: %s" % p["kind"])
    if "content" in p:
        for key in ("title", "status", "confidence", "area", "updated", "path"):
            ctx.say("%s: %s" % (key, p.get(key) if p.get(key) is not None else ""))
        ctx.say("")
        ctx.say("----- content -----")
        ctx.out.write(p["content"] if p["content"].endswith("\n") else p["content"] + "\n")
        ctx.say("----- anchors -----")
        for a in p["anchors"]:
            ctx.say("%s [%s] %s:%s-%s symbol=%s commit=%s" % (
                a["id"], a["state"], a["path"], a["start_line"], a["end_line"],
                a["symbol"] or "-", (a["commit"] or "null")[:12]))
        if not p["anchors"]:
            ctx.say("(none)")
        ctx.say("----- links out -----")
        for l in p["links_out"]:
            ctx.say("%s %s" % (l["relation"], l["dst"]))
        if not p["links_out"]:
            ctx.say("(none)")
        ctx.say("----- backlinks -----")
        for l in p["backlinks"]:
            ctx.say("%s %s" % (l["relation"], l["src"]))
        if not p["backlinks"]:
            ctx.say("(none)")
        ctx.say("----- evidence -----")
        for e in p["evidence"]:
            ctx.say("%s %s subject=%s run=%s at %s by %s" % (
                e["id"], e["type"], e["subject"], e["run_id"], e["timestamp"], e["producer"]))
        if not p["evidence"]:
            ctx.say("(none)")
    elif p["kind"] == "anchor":
        a = p["anchor"]
        ctx.say("owner: %s" % p["owner"])
        ctx.say("state: %s" % a["state"])
        ctx.say("location: %s:%s-%s symbol=%s" % (a["path"], a["start_line"], a["end_line"], a["symbol"] or "-"))
        ctx.say("commit: %s" % (a["commit"] or "null"))
        ctx.say("fingerprint: %s" % a["fingerprint"])
    elif p["kind"] == "evidence":
        ctx.say("run: %s" % p["run"])
        ctx.say(json.dumps(p["record"], indent=2, sort_keys=True))
    elif p["kind"] == "claim":
        ctx.say("owner: %s" % p["owner"])
        for e in p["evidence"]:
            ctx.say("evidence %s %s at %s" % (e["id"], e["type"], e["timestamp"]))
    if "events" in p:
        ctx.say("----- events (first %d max) -----" % p["events_limit"])
        for ev in p["events"]:
            ctx.say(json.dumps(ev, sort_keys=True))
        if not p["events"]:
            ctx.say("(none)")


def cmd_list(ctx: Context, args: argparse.Namespace) -> int:
    area = normalize_area(args.area) if args.area else None
    rows = Index(ctx).list_docs(args.kind, args.status, area, args.confidence)
    if ctx.json_mode:
        ctx.emit_json(rows)
        return 0
    if not rows:
        ctx.say("(no matching artifacts)")
    for r in rows:
        ctx.say("%-24s %-8s %-14s %-13s %s" % (r["id"], r["kind"], r["status"], r["confidence"] or "-", r["title"]))
    return 0


def cmd_search(ctx: Context, args: argparse.Namespace) -> int:
    terms = [t.strip() for t in args.words if t.strip()]
    if not terms:
        raise KitError("search needs at least one non-empty word")
    if args.limit < 1 or args.limit > 1000:
        raise KitError("--limit must be between 1 and 1000")
    rows = Index(ctx).search(terms, args.limit)
    if ctx.json_mode:
        ctx.emit_json(rows)
        return 0
    if not rows:
        ctx.say("(no matches)")
    for r in rows:
        ctx.say("%-24s %-8s %s  (%s)" % (r["id"], r["kind"], r["title"], r["path"]))
    return 0


def cmd_graph(ctx: Context, args: argparse.Namespace) -> int:
    if not ANY_ID_RE.match(args.id or ""):
        raise KitError("invalid ID: %r" % args.id)
    if args.depth < 0 or args.depth > 10:
        raise KitError("--depth must be between 0 and 10")
    result = Index(ctx).graph(args.id, args.depth)
    if ctx.json_mode:
        ctx.emit_json(result)
        return 0
    for n in result["nodes"]:
        label = "[%s] %s" % (n["kind"], n["title"]) if n["kind"] else ""
        ctx.say("%s%s %s" % ("  " * n["depth"], n["id"], label))
    ctx.say("edges:")
    for e in result["edges"]:
        ctx.say("  %s --%s--> %s" % (e["src"], e["relation"], e["dst"]))
    if not result["edges"]:
        ctx.say("  (none)")
    return 0


def walk_target(ctx: Context) -> Tuple[List[str], int]:
    files: Set[str] = set()
    skipped = 0
    starts = ctx.codebases if ctx.codebases else ["."]
    for start in starts:
        base = ctx.root if start == "." else ctx.root.joinpath(*start.split("/"))
        for dirpath, dirnames, filenames in os.walk(str(base), followlinks=False):
            kept = []
            for d in sorted(dirnames):
                full = os.path.join(dirpath, d)
                if d in COVERAGE_EXCLUDED_DIRS or Path(os.path.realpath(full)) == ctx.study:
                    continue
                if os.path.islink(full):
                    skipped += 1
                    continue
                kept.append(d)
            dirnames[:] = kept
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                if os.path.islink(full):
                    skipped += 1
                    continue
                files.add(Path(full).relative_to(ctx.root).as_posix())
    return sorted(files), skipped


def cmd_coverage(ctx: Context, args: argparse.Namespace) -> int:
    if args.depth < 1 or args.depth > 20:
        raise KitError("--depth must be between 1 and 20")
    rec = load_records(ctx, evaluate=False)
    anchored_paths = {a["path"] for a in rec.anchors}
    files, skipped = walk_target(ctx)
    groups: Dict[str, Dict[str, Any]] = {}
    for rel in files:
        parts = rel.split("/")[:-1]
        key = "/".join(parts[:args.depth]) or "."
        g = groups.setdefault(key, {"directory": key, "total": 0, "anchored": 0, "unanchored": 0,
                                    "unanchored_files": []})
        g["total"] += 1
        if rel in anchored_paths:
            g["anchored"] += 1
        else:
            g["unanchored"] += 1
            g["unanchored_files"].append(rel)
    ordered = [groups[k] for k in sorted(groups)]
    total = len(files)
    anchored = sum(g["anchored"] for g in ordered)
    payload = {"total": total, "anchored": anchored, "unanchored": total - anchored, "depth": args.depth,
               "skipped_symlinks": skipped, "groups": ordered,
               "note": "descriptive only: an anchored file is not necessarily understood or correct"}
    if rec.fatal_problems():
        ctx.warn("%d structural error(s) in study records; coverage may be incomplete (run: check)"
                 % len(rec.fatal_problems()))
    if ctx.json_mode:
        ctx.emit_json(payload)
        return 0
    ctx.say("files: %d total, %d anchored, %d unanchored (symlinks skipped: %d)"
            % (total, anchored, total - anchored, skipped))
    for g in ordered:
        ctx.say("%-30s total=%d anchored=%d unanchored=%d" % (g["directory"], g["total"], g["anchored"],
                                                              g["unanchored"]))
        for rel in g["unanchored_files"][:20]:
            ctx.say("    unanchored: %s" % rel)
        if len(g["unanchored_files"]) > 20:
            ctx.say("    ... and %d more" % (len(g["unanchored_files"]) - 20))
    ctx.say("note: " + payload["note"])
    return 0


def cmd_check(ctx: Context, args: argparse.Namespace) -> int:
    rec, problems = gather(ctx)
    index = Index(ctx)
    extra: List[Problem] = []
    if not rec.fatal_problems():
        try:
            index.trial_build(rec)
        except (sqlite3.Error, KitError) as exc:
            extra.append(Problem("error", "db-unbuildable", "index cannot be rebuilt: %s" % exc))
    if not index.exists():
        extra.append(Problem("warning", "db-missing", "index database is missing (run: rebuild)"))
    problems = sorted(problems + extra, key=problem_sort_key)
    errors = [p for p in problems if p.severity == "error"]
    warnings = [p for p in problems if p.severity == "warning"]
    anchor_states = {a["id"]: a["state"] for a in rec.anchors}
    if ctx.json_mode:
        ctx.emit_json({"ok": not errors, "errors": len(errors), "warnings": len(warnings),
                       "problems": [p.to_dict() for p in problems], "anchors": anchor_states})
    else:
        for p in problems:
            ctx.say(p.format())
        ctx.say("check: %d error(s), %d warning(s), %d anchor(s)" % (len(errors), len(warnings), len(anchor_states)))
    if errors:
        ctx.err.write("error: check failed with %d structural error(s)\n" % len(errors))
        return 1
    return 0


def scan_codebases(ctx: Context, max_depth: int = SCAN_MAX_DEPTH) -> List[str]:
    """Root-relative paths of Git work trees found below the root (read-only; does not descend into one)."""
    found: List[str] = []

    def has_git(directory: Path) -> bool:
        return (directory / ".git").exists()

    def walk(directory: Path, rel: str, depth: int) -> None:
        if rel != "." and has_git(directory):
            found.append(rel)
            return
        if depth >= max_depth:
            return
        try:
            names = sorted(os.listdir(str(directory)))
        except OSError:
            return
        for name in names:
            full = directory / name
            if name in COVERAGE_EXCLUDED_DIRS or name.startswith("."):
                continue
            if os.path.islink(str(full)) or not full.is_dir() or Path(os.path.realpath(str(full))) == ctx.study:
                continue
            walk(full, name if rel == "." else rel + "/" + name, depth + 1)

    if has_git(ctx.root):
        found.append(".")
    walk(ctx.root, ".", 0)
    return sorted(found)


def codebase_arg(ctx: Context, raw: str) -> str:
    """Normalize a user-supplied codebase path to a root-relative POSIX path ('.' for the root)."""
    if not isinstance(raw, str) or not raw.strip():
        raise KitError("codebase path must not be empty")
    if os.path.isabs(raw) or re.match(r"^[A-Za-z]:[\\/]", raw):
        real = Path(os.path.realpath(raw))
        if real == ctx.root:
            rel = "."
        elif is_relative_to(real, ctx.root):
            rel = real.relative_to(ctx.root).as_posix()
        else:
            raise KitError("codebase must be inside the study root %s: %s" % (ctx.root, raw))
    elif raw.replace("\\", "/").strip("/") in ("", "."):
        rel = "."
    else:
        rel = normalize_repo_relpath(raw.replace("\\", "/"), "codebase path")
    if rel != "." and rel.split("/")[0] in (".git", ".study"):
        raise KitError("not a valid codebase path: %s" % raw)
    real = resolve_inside(ctx.root, rel)
    if not real.is_dir():
        raise KitError("codebase directory not found: %s" % rel)
    if real == ctx.study or is_relative_to(real, ctx.study):
        raise KitError("the study directory cannot be a codebase")
    return rel


def write_codebases(ctx: Context, paths: Sequence[str], added: Dict[str, str]) -> None:
    entries = [{"path": p, "added": added.get(p) or utc_now()} for p in sorted(paths)]
    obj = {"schema": CODEBASES_SCHEMA, "codebases": entries}
    ctx.fs.atomic_write(ctx.study / CODEBASES_FILE, (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def registry_added(ctx: Context) -> Dict[str, str]:
    path = ctx.study / CODEBASES_FILE
    if not path.is_file():
        return {}
    try:
        obj = json.loads(ctx.fs.read_bytes(path).decode("utf-8"))
        return {e["path"]: e.get("added", "") for e in obj.get("codebases", [])}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def require_no_open_run(ctx: Context) -> None:
    open_runs = [r["id"] for r in load_records(ctx, evaluate=False).runs if r["status"] == "open"]
    if open_runs:
        raise KitError("end the open run(s) first (%s): changing the codebase map during a run would "
                       "make the source-change guard compare different sets of repositories" % ", ".join(open_runs))


def cmd_codebase(ctx: Context, args: argparse.Namespace) -> int:
    action = args.codebase_cmd
    if action == "scan":
        found = scan_codebases(ctx)
        rows = [{"path": p, "registered": p in ctx.codebases} for p in found]
        if ctx.json_mode:
            ctx.emit_json({"root": str(ctx.root), "candidates": rows})
            return 0
        if not rows:
            ctx.say("no Git work trees found within %d levels of %s" % (SCAN_MAX_DEPTH, ctx.root))
            return 0
        for row in rows:
            ctx.say("%-30s %s" % (row["path"], "registered" if row["registered"] else "not registered"))
        todo = [r["path"] for r in rows if not r["registered"]]
        if todo:
            ctx.say("register with: %s codebase add %s    (or: codebase add --detected)"
                    % (ctx.kernel_cmd(), " ".join(todo)))
        return 0

    if action == "list":
        rows = ctx.git.codebase_rows()
        anchors = load_records(ctx, evaluate=False).anchors
        for row in rows:
            row["anchors"] = len([a for a in anchors if a["repository"] == row["path"]])
        if ctx.json_mode:
            ctx.emit_json({"mode": "workspace" if ctx.codebases else "single", "root": str(ctx.root),
                           "codebases": rows})
            return 0
        if not rows:
            ctx.say("single-repository mode: the study root is the only codebase (%s)" % ctx.root)
            ctx.say("add codebases with: %s codebase add <path>   (find them: codebase scan)" % ctx.kernel_cmd())
            return 0
        ctx.say("workspace mode, root: %s" % ctx.root)
        for row in rows:
            state = ("HEAD %s, %d working-tree entries" % ((row["head"] or "none")[:12], row["changed_entries"])
                     if row["git"] else "not a Git work tree (guard unavailable)")
            ctx.say("  %-24s %s; anchors: %d" % (row["path"], state, row["anchors"]))
        return 0

    require_no_open_run(ctx)
    current = list(ctx.codebases)
    added = registry_added(ctx)
    if action == "add":
        wanted = [codebase_arg(ctx, raw) for raw in (args.paths or [])]
        if args.detected:
            wanted.extend(p for p in scan_codebases(ctx) if p not in wanted)
        if not wanted:
            raise KitError("give at least one PATH, or --detected (see: codebase scan)")
        results = []
        for rel in wanted:
            if rel in current:
                results.append({"path": rel, "result": "already registered"})
                continue
            base = ctx.root if rel == "." else ctx.root.joinpath(*rel.split("/"))
            probe = GitInspector(base, None)
            if not probe.available() and not args.no_git:
                raise KitError("%s is not a Git work tree (use --no-git to register it without a source-change "
                               "guard)" % rel)
            current.append(rel)
            added[rel] = utc_now()
            results.append({"path": rel, "result": "registered", "head": probe.head() if probe.available() else None,
                            "git": probe.available()})
        write_codebases(ctx, current, added)
        if ctx.json_mode:
            ctx.emit_json({"codebases": sorted(current), "results": results})
            return 0
        for r in results:
            note = "" if r["result"] != "registered" else (
                " (HEAD %s)" % (r["head"] or "none")[:12] if r["git"] else " (no Git: guard unavailable)")
            ctx.say("%s %s%s" % (r["result"], r["path"], note))
        return 0

    # remove
    rel = codebase_arg_loose(ctx, args.path)
    if rel not in current:
        raise KitError("not a registered codebase: %s (registered: %s)" % (rel, ", ".join(current) or "none"))
    current.remove(rel)
    write_codebases(ctx, current, added)
    kept = len([a for a in load_records(ctx, evaluate=False).anchors if a["repository"] == rel])
    if ctx.json_mode:
        ctx.emit_json({"removed": rel, "codebases": sorted(current), "anchors_kept": kept})
        return 0
    ctx.say("removed %s from the codebase map; no files or records were deleted" % rel)
    if kept:
        ctx.say("%d anchor(s) for it stay in the records and will be reported as codebase-unregistered" % kept)
    if not current:
        ctx.say("no codebases left: back to single-repository mode")
    return 0


def codebase_arg_loose(ctx: Context, raw: str) -> str:
    """Like codebase_arg, but the directory may no longer exist (it is being removed)."""
    try:
        return codebase_arg(ctx, raw)
    except KitError:
        rel = "." if raw.strip().strip("/") in ("", ".") else normalize_repo_relpath(raw.replace("\\", "/"), "codebase path")
        return rel


def say_codebases(ctx: Context, rows: Sequence[Dict[str, Any]]) -> None:
    ctx.say("codebases: %d" % len(rows))
    for row in rows:
        if row["git"]:
            ctx.say("  %-24s HEAD %s, %d working-tree entries"
                    % (row["path"], (row["head"] or "none")[:12], row["changed_entries"]))
        else:
            ctx.say("  %-24s not a Git work tree (guard unavailable)" % row["path"])


def cmd_status(ctx: Context, args: argparse.Namespace) -> int:
    rec, problems = gather(ctx)
    index = Index(ctx)
    counts: Dict[str, int] = {k: 0 for k in KINDS}
    for d in rec.docs:
        counts[d["kind"]] += 1
    open_findings = {s: 0 for s in SEVERITIES}
    finding_status = {s: 0 for s in KIND_STATUSES["finding"]}
    for d in rec.docs:
        if d["kind"] == "finding":
            finding_status[d["status"]] += 1
            if d["status"] == "open":
                open_findings[d["fm"]["severity"]] += 1
    open_runs = sorted(r["id"] for r in rec.runs if r["status"] == "open")
    anchor_counts = {s: 0 for s in ANCHOR_STATES}
    for a in rec.anchors:
        anchor_counts[a["state"]] += 1
    attention: List[str] = []
    for r in open_runs:
        attention.append("open run %s" % r)
    for d in rec.docs:
        if d["kind"] == "finding" and d["status"] == "open" and d["fm"]["severity"] in ("high", "critical"):
            attention.append("open %s finding %s: %s" % (d["fm"]["severity"], d["id"], d["title"]))
    quiet = {d["id"] for d in rec.docs if d["status"] in QUIET_STATUSES}
    for a in rec.anchors:
        if a["state"] != "ok" and a["doc_id"] not in quiet:
            attention.append("anchor %s is %s (%s)" % (a["id"], a["state"], a["path"]))
    for p in problems:
        if p.code == "finding-needs-recheck":
            attention.append(p.message)
    n_err = len([p for p in problems if p.severity == "error"])
    if n_err:
        attention.append("%d structural error(s); run: check" % n_err)
    if not index.exists():
        attention.append("index database missing; run: rebuild")
    if not ctx.git.available() and not ctx.codebases:
        attention.append("not a Git repository; freshness and the source-change guard are limited")
    rows = ctx.git.codebase_rows()
    for row in rows:
        if not row["git"]:
            attention.append("codebase %s is missing or not a Git work tree; its guard is unavailable" % row["path"])
    head = ctx.git.head()
    payload = {
        "git_available": ctx.git.available(), "git_head": head, "documents": counts,
        "open_findings": open_findings, "finding_status": finding_status, "open_runs": open_runs, "anchor_states": anchor_counts,
        "search_backend": index.backend(), "database": ctx.display(index.db_path),
        "database_present": index.exists(), "attention": attention,
    }
    if rows:
        payload["codebases"] = rows
    if ctx.json_mode:
        ctx.emit_json(payload)
        return 0
    if rows:
        say_codebases(ctx, rows)
    else:
        ctx.say("git head: %s" % (head or ("none" if ctx.git.available() else "none (not a Git repository)")))
    ctx.say("documents: " + " ".join("%s=%d" % (k, counts[k]) for k in KINDS))
    ctx.say("open findings: " + " ".join("%s=%d" % (s, open_findings[s]) for s in SEVERITIES))
    ctx.say("findings: " + " ".join("%s=%d" % (s, finding_status[s]) for s in KIND_STATUSES["finding"]))
    ctx.say("open runs: %s" % (", ".join(open_runs) if open_runs else "none"))
    ctx.say("anchors: " + " ".join("%s=%d" % (s, anchor_counts[s]) for s in ANCHOR_STATES))
    ctx.say("search backend: %s" % payload["search_backend"])
    ctx.say("database: %s (%s)" % (payload["database"], "present" if payload["database_present"] else "missing"))
    ctx.say("attention:" if attention else "attention: none")
    for item in attention[:30]:
        ctx.say("  - %s" % item)
    if len(attention) > 30:
        ctx.say("  ... and %d more" % (len(attention) - 30))
    return 0


# ---- claim, set, orient (added after the first V0 drop) ----

LANGUAGE_BY_EXT = {
    ".py": "Python", ".pyi": "Python", ".java": "Java", ".kt": "Kotlin", ".scala": "Scala",
    ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".go": "Go", ".rs": "Rust", ".c": "C", ".h": "C/C++ header",
    ".cc": "C++", ".cpp": "C++", ".cxx": "C++", ".hpp": "C++", ".cs": "C#", ".rb": "Ruby", ".php": "PHP",
    ".swift": "Swift", ".m": "Objective-C", ".sh": "Shell", ".bash": "Shell", ".ps1": "PowerShell",
    ".sql": "SQL", ".lua": "Lua", ".pl": "Perl", ".r": "R", ".dart": "Dart", ".ex": "Elixir",
    ".exs": "Elixir", ".erl": "Erlang", ".hs": "Haskell", ".clj": "Clojure", ".vue": "Vue",
    ".svelte": "Svelte", ".html": "HTML", ".css": "CSS", ".scss": "CSS", ".tf": "Terraform",
    ".proto": "Protocol Buffers", ".zig": "Zig",
}
BUILD_MARKERS = {
    "pyproject.toml": "Python (pyproject)", "setup.py": "Python (setuptools)", "setup.cfg": "Python (setuptools)",
    "requirements.txt": "pip requirements", "Pipfile": "Pipenv", "poetry.lock": "Poetry", "uv.lock": "uv",
    "tox.ini": "tox", "package.json": "npm/Node", "yarn.lock": "Yarn", "pnpm-lock.yaml": "pnpm",
    "package-lock.json": "npm", "tsconfig.json": "TypeScript", "Cargo.toml": "Cargo", "go.mod": "Go modules",
    "pom.xml": "Maven", "build.gradle": "Gradle", "build.gradle.kts": "Gradle", "settings.gradle": "Gradle",
    "Makefile": "make", "CMakeLists.txt": "CMake", "meson.build": "Meson", "BUILD.bazel": "Bazel",
    "WORKSPACE": "Bazel", "Gemfile": "Bundler", "composer.json": "Composer", "mix.exs": "Mix",
    "Dockerfile": "Docker", "docker-compose.yml": "Docker Compose", "docker-compose.yaml": "Docker Compose",
    "compose.yaml": "Docker Compose", "justfile": "just", "Taskfile.yml": "Task",
}
ENTRY_POINT_NAMES = {
    "main.py", "__main__.py", "manage.py", "app.py", "wsgi.py", "asgi.py", "server.py", "cli.py", "run.py",
    "index.js", "index.ts", "server.js", "server.ts", "app.js", "app.ts", "main.js", "main.ts",
    "main.go", "main.rs", "Main.java", "Application.java", "Program.cs", "main.c", "main.cpp",
}
TEST_DIR_NAMES = {"test", "tests", "__tests__", "spec", "specs", "e2e", "integration_tests"}
TEST_FILE_RES = (
    re.compile(r"^test_.*\.py$"), re.compile(r".*_test\.py$"), re.compile(r".*_test\.go$"),
    re.compile(r".*\.(test|spec)\.[jt]sx?$"), re.compile(r".*(Test|Tests|IT)\.java$"),
    re.compile(r".*_spec\.rb$"), re.compile(r".*Tests?\.cs$"),
)
DOC_NAMES = ("README.md", "README.rst", "README", "AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "ARCHITECTURE.md")
ORIENT_MAX_LIST = 15


def append_claim(body: str, line: str) -> str:
    return append_to_section(body, "Claims", line)


def cmd_claim_add(ctx: Context, args: argparse.Namespace) -> int:
    run_id = require_open_run(ctx, args.run)
    doc_id = args.document_id
    if not (ID_RE_BY_KIND["system"].match(doc_id or "") or ID_RE_BY_KIND["flow"].match(doc_id or "")):
        raise KitError("claims can only be added to a system or flow document (SYS-* or FLOW-*)")
    path = doc_path(ctx, doc_id)
    if not path.is_file():
        raise KitError("unknown document: %s" % doc_id)
    text = clean_line(args.text, "claim text", 1000)
    anchors = validate_anchor_refs(ctx, args.anchor)
    evidence: List[str] = []
    for ref in args.evidence or []:
        if not EV_ID_RE.match(ref):
            raise KitError("invalid evidence ID %r (expected EV-NNNN)" % ref)
        if ref not in evidence:
            evidence.append(ref)
    if evidence:
        known = {e["id"] for e in load_records(ctx, evaluate=False).evidence}
        for ref in evidence:
            if ref not in known:
                raise KitError("unknown evidence: %s" % ref)
    if not anchors and not evidence:
        raise KitError("a claim needs at least one --anchor or --evidence reference "
                       "(record unsupported ideas as an open question or a finding instead)")
    fm, body = read_doc_file(ctx, path)
    number = max_number_in_files(ctx, all_doc_files(ctx), "CLM") + 1
    claim_id = "CLM-%04d" % number
    statement = md_safe(text)
    if args.inference and not statement.startswith("Inference:"):
        statement = "Inference: " + statement
    refs = anchors + evidence
    line = "- %s: %s (%s)" % (claim_id, statement, ", ".join(refs))
    new_body = append_claim(body, line)
    fm["updated"] = utc_now()
    fm["updated_by"] = ctx.agent
    write_doc_file(ctx, path, fm, new_body.rstrip("\n") + "\n")
    append_event(ctx, run_id, "claim_add", [doc_id, claim_id] + refs, {"inference": bool(args.inference)})
    refresh_index(ctx)
    if ctx.json_mode:
        ctx.emit_json({"id": claim_id, "document": doc_id, "refs": refs})
    else:
        ctx.say(claim_id)
    return 0


def source_as_of(ctx: Context, fm: Dict[str, Any]) -> str:
    """Where the source stood when a finding was closed: the HEAD of each codebase its anchors live in."""
    wanted = set(fm.get("anchors", []))
    repos = sorted({a["repository"] for a in load_records(ctx, evaluate=False).anchors if a["id"] in wanted})
    if not repos:
        repos = [] if ctx.codebases else ["."]
    parts = []
    for repo in repos:
        head = ctx.git.head_for(repo)
        label = "HEAD" if repo == "." else repo
        parts.append("%s %s" % (label, head[:12] if head else "(no commit)"))
    return ", ".join(parts) or "no recorded codebase"


def cmd_set(ctx: Context, args: argparse.Namespace) -> int:
    run_id = require_open_run(ctx, args.run)
    doc_id = args.id
    if not ANY_ID_RE.match(doc_id or "") or RUN_ID_RE.match(doc_id) or not (
            ID_RE_BY_KIND["system"].match(doc_id) or ID_RE_BY_KIND["flow"].match(doc_id)
            or FINDING_ID_RE.match(doc_id)):
        raise KitError("set works on system, flow or finding documents (SYS-*, FLOW-*, F-NNNN); got %r" % doc_id)
    if args.status is None and args.confidence is None:
        raise KitError("nothing to change: give --status and/or --confidence")
    cited: List[str] = []
    for ref in args.evidence or []:
        if not EV_ID_RE.match(ref or ""):
            raise KitError("invalid evidence ID %r (expected EV-NNNN)" % ref)
        if ref not in cited:
            cited.append(ref)
    path = doc_path(ctx, doc_id)
    if not path.is_file():
        raise KitError("unknown document: %s" % doc_id)
    fm, body = read_doc_file(ctx, path)
    kind = fm["kind"]
    changes: List[str] = []
    note = clean_line(args.note, "note", 1000) if args.note else ""
    if args.status is not None:
        allowed = KIND_STATUSES[kind]
        if args.status not in allowed:
            raise KitError("status %r is not allowed for a %s (allowed: %s)" % (args.status, kind, ", ".join(allowed)))
        if args.status in ("dismissed", "deprecated") and not note:
            raise KitError("--note is required when setting status %s (say why)" % args.status)
        if kind == "finding" and args.status != fm["status"]:
            if args.status not in FINDING_TRANSITIONS[fm["status"]]:
                raise KitError("a %s finding can only move to: %s%s"
                               % (fm["status"], ", ".join(FINDING_TRANSITIONS[fm["status"]]),
                                  " (reopen it first)" if fm["status"] in FINDING_CLOSED else ""))
            if args.status in FINDING_NEEDS_EVIDENCE and not note:
                raise KitError("--note is required when setting status %s (say what you re-inspected)" % args.status)
            if args.status == "open" and fm["status"] in FINDING_CLOSED and not note:
                raise KitError("--note is required to reopen a finding (say why it is back)")
        if args.status in FINDING_NEEDS_EVIDENCE and kind == "finding" and args.status != fm["status"]:
            if not cited:
                raise KitError("status %s needs --evidence EV-NNNN from a re-inspection in this run: "
                               "%s evidence add %s --type source-inspection --result \"...\" --anchor ANC-NNNN --run %s"
                               % (args.status, ctx.kernel_cmd(), doc_id, run_id))
        if args.status != fm["status"]:
            changes.append("status %s -> %s" % (fm["status"], args.status))
            fm["status"] = args.status
    if cited:
        if kind != "finding" or args.status is None:
            raise KitError("--evidence applies to a finding status change")
        known = {e["id"]: e for e in load_records(ctx, evaluate=False).evidence}
        for ref in cited:
            ev = known.get(ref)
            if ev is None:
                raise KitError("unknown evidence: %s" % ref)
            if args.status in FINDING_NEEDS_EVIDENCE and (ev["run_id"] != run_id or ev["subject"] != doc_id):
                raise KitError("%s must be recorded in this run (%s) with subject %s, so it shows the current "
                               "state of the source; it is from %s about %s"
                               % (ref, run_id, doc_id, ev["run_id"], ev["subject"]))
    as_of = ""
    if kind == "finding" and args.status in FINDING_NEEDS_EVIDENCE and changes:
        as_of = source_as_of(ctx, fm)
    if args.confidence is not None:
        if args.confidence not in V0_CREATABLE_CONFIDENCE:
            raise KitError("confidence must be one of %s in V0 ('corroborated', 'executed' and 'verified' are "
                           "reserved for later extensions)" % ", ".join(V0_CREATABLE_CONFIDENCE))
        if "confidence" not in fm:
            raise KitError("%s has no confidence field" % doc_id)
        if args.confidence != fm["confidence"]:
            changes.append("confidence %s -> %s" % (fm["confidence"], args.confidence))
            fm["confidence"] = args.confidence
    if not changes:
        ctx.say("%s unchanged" % doc_id)
        return 0
    stamp = utc_now()
    entry = "- %s %s: %s (%s)" % (stamp, ctx.agent, "; ".join(changes), run_id)
    if note:
        entry += " - " + md_safe(note)
    extra = []
    if cited:
        extra.append("evidence " + ", ".join(cited))
    if as_of:
        extra.append("source as of " + as_of)
    if extra:
        entry += " [" + "; ".join(extra) + "]"
    body = append_to_section(body, "Status log", entry)
    fm["updated"] = stamp
    fm["updated_by"] = ctx.agent
    write_doc_file(ctx, path, fm, body.rstrip("\n") + "\n")
    append_event(ctx, run_id, "set", [doc_id] + cited, {"changes": changes, "note": note, "evidence": cited,
                                                       "source_as_of": as_of})
    refresh_index(ctx)
    if ctx.json_mode:
        ctx.emit_json({"id": doc_id, "changes": changes})
    else:
        ctx.say("%s: %s" % (doc_id, "; ".join(changes)))
    return 0


def _read_small_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if path.stat().st_size > 1024 * 1024:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def orient_scan(ctx: Context, files: Sequence[str]) -> Dict[str, Any]:
    """Filename-and-layout survey. Candidates only: nothing here is verified behavior."""
    languages: Dict[str, int] = {}
    build: Dict[str, List[str]] = {}
    test_dirs: Dict[str, int] = {}
    test_files = 0
    entries: List[str] = []
    docs: List[str] = []
    ci: List[str] = []
    top: Dict[str, int] = {}
    for rel in files:
        parts = rel.split("/")
        name = parts[-1]
        top_key = parts[0] if len(parts) > 1 else "."
        top[top_key] = top.get(top_key, 0) + 1
        lang = LANGUAGE_BY_EXT.get(os.path.splitext(name)[1].lower())
        if lang:
            languages[lang] = languages.get(lang, 0) + 1
        if name in BUILD_MARKERS:
            build.setdefault(BUILD_MARKERS[name], []).append(rel)
        if name in ENTRY_POINT_NAMES:
            entries.append(rel)
        if any(r.match(name) for r in TEST_FILE_RES):
            test_files += 1
        for depth, part in enumerate(parts[:-1]):
            if part.lower() in TEST_DIR_NAMES:
                key = "/".join(parts[:depth + 1])
                test_dirs[key] = test_dirs.get(key, 0) + 1
                break
        if len(parts) == 1 and name in DOC_NAMES:
            docs.append(rel)
        if rel.startswith(".github/workflows/") or name in (".gitlab-ci.yml", "Jenkinsfile", ".travis.yml",
                                                            "azure-pipelines.yml", ".circleci/config.yml"):
            ci.append(rel)
    package_hints: List[str] = []
    pkg_rel = next((r for r in build.get("npm/Node", []) if r == "package.json"), None)
    if pkg_rel:
        pkg = _read_small_json(ctx.root / pkg_rel)
        if pkg:
            if isinstance(pkg.get("main"), str):
                package_hints.append("package.json main: %s" % truncate_utf8(pkg["main"], 200))
            if isinstance(pkg.get("bin"), str):
                package_hints.append("package.json bin: %s" % truncate_utf8(pkg["bin"], 200))
            elif isinstance(pkg.get("bin"), dict):
                package_hints.extend("package.json bin %s: %s" % (truncate_utf8(str(k), 80), truncate_utf8(str(v), 200))
                                     for k, v in sorted(pkg["bin"].items())[:5])
            scripts = pkg.get("scripts")
            if isinstance(scripts, dict):
                package_hints.append("package.json scripts: " + ", ".join(sorted(str(k) for k in scripts)[:15]))
    go_cmds = sorted({r for r in files if re.match(r"^cmd/[^/]+/main\.go$", r)})
    ordered_langs = sorted(languages.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "languages": [{"language": k, "files": v} for k, v in ordered_langs],
        "build_tools": [{"tool": k, "files": sorted(v)[:5]} for k, v in sorted(build.items())],
        "test_locations": [{"path": k, "files": v} for k, v in sorted(test_dirs.items(), key=lambda kv: (-kv[1], kv[0]))],
        "test_file_matches": test_files,
        "entry_point_candidates": sorted(set(entries) | set(go_cmds)),
        "manifest_hints": package_hints,
        "docs": sorted(docs),
        "ci": sorted(ci),
        "top_level": [{"directory": k, "files": v} for k, v in sorted(top.items(), key=lambda kv: (-kv[1], kv[0]))],
    }


def cmd_orient(ctx: Context, args: argparse.Namespace) -> int:
    files, skipped = walk_target(ctx)
    scan = orient_scan(ctx, files)
    rec = load_records(ctx, evaluate=False)
    anchored = {a["path"] for a in rec.anchors}
    payload = {
        "root": str(ctx.root),
        "git_available": ctx.git.available(),
        "git_head": ctx.git.head(),
        "codebases": ctx.git.codebase_rows(),
        "files": len(files),
        "skipped_symlinks": skipped,
        "anchored_files": len([f for f in files if f in anchored]),
        "study_documents": {k: len([d for d in rec.docs if d["kind"] == k]) for k in KINDS},
        "note": "filename and layout heuristics only: candidates, not verified behavior. "
                "Read the source and anchor what you rely on.",
    }
    payload.update(scan)
    if ctx.json_mode:
        ctx.emit_json(payload)
        return 0
    n = ORIENT_MAX_LIST
    ctx.say("root: %s" % ctx.root)
    if payload["codebases"]:
        say_codebases(ctx, payload["codebases"])
    else:
        ctx.say("git head: %s" % (payload["git_head"] or ("none" if payload["git_available"] else "none (not a Git repository)")))
    ctx.say("files: %d (symlinks skipped: %d), anchored: %d" % (len(files), skipped, payload["anchored_files"]))
    ctx.say("study documents: " + " ".join("%s=%d" % (k, v) for k, v in payload["study_documents"].items()))
    langs = payload["languages"]
    ctx.say("languages: " + (", ".join("%s (%d)" % (x["language"], x["files"]) for x in langs[:8]) or "none detected")
            + (" ..." if len(langs) > 8 else ""))
    ctx.say("build tools: " + (", ".join("%s [%s]" % (x["tool"], x["files"][0]) for x in payload["build_tools"][:n])
                               or "none detected"))
    tl = payload["test_locations"]
    ctx.say("test locations: " + (", ".join("%s (%d files)" % (x["path"], x["files"]) for x in tl[:n])
                                  or "no test directories") + "; test-named files: %d" % payload["test_file_matches"])
    eps = payload["entry_point_candidates"]
    ctx.say("entry point candidates (by filename convention):" + ("" if eps else " none"))
    for e in eps[:n]:
        ctx.say("  - %s" % e)
    if len(eps) > n:
        ctx.say("  ... and %d more" % (len(eps) - n))
    for hint in payload["manifest_hints"]:
        ctx.say("  - %s" % hint)
    ctx.say("docs: " + (", ".join(payload["docs"]) or "none at root"))
    ctx.say("ci: " + (", ".join(payload["ci"][:n]) or "none detected"))
    ctx.say("top-level directories (file counts):")
    for x in payload["top_level"][:n]:
        ctx.say("  %-30s %d" % (x["directory"], x["files"]))
    if len(payload["top_level"]) > n:
        ctx.say("  ... and %d more" % (len(payload["top_level"]) - n))
    ctx.say("note: " + payload["note"])
    return 0


# =============================================================================
# 11. CLI parser and dispatch
# =============================================================================


class KitArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise KitError(message)


TOP_EPILOG = """\
examples:
  python kernel.py init --root /path/to/repo
  python .study/kernel.py status
  python .study/kernel.py run start --goal "Map the login flow"
  python .study/kernel.py new system auth --title "Authentication" --area src/auth --run RUN-0001
  python .study/kernel.py anchor add SYS-auth src/auth/token.py --symbol verify_token --run RUN-0001
  python .study/kernel.py evidence add ANC-0001 --type source-inspection --result "..." --run RUN-0001
  python .study/kernel.py claim add SYS-auth "verify_token rejects empty tokens" --anchor ANC-0001 --run RUN-0001
  python .study/kernel.py orient
  python .study/kernel.py codebase scan                       # study root holds several repositories
  python .study/kernel.py codebase add --detected
  python .study/kernel.py search token expiry
  python .study/kernel.py run end --id RUN-0001 --summary "Mapped auth entry points"

global options (accepted before or after the command): --root, --study-dir, --agent, --json,
--max-event-bytes. Errors print 'error: <message>' on stderr and exit 1.
"""


def build_parser() -> argparse.ArgumentParser:
    common = KitArgumentParser(add_help=False)
    common.add_argument("--root", metavar="PATH", default=argparse.SUPPRESS,
                        help="target repository (default: parent of .study, else current directory)")
    common.add_argument("--study-dir", dest="study_dir", metavar="PATH", default=argparse.SUPPRESS,
                        help="study directory (default: <root>/.study; must resolve inside root)")
    common.add_argument("--agent", metavar="NAME", default=argparse.SUPPRESS,
                        help="agent name (default: $STUDY_AGENT or 'unknown')")
    common.add_argument("--json", dest="json_out", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable output where supported")
    common.add_argument("--max-event-bytes", dest="max_event_bytes", type=int, metavar="N",
                        default=argparse.SUPPRESS,
                        help="truncate strings recorded in events to N bytes (default 16384)")
    parser = KitArgumentParser(
        prog="kernel.py", description="Engineering Study Kit V0 (Study mode, read-only first).",
        epilog=TOP_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter, parents=[common])
    parser.add_argument("--version", action="version", version="engineering-study-kit " + VERSION)
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")
    sub.required = True

    def leaf(subparsers: Any, name: str, help_text: str, example: str) -> argparse.ArgumentParser:
        return subparsers.add_parser(name, help=help_text, parents=[common],
                                     description=help_text, epilog="example:\n  " + example,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)

    p = leaf(sub, "init", "install the study directory into a repository",
             "python kernel.py init --root /path/to/repo")
    p.add_argument("--force", action="store_true", help="overwrite kit files (never durable records)")

    p = sub.add_parser("run", help="start or end a study run")
    rsub = p.add_subparsers(dest="run_cmd", metavar="ACTION")
    rsub.required = True
    q = leaf(rsub, "start", "start a run and snapshot source state",
             'python .study/kernel.py run start --goal "Map auth"')
    q.add_argument("--goal", required=True, help="what this run intends to study")
    q = leaf(rsub, "end", "end a run, verify the source is unchanged, write the handoff",
             'python .study/kernel.py run end --id RUN-0001 --summary "Mapped auth" --next "Trace refresh"')
    q.add_argument("--id", required=True, help="explicit run ID (RUN-NNNN)")
    q.add_argument("--summary", required=True, help="handoff summary text")
    q.add_argument("--next", help="suggested next step")
    q.add_argument("--open-question", dest="open_question", action="extend", nargs="+", metavar="TEXT",
                   help="an open question (repeatable)")
    q.add_argument("--covered", action="extend", nargs="+", metavar="ID", help="document IDs covered")

    p = sub.add_parser("new", help="create a system or flow document")
    nsub = p.add_subparsers(dest="new_kind", metavar="KIND")
    nsub.required = True
    for kind in ("system", "flow"):
        q = leaf(nsub, kind, "create a %s document" % kind,
                 'python .study/kernel.py new %s my-slug --title "Title" --area src/x --run RUN-0001' % kind)
        q.add_argument("slug", help="lowercase letters, digits, single hyphens")
        q.add_argument("--title", required=True)
        q.add_argument("--area", help="repository-relative path")
        q.add_argument("--run", required=True, help="open run ID")

    q = leaf(sub, "finding", "record a potential finding (never apply a fix)",
             'python .study/kernel.py finding "Expiry not checked" --severity medium --anchor ANC-0001 --run RUN-0001')
    q.add_argument("title")
    q.add_argument("--severity", required=True, choices=SEVERITIES)
    q.add_argument("--area")
    q.add_argument("--anchor", action="extend", nargs="+", metavar="ANC-NNNN", help="existing anchor IDs")
    q.add_argument("--run", required=True)

    p = sub.add_parser("claim", help="record a source-grounded claim in a system or flow")
    csub = p.add_subparsers(dest="claim_cmd", metavar="ACTION")
    csub.required = True
    q = leaf(csub, "add", "append a numbered claim to a document's Claims section",
             'python .study/kernel.py claim add SYS-auth "verify_token rejects empty tokens" --anchor ANC-0001 --evidence EV-0001 --run RUN-0001')
    q.add_argument("document_id", help="SYS-* or FLOW-* document")
    q.add_argument("text", help="the claim, one line")
    q.add_argument("--anchor", action="extend", nargs="+", metavar="ANC-NNNN", help="existing anchor IDs")
    q.add_argument("--evidence", action="extend", nargs="+", metavar="EV-NNNN", help="existing evidence IDs")
    q.add_argument("--inference", action="store_true", help="prefix the claim with 'Inference:'")
    q.add_argument("--run", required=True, help="open run ID")

    q = leaf(sub, "set", "change a document's status and/or confidence (logged in its Status log)",
             'python .study/kernel.py set F-0001 --status dismissed --note "intended behavior, see ADR" --run RUN-0001')
    q.add_argument("id", help="SYS-*, FLOW-* or F-NNNN")
    q.add_argument("--status", help="system/flow: draft, reviewed, deprecated; "
                                    "finding: open, triaged, resolved, obsolete, dismissed")
    q.add_argument("--confidence", choices=V0_CREATABLE_CONFIDENCE, help="hypothesis, inferred or observed")
    q.add_argument("--note", help="reason (required for dismissed, deprecated, resolved, obsolete and for reopening)")
    q.add_argument("--evidence", action="extend", nargs="+", metavar="EV-NNNN",
                   help="re-inspection evidence from this run about the finding (required for resolved, obsolete)")
    q.add_argument("--run", required=True, help="open run ID")

    leaf(sub, "orient", "survey languages, build tools, tests and entry-point candidates (read-only)",
         "python .study/kernel.py orient --json")

    p = sub.add_parser("anchor", help="manage anchors")
    asub = p.add_subparsers(dest="anchor_cmd", metavar="ACTION")
    asub.required = True
    q = leaf(asub, "add", "anchor a document to source text",
             "python .study/kernel.py anchor add SYS-auth src/auth/token.py --start-line 40 --end-line 61 --run RUN-0001")
    q.add_argument("document_id")
    q.add_argument("path", help="repository-relative POSIX path")
    q.add_argument("--symbol")
    q.add_argument("--start-line", dest="start_line", type=int)
    q.add_argument("--end-line", dest="end_line", type=int)
    q.add_argument("--run", required=True)

    p = sub.add_parser("codebase", help="manage which repositories under the study root are studied")
    bsub = p.add_subparsers(dest="codebase_cmd", metavar="ACTION")
    bsub.required = True
    q = leaf(bsub, "add", "register repositories (workspace mode); anchors must fall inside one",
             "python .study/kernel.py codebase add api web   |   codebase add --detected")
    q.add_argument("paths", nargs="*", metavar="PATH", help="directory relative to the study root")
    q.add_argument("--detected", action="store_true", help="register every Git work tree found by 'codebase scan'")
    q.add_argument("--no-git", dest="no_git", action="store_true",
                   help="allow a directory that is not a Git work tree (no source-change guard)")
    q = leaf(bsub, "remove", "unregister a repository; records and files are kept",
             "python .study/kernel.py codebase remove web")
    q.add_argument("path", metavar="PATH")
    leaf(bsub, "list", "show registered repositories, their HEAD and working-tree state",
         "python .study/kernel.py codebase list")
    leaf(bsub, "scan", "find Git work trees below the study root (read-only)",
         "python .study/kernel.py codebase scan")

    p = sub.add_parser("evidence", help="record evidence")
    esub = p.add_subparsers(dest="evidence_cmd", metavar="ACTION")
    esub.required = True
    q = leaf(esub, "add", "append an evidence record to a run (the command text is never executed)",
             'python .study/kernel.py evidence add CLM-0001 --type source-inspection --result "..." --run RUN-0001')
    q.add_argument("subject", help="ID of the thing the evidence concerns")
    q.add_argument("--type", required=True, choices=EVIDENCE_TYPES)
    q.add_argument("--result", required=True)
    q.add_argument("--anchor", action="extend", nargs="+", metavar="ANC-NNNN")
    q.add_argument("--limitation", action="extend", nargs="+", metavar="TEXT")
    q.add_argument("--command", dest="cmd_text", help="command text that produced the observation (recorded only)")
    q.add_argument("--exit-code", dest="exit_code", type=int)
    q.add_argument("--run", required=True)

    q = leaf(sub, "show", "show an artifact, its anchors, links, backlinks and evidence metadata",
             "python .study/kernel.py show RUN-0001 --events --limit 10")
    q.add_argument("id")
    q.add_argument("--events", action="store_true", help="include raw run events (bounded; RUN IDs only)")
    q.add_argument("--limit", type=int, help="max events with --events (default 50)")

    q = leaf(sub, "list", "list artifacts", "python .study/kernel.py list --kind finding --status open")
    q.add_argument("--kind", choices=KINDS)
    q.add_argument("--status")
    q.add_argument("--area")
    q.add_argument("--confidence", choices=CONFIDENCE_LEVELS)

    q = leaf(sub, "search", "search document titles and bodies (AND semantics)",
             "python .study/kernel.py search token expiry --limit 5")
    q.add_argument("words", nargs="+", metavar="WORD")
    q.add_argument("--limit", type=int, default=20)

    q = leaf(sub, "graph", "traverse links and anchor ownership", "python .study/kernel.py graph SYS-auth --depth 2")
    q.add_argument("id")
    q.add_argument("--depth", type=int, default=2, help="0-10 (default 2)")

    q = leaf(sub, "coverage", "report anchored vs unanchored files (descriptive only)",
             "python .study/kernel.py coverage --depth 2")
    q.add_argument("--depth", type=int, default=1, help="directory grouping depth (default 1)")

    leaf(sub, "check", "validate records, anchors and index rebuildability", "python .study/kernel.py check --json")
    leaf(sub, "rebuild", "atomically rebuild the disposable index", "python .study/kernel.py rebuild")
    leaf(sub, "status", "summarize study state and items needing attention", "python .study/kernel.py status")
    return parser


def dispatch_key(args: argparse.Namespace) -> str:
    cmd = args.cmd
    if cmd == "run":
        return "run:" + args.run_cmd
    if cmd == "new":
        return "new"
    if cmd == "anchor":
        return "anchor:" + args.anchor_cmd
    if cmd == "evidence":
        return "evidence:" + args.evidence_cmd
    if cmd == "claim":
        return "claim:" + args.claim_cmd
    if cmd == "codebase":
        return "codebase:" + args.codebase_cmd
    return cmd


HANDLERS = {
    "init": cmd_init, "run:start": cmd_run_start, "run:end": cmd_run_end, "new": cmd_new,
    "finding": cmd_finding, "anchor:add": cmd_anchor_add, "claim:add": cmd_claim_add, "set": cmd_set, "orient": cmd_orient, "evidence:add": cmd_evidence_add,
    "show": cmd_show, "list": cmd_list, "search": cmd_search, "graph": cmd_graph,
    "codebase:add": cmd_codebase, "codebase:remove": cmd_codebase, "codebase:list": cmd_codebase,
    "codebase:scan": cmd_codebase,
    "coverage": cmd_coverage, "check": cmd_check, "rebuild": cmd_rebuild, "status": cmd_status,
}


def main(argv: Optional[Sequence[str]] = None, stdout: Any = None, stderr: Any = None) -> int:
    real_streams = stdout is None and stderr is None
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    if real_streams:
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure:
                try:
                    reconfigure(encoding="utf-8", errors="replace")
                except (OSError, ValueError):
                    pass
    try:
        args = build_parser().parse_args(argv)
        key = dispatch_key(args)
        ctx = build_context(args, out, err, require_study=(key not in ("init", "orient", "codebase:scan")))
        return int(HANDLERS[key](ctx, args) or 0)
    except KitError as exc:
        print("error: %s" % exc, file=err)
        return 1
    except SystemExit as exc:  # -h / --version
        return 0 if exc.code in (None, 0) else 1
    except KeyboardInterrupt:
        print("error: interrupted", file=err)
        return 1
    except OSError as exc:
        print("error: %s" % exc, file=err)
        return 1


if __name__ == "__main__":
    sys.exit(main())
