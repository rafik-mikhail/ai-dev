"""emkit command line: install the packaged Study kit into a repository.

Only two jobs live here: `init` copies the self-contained runtime into a repository, and `doctor`
reports whether an installation is usable. Everything else is done by the installed
`.study/kernel.py`, which is a byte-for-byte copy of the packaged `kernel.py` and never imports
this package.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from importlib import resources
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Sequence, Set, Tuple

from . import __version__

MIN_PYTHON = (3, 9)
STUDY_DIR = ".study"
ARTIFACT_DIRS = ("systems", "flows", "findings", "runs", "scratch")
KERNEL_TIMEOUT = 120


class EmkitError(Exception):
    """A user-facing error. Rendered as 'error: <message>' on stderr, exit 1."""


class Managed(NamedTuple):
    dest: str  # POSIX path relative to the repository root
    source: Optional[Tuple[str, ...]]  # path parts inside the package; None = generated


MANAGED: Tuple[Managed, ...] = (
    Managed(".study/kernel.py", ("kernel.py",)),
    Managed(".study/PROTOCOL.md", ("resources", "PROTOCOL.md")),
    Managed(".study/schema.json", ("resources", "schema.json")),
    Managed(".study/templates/system.md", ("resources", "templates", "system.md")),
    Managed(".study/templates/flow.md", ("resources", "templates", "flow.md")),
    Managed(".study/templates/finding.md", ("resources", "templates", "finding.md")),
    Managed(".study/templates/run.md", ("resources", "templates", "run.md")),
    Managed(".study/AGENTS.md", ("resources", "AGENTS.md")),
    Managed(".study/VERSION", None),
)
AGENTS_FILE = "AGENTS.md"  # at the root; never owned by emkit, only a marked block inside it
BLOCK_BEGIN = "<!-- emkit:begin -->"
BLOCK_END = "<!-- emkit:end -->"
EXCLUDE_ENTRY = STUDY_DIR + "/"  # added to .git/info/exclude so the workspace stays out of `git status`
STUDY_MANAGED = tuple(m for m in MANAGED if m.dest.startswith(STUDY_DIR + "/"))
TEMPLATE_DESTS = tuple(m.dest for m in MANAGED if m.dest.startswith(".study/templates/"))


# ---------------------------------------------------------------------------
# Packaged resources
# ---------------------------------------------------------------------------

def packaged_bytes(m: Managed) -> bytes:
    """Read a managed file's canonical content from the installed package."""
    if m.source is None:
        return (__version__ + "\n").encode("utf-8")
    node = resources.files("emkit")
    for part in m.source:
        node = node / part
    try:
        return node.read_bytes()
    except (OSError, FileNotFoundError) as exc:
        raise EmkitError("packaged resource missing: %s (%s)" % ("/".join(m.source), exc))


def local_path(root: Path, rel: str) -> Path:
    return root.joinpath(*rel.split("/"))


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

def atomic_write(path: Path, data: bytes) -> None:
    """Write via a temporary file in the same directory, then rename into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".emkit-tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp_name, str(path))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def is_inside(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


def resolve_target(raw: str, create: bool) -> Path:
    path = Path(raw).expanduser()
    if path.exists():
        if not path.is_dir():
            raise EmkitError("not a directory: %s" % path)
    elif create:
        try:
            path.mkdir(parents=True)
        except OSError as exc:
            raise EmkitError("cannot create %s: %s" % (path, exc))
    return Path(os.path.realpath(str(path)))


def check_layout_safe(root: Path) -> None:
    """Refuse to write through symlinks or to a .study that escapes the repository."""
    study = root / STUDY_DIR
    if os.path.lexists(str(study)):
        if os.path.islink(str(study)):
            raise EmkitError("%s is a symlink; refusing to install through it" % STUDY_DIR)
        if not study.is_dir():
            raise EmkitError("%s exists and is not a directory" % STUDY_DIR)
        if not is_inside(Path(os.path.realpath(str(study))), root):
            raise EmkitError("%s resolves outside the repository" % STUDY_DIR)
    for m in MANAGED:
        dest = local_path(root, m.dest)
        if os.path.islink(str(dest)):
            raise EmkitError("%s is a symlink; refusing to write through it" % m.dest)
        if os.path.lexists(str(dest)) and not dest.is_file():
            raise EmkitError("%s exists and is not a regular file" % m.dest)
    agents = local_path(root, AGENTS_FILE)
    if os.path.islink(str(agents)):
        raise EmkitError("%s is a symlink; refusing to write through it" % AGENTS_FILE)
    if os.path.lexists(str(agents)) and not agents.is_file():
        raise EmkitError("%s exists and is not a regular file" % AGENTS_FILE)
    for sub in ("templates",) + ARTIFACT_DIRS:
        d = study / sub
        if os.path.islink(str(d)):
            raise EmkitError("%s/%s is a symlink; refusing to install through it" % (STUDY_DIR, sub))
        if os.path.lexists(str(d)) and not d.is_dir():
            raise EmkitError("%s/%s exists and is not a directory" % (STUDY_DIR, sub))


def file_state(root: Path, m: Managed) -> str:
    """'missing', 'same' or 'differs' relative to the packaged content."""
    dest = local_path(root, m.dest)
    if not dest.is_file():
        return "missing"
    try:
        return "same" if dest.read_bytes() == packaged_bytes(m) else "differs"
    except OSError:
        return "differs"


def agents_body() -> str:
    raw = packaged_bytes(Managed(".study/AGENTS.md", ("resources", "AGENTS.md")))
    return raw.decode("utf-8").replace("\r\n", "\n").strip("\n")


def agents_plan(root: Path) -> Tuple[str, Optional[bytes]]:
    """Decide what to do with the root AGENTS.md. The file belongs to the repository owner: emkit only ever
    adds or refreshes one marked block and never touches the text around it.

    Returns (state, new_content): missing | absent | same | differs | broken | unreadable."""
    path = local_path(root, AGENTS_FILE)
    body = agents_body()
    if not path.is_file():
        block = "%s\n%s\n%s\n" % (BLOCK_BEGIN, body, BLOCK_END)
        return "missing", ("# AGENTS.md\n\n" + block).encode("utf-8")
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return "unreadable", None
    eol = "\r\n" if "\r\n" in text else "\n"
    block = (BLOCK_BEGIN + "\n" + body + "\n" + BLOCK_END).replace("\n", eol)
    begins, ends = text.count(BLOCK_BEGIN), text.count(BLOCK_END)
    if begins == 0 and ends == 0:
        sep = "" if not text else (eol if text.endswith("\n") else eol + eol)
        return "absent", (text + sep + block + eol).encode("utf-8")
    start, end = text.find(BLOCK_BEGIN), text.find(BLOCK_END)
    if begins != 1 or ends != 1 or end < start:
        return "broken", None
    end += len(BLOCK_END)
    if text[start:end].replace("\r\n", "\n") == (BLOCK_BEGIN + "\n" + body + "\n" + BLOCK_END):
        return "same", None
    return "differs", (text[:start] + block + text[end:]).encode("utf-8")


# ---------------------------------------------------------------------------
# Git and kernel subprocesses
# ---------------------------------------------------------------------------

def _git(root: Path, *args: str) -> Optional[Tuple[int, bytes]]:
    env = dict(os.environ)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(key, None)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["LC_ALL"] = "C"
    cmd = ["git", "-c", "core.fsmonitor=false", "--no-optional-locks", "-C", str(root)] + list(args)
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                              timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.returncode, proc.stdout


def git_info(root: Path) -> Dict[str, object]:
    """Never raises. {'git': bool, 'repo': bool, 'head': str|None, 'dirty': set|None}."""
    probe = _git(root, "--version")
    if probe is None or probe[0] != 0:
        return {"git": False, "repo": False, "head": None, "dirty": None}
    inside = _git(root, "rev-parse", "--is-inside-work-tree")
    if not inside or inside[0] != 0 or inside[1].strip() != b"true":
        return {"git": True, "repo": False, "head": None, "dirty": None}
    head = _git(root, "rev-parse", "--verify", "--quiet", "HEAD")
    sha = head[1].decode("ascii", "replace").strip() if head and head[0] == 0 else None
    status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", ".",
                  ":(exclude)%s" % STUDY_DIR, ":(exclude)AGENTS.md")
    dirty: Optional[Set[str]] = None
    if status and status[0] == 0:
        dirty = {t.decode("utf-8", "replace") for t in status[1].split(b"\0") if t}
    return {"git": True, "repo": True, "head": sha, "dirty": dirty}


def git_exclude_path(root: Path) -> Optional[Path]:
    """Location of .git/info/exclude for the repository containing root, or None outside Git."""
    info = git_info(root)
    if not info["repo"]:
        return None
    res = _git(root, "rev-parse", "--git-path", "info/exclude")
    if not res or res[0] != 0:
        return None
    raw = res[1].decode("utf-8", "replace").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else root / path


def exclude_has(path: Path, entry: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return entry in {line.strip() for line in text.splitlines()}


def ensure_exclude(path: Path, entry: str) -> bool:
    """Append entry to the exclude file, creating the file (and info/) if missing. True if written."""
    if exclude_has(path, entry):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_bytes() if path.exists() else b""
    chunk = (b"\n" if existing and not existing.endswith(b"\n") else b"") + (entry + "\n").encode("utf-8")
    with open(str(path), "ab") as fh:
        fh.write(chunk)
    return True


def run_kernel(root: Path, *args: str) -> "subprocess.CompletedProcess[bytes]":
    kernel = local_path(root, ".study/kernel.py")
    cmd = [sys.executable, "-I", str(kernel)] + list(args)
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=KERNEL_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        raise EmkitError("kernel timed out: %s" % " ".join(args))
    except OSError as exc:
        raise EmkitError("cannot run the installed kernel: %s" % exc)


def _tail(raw: bytes, limit: int = 600) -> str:
    text = raw.decode("utf-8", "replace").strip()
    return text if len(text) <= limit else "..." + text[-limit:]


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    raw = Path(args.path).expanduser()
    existed = raw.exists()
    root = resolve_target(args.path, create=not args.dry_run)
    if not existed and args.dry_run:
        root = Path(os.path.abspath(str(raw)))
    if existed or not args.dry_run:
        check_layout_safe(root)
    prefix = "would " if args.dry_run else ""

    report: List[str] = []
    problems: List[str] = []
    if not existed:
        report.append("%screate directory %s" % (prefix, root.as_posix()))

    dirs = [STUDY_DIR, STUDY_DIR + "/templates"] + ["%s/%s" % (STUDY_DIR, d) for d in ARTIFACT_DIRS]
    missing_dirs = [d for d in dirs if not local_path(root, d).is_dir()]
    for d in missing_dirs:
        report.append("%screate directory %s/" % (prefix, d))

    writes: List[Tuple[Managed, bytes]] = []
    for m in MANAGED:
        state = file_state(root, m)
        if state == "missing":
            report.append("%screate    %s" % (prefix, m.dest))
            writes.append((m, packaged_bytes(m)))
        elif state == "same":
            report.append("unchanged %s" % m.dest)
        elif args.force:
            report.append("%sreplace   %s" % (prefix, m.dest))
            writes.append((m, packaged_bytes(m)))
        else:
            report.append("skip      %s (differs from the packaged copy; existing file kept, use --force to replace)"
                          % m.dest)

    agents_write: Optional[bytes] = None
    if args.no_agents:
        report.append("skip      %s (--no-agents); the rules are in %s/AGENTS.md" % (AGENTS_FILE, STUDY_DIR))
    else:
        a_state, a_new = agents_plan(root)
        if a_state == "missing":
            report.append("%screate    %s" % (prefix, AGENTS_FILE))
            agents_write = a_new
        elif a_state == "absent":
            report.append("%sappend    %s (adds the Study block; your existing text is untouched)" % (prefix, AGENTS_FILE))
            agents_write = a_new
        elif a_state == "same":
            report.append("unchanged %s (Study block)" % AGENTS_FILE)
        elif a_state == "differs" and args.force:
            report.append("%sreplace   %s (Study block only)" % (prefix, AGENTS_FILE))
            agents_write = a_new
        elif a_state == "differs":
            report.append("skip      %s (Study block differs from the packaged one; use --force to refresh it)"
                          % AGENTS_FILE)
        elif a_state == "broken":
            report.append("skip      %s (unbalanced %s markers; left untouched even with --force, fix by hand)" % (AGENTS_FILE, BLOCK_BEGIN))
        else:
            report.append("skip      %s (not valid UTF-8; left untouched even with --force)" % AGENTS_FILE)

    codebases: List[str] = []  # absolute paths handed to the kernel
    for raw_cb in args.codebase:
        cb = Path(raw_cb).expanduser()
        cb = cb if cb.is_absolute() else root / cb  # relative paths are relative to PATH, the workspace
        cb_real = Path(os.path.realpath(str(cb)))
        if not (args.dry_run or cb_real.is_dir()):
            raise EmkitError("codebase directory not found: %s" % raw_cb)
        if not is_inside(cb_real, root) and cb_real != root:
            raise EmkitError("codebase %s is not inside %s" % (raw_cb, root.as_posix()))
        codebases.append(str(cb_real))
        report.append("%sregister  codebase %s" % (prefix, raw_cb.replace("\\", "/")))
    if args.detect:
        report.append("%sregister  every Git work tree found below %s (codebase scan)" % (prefix, root.as_posix()))

    exclude_file = git_exclude_path(root) if root.exists() else None
    if exclude_file is not None and not exclude_has(exclude_file, EXCLUDE_ENTRY):
        report.append("%s%s %s: %s" % (prefix, "append to" if exclude_file.exists() else "create",
                                         ".git/info/exclude" if exclude_file.name == "exclude" else exclude_file.name,
                                         EXCLUDE_ENTRY))

    if args.dry_run:
        for line in report:
            print(line)
        print("dry run: nothing written")
        return 0

    before = git_info(root)
    for d in missing_dirs:
        local_path(root, d).mkdir(parents=True, exist_ok=True)
    for m, data in writes:
        atomic_write(local_path(root, m.dest), data)
    if agents_write is not None:
        atomic_write(local_path(root, AGENTS_FILE), agents_write)
    if exclude_file is not None:
        try:
            ensure_exclude(exclude_file, EXCLUDE_ENTRY)
        except OSError as exc:
            problems.append("cannot update %s: %s" % (exclude_file, exc))
    for line in report:
        print(line)

    # Let the installed kernel finish the setup (index, git excludes) and prove it runs.
    init_run = run_kernel(root, "init", "--root", str(root))
    if init_run.returncode != 0:
        problems.append("kernel init failed: %s" % _tail(init_run.stderr or init_run.stdout))
    else:
        print("kernel init: ok")
        check_run = run_kernel(root, "check", "--root", str(root))
        if check_run.returncode != 0:
            problems.append("kernel check failed: %s" % _tail(check_run.stdout or check_run.stderr))
        else:
            print("kernel check: ok")
        if codebases or args.detect:
            reg_args = ["codebase", "add", "--root", str(root)] + codebases + (["--detected"] if args.detect else [])
            reg = run_kernel(root, *reg_args)
            sys.stdout.write(reg.stdout.decode("utf-8", errors="replace"))
            if reg.returncode != 0:
                problems.append("codebase registration failed: %s" % _tail(reg.stderr or reg.stdout))

    after = git_info(root)
    if before.get("dirty") is not None and after.get("dirty") is not None and before["dirty"] != after["dirty"]:
        problems.append("working-tree state changed outside %s/ and AGENTS.md during init" % STUDY_DIR)

    for m in STUDY_MANAGED:
        if not local_path(root, m.dest).is_file():
            problems.append("missing after install: %s" % m.dest)
    for d in dirs:
        if not local_path(root, d).is_dir():
            problems.append("missing directory after install: %s" % d)

    if problems:
        for p in problems:
            print("error: %s" % p, file=sys.stderr)
        print("installation incomplete", file=sys.stderr)
        return 1
    print("installed emkit %s into %s" % (__version__, root.as_posix()))
    print("")
    print("next steps:")
    print("  python %s/kernel.py --help                       list every command" % STUDY_DIR)
    print("  python %s/kernel.py orient                       survey the repository" % STUDY_DIR)
    print('  python %s/kernel.py run start --goal "..."      begin a study run' % STUDY_DIR)
    print("  read %s/PROTOCOL.md once; the rules for AI agents are in AGENTS.md and %s/AGENTS.md" % (STUDY_DIR, STUDY_DIR))
    if not codebases and not args.detect:
        print("  several repositories under this folder? python %s/kernel.py codebase scan" % STUDY_DIR)
    return 0


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

class Report:
    def __init__(self) -> None:
        self.failed = False

    def ok(self, text: str) -> None:
        print("[ok]   %s" % text)

    def warn(self, text: str) -> None:
        print("[warn] %s" % text)

    def fail(self, text: str) -> None:
        self.failed = True
        print("[FAIL] %s" % text)


def cmd_doctor(args: argparse.Namespace) -> int:
    r = Report()
    path = Path(args.path).expanduser()
    if sys.version_info[:2] >= MIN_PYTHON:
        r.ok("python %d.%d.%d (kit needs >= %d.%d)" % (sys.version_info[:3] + MIN_PYTHON))
    else:
        r.fail("python %d.%d is older than the required %d.%d" % (sys.version_info[:2] + MIN_PYTHON))
    if not path.is_dir():
        r.fail("target directory does not exist: %s" % path)
        return 1
    root = Path(os.path.realpath(str(path)))
    r.ok("target directory: %s" % root.as_posix())
    try:
        check_layout_safe(root)
    except EmkitError as exc:
        r.fail(str(exc))
        return 1

    study = root / STUDY_DIR
    if not study.is_dir():
        r.fail("%s/ not found (run: emkit init %s)" % (STUDY_DIR, args.path))
    kernel_ok = local_path(root, ".study/kernel.py").is_file()
    if kernel_ok:
        r.ok(".study/kernel.py present")
    elif study.is_dir():
        r.fail(".study/kernel.py is missing")

    version_file = local_path(root, ".study/VERSION")
    installed: Optional[str] = None
    if version_file.is_file():
        installed = version_file.read_text(encoding="utf-8", errors="replace").strip()
        if installed == __version__:
            r.ok("kit version %s (matches emkit %s)" % (installed, __version__))
        else:
            r.warn("kit version %s differs from emkit %s (no upgrade command; use init --force)"
                   % (installed or "?", __version__))
    elif study.is_dir():
        r.warn(".study/VERSION is missing; installed kit version unknown")

    if study.is_dir():
        for m in MANAGED:
            state = file_state(root, m)
            if state == "same":
                continue
            if state == "missing":
                (r.fail if m.dest != ".study/VERSION" else r.warn)("managed file missing: %s" % m.dest)
            elif m.dest == ".study/VERSION":
                continue  # reported above
            elif installed == __version__:
                r.fail("managed file does not match the packaged copy (corrupted or edited): %s" % m.dest)
            else:
                r.warn("managed file differs from emkit %s: %s" % (__version__, m.dest))
        for sub in ARTIFACT_DIRS:
            if not (study / sub).is_dir():
                r.warn("directory missing: %s/%s" % (STUDY_DIR, sub))
        a_state, _ = agents_plan(root)
        if a_state == "same":
            r.ok("AGENTS.md carries the current Study block")
        elif a_state == "missing":
            r.warn("no AGENTS.md: agents will not be told about the Study rules (emkit init adds them)")
        elif a_state == "absent":
            r.warn("AGENTS.md has no Study block: agents may not know the rules (emkit init appends it)")
        elif a_state == "differs":
            r.warn("AGENTS.md Study block differs from emkit %s (emkit init --force refreshes only that block)"
                   % __version__)
        else:
            r.warn("AGENTS.md cannot be managed (%s); add the rules from %s/AGENTS.md by hand" % (a_state, STUDY_DIR))
        cb_file = study / "codebases.json"
        if cb_file.is_file():
            try:
                names = [e["path"] for e in json.loads(cb_file.read_text(encoding="utf-8"))["codebases"]]
                r.ok("workspace mode: %d registered codebase(s): %s" % (len(names), ", ".join(names) or "none"))
            except (OSError, ValueError, KeyError, TypeError):
                r.fail("%s/codebases.json is unreadable or malformed" % STUDY_DIR)

        bad: List[str] = []
        for dest in TEMPLATE_DESTS:
            p = local_path(root, dest)
            if not p.is_file():
                continue
            try:
                lines = p.read_text(encoding="utf-8").split("\n")
            except (OSError, UnicodeDecodeError):
                bad.append(dest)
                continue
            if not lines or lines[0] != "---" or "---" not in lines[1:]:
                bad.append(dest)
        if bad:
            for dest in bad:
                r.fail("template unreadable or has no frontmatter: %s" % dest)
        elif all(local_path(root, d).is_file() for d in TEMPLATE_DESTS):
            r.ok("templates readable")

        if kernel_ok:
            try:
                helped = run_kernel(root, "--help")
                if helped.returncode == 0 and b"usage" in helped.stdout.lower():
                    r.ok("kernel.py --help runs")
                else:
                    r.fail("kernel.py --help failed: %s" % _tail(helped.stderr or helped.stdout))
                checked = run_kernel(root, "check", "--root", str(root))
                if checked.returncode == 0:
                    r.ok("kernel.py check passes")
                else:
                    r.warn("kernel.py check reports problems (study records, not the install): %s"
                           % _tail(checked.stdout or checked.stderr, 300))
            except EmkitError as exc:
                r.fail(str(exc))

    info = git_info(root)
    workspace = False
    try:
        workspace = bool(json.loads((study / "codebases.json").read_text(encoding="utf-8"))["codebases"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if workspace:
        if not info["git"]:
            r.warn("git not found: the per-codebase source-change guard is unavailable")
        else:
            r.ok("git found; repository state is tracked per registered codebase")
    elif not info["git"]:
        r.warn("git not found: anchor freshness and the run source-change guard are unavailable")
    elif not info["repo"]:
        r.warn("not a Git repository: anchor freshness and the run source-change guard are limited")
    else:
        r.ok("git repository, HEAD %s" % (info["head"] or "none (no commits yet)"))

    print("doctor: %s" % ("installation NOT usable" if r.failed else "installation usable"))
    return 1 if r.failed else 0


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="emkit",
        description="Install the Engineering Study Kit (.study/) into a repository. "
                    "After install, use: python .study/kernel.py --help",
        epilog="examples:\n  emkit init .\n  emkit init ~/code/app --dry-run\n  emkit doctor .",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version="emkit " + __version__)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    p = sub.add_parser("init", help="install .study/ (and a Study block in AGENTS.md) into a repository or workspace",
                       description="Copy the packaged kernel, protocol, schema and templates into PATH/.study/ "
                                   "and add a marked Study block to PATH/AGENTS.md (created if absent, "
                                   "appended to if it exists). Existing files are never overwritten unless "
                                   "--force, and --force only refreshes the marked block in AGENTS.md.")
    p.add_argument("path", nargs="?", default=".", metavar="PATH", help="repository directory (default: .)")
    p.add_argument("--force", action="store_true",
                   help="replace managed files that differ (never touches records, runs, the index or unknown files)")
    p.add_argument("--dry-run", action="store_true", help="print what would happen and write nothing")
    p.add_argument("--no-agents", dest="no_agents", action="store_true",
                   help="leave the root AGENTS.md alone (the rules are still installed as .study/AGENTS.md)")
    p.add_argument("--codebase", action="append", default=[], metavar="DIR",
                   help="register a repository below PATH as a codebase to study (repeatable; workspace mode)")
    p.add_argument("--detect", action="store_true",
                   help="register every Git work tree found below PATH (workspace mode)")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("doctor", help="check that an installation is usable",
                       description="Report whether PATH/.study/ is complete, readable and runnable.")
    p.add_argument("path", nargs="?", default=".", metavar="PATH", help="repository directory (default: .)")
    p.set_defaults(func=cmd_doctor)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    try:
        args = build_parser().parse_args(argv)
        return int(args.func(args) or 0)
    except EmkitError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 1
    except OSError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
