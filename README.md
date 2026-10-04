# Engineering Memory Kit

Portable, evidence-backed codebase exploration for humans and AI agents. The kit installs a self-contained `.study/` workspace into any repository: a standard-library Python kernel that records subsystem maps, execution flows, source-anchored evidence, claims and potential findings as Markdown and JSONL, with a disposable SQLite index for search. Study mode only: it never edits the target source.

## Quick start

Initialize the current repository:

```bash
uvx --from git+https://github.com/rafik-mikhail/ai-dev@v0.1.0 emkit init .
```

Then use the installed local kernel:

```bash
python .study/kernel.py --help
```

On macOS and Linux, use `python3` if `python` is not on your PATH.

## Installing

`uvx` runs the package in a temporary, isolated environment and discards it afterwards. `--from` accepts a Git URL with a branch, tag or commit.

| Goal | Command |
|---|---|
| Reproducible (recommended) | `uvx --from git+https://github.com/rafik-mikhail/ai-dev@v0.1.0 emkit init .` |
| Latest default branch (testing) | `uvx --from git+https://github.com/rafik-mikhail/ai-dev emkit init .` |
| A branch or commit | `uvx --from git+https://github.com/rafik-mikhail/ai-dev@BRANCH_OR_SHA emkit init .` |
| Persistent install | `uv tool install git+https://github.com/rafik-mikhail/ai-dev@v0.1.0`, then `emkit init .` |
| Another directory | `emkit init /path/to/repo` |

An unpinned URL follows the default branch, so two runs can install different kernels. Pin a tag when you want the same `.study/` everywhere.

Fallback without `uv` or network tooling: download the source ZIP from GitHub, unpack it, then either

```bash
python -m pip install ./ai-dev-main      # then: emkit init .
# or, with no installation at all:
PYTHONPATH=./ai-dev-main/src python -m emkit init .
```

(PowerShell: `$env:PYTHONPATH = ".\ai-dev-main\src"; python -m emkit init .`). The kernel alone can also be initialized with `python ai-dev-main/src/emkit/kernel.py init --root .`; that route does not write `.study/VERSION` or the root `AGENTS.md`.

## What `emkit` does

`emkit` has two commands. Everything else is done by the installed kernel.

```bash
emkit --version
emkit init [PATH] [--force] [--dry-run]
emkit doctor [PATH]
```

`emkit init` copies the packaged kernel, protocol, schema and templates into `PATH/.study/`, writes `.study/VERSION`, installs `AGENTS.md` at the repository root, creates the artifact directories, then runs the installed kernel's `init` and `check`. It prints every path it creates, replaces, skips or finds unchanged, and exits non-zero if the installation is incomplete.

```text
PATH/
├── AGENTS.md
└── .study/
    ├── kernel.py  PROTOCOL.md  schema.json  VERSION
    ├── templates/{system,flow,finding,run}.md
    ├── systems/  flows/  findings/  runs/  scratch/
    └── study.db           # disposable index, excluded from Git
```

- Nothing is overwritten by default. A managed file that exists and differs is skipped and reported. An existing `AGENTS.md` of your own is kept; merge the Study rules into it by hand.
- `--force` replaces managed files that differ, including a root `AGENTS.md`. It never touches records, runs, the index, or files it does not manage, and never deletes anything.
- `--dry-run` prints what would be created or replaced and writes nothing, not even a missing destination directory.
- Writes use a temporary file and an atomic rename. Symlinked destinations are refused.
- Re-running `init` on an existing installation is safe: unchanged files are left alone, the index is rebuilt from your records.
- There is no upgrade or merge command yet. To move to a newer kit, run a newer `emkit init --force` and review the diff in Git.
- In a Git repository, `init` adds `.study/` to `.git/info/exclude` (creating the file if it is missing, appending otherwise, never duplicating), so the workspace stays out of `git status` and out of commits. The file is local to your clone and is not shared. To version the records instead, delete that line. The kernel also appends its four narrower patterns for the index and scratch files. Source files are never touched, and `init` fails if the working tree changes.

`emkit doctor` reports Python compatibility, whether the directory and `.study/kernel.py` exist, the installed kit version, missing or differing managed files, readable templates, whether `kernel.py --help` runs, and Git availability and status (Git is optional). It does not judge your uncommitted changes; the no-source-change guarantee is enforced during `init` itself. It exits zero only when the installation is usable. A file that differs from the packaged copy is an error when the recorded kit version matches (corrupted or edited), and a warning when it comes from another version.

The installed `.study/kernel.py` is a byte-for-byte copy of the packaged kernel. It needs only Python 3.9+, never imports `emkit`, and keeps working after the `uvx` environment is gone.

## Using the kernel

```bash
python .study/kernel.py orient
python .study/kernel.py run start --goal "Map the login flow"              # RUN-0001
python .study/kernel.py new system auth --title "Authentication" --area src/auth --run RUN-0001
python .study/kernel.py anchor add SYS-auth src/auth/token.py --symbol verify_token --run RUN-0001
python .study/kernel.py evidence add ANC-0001 --type source-inspection --result "..." --run RUN-0001
python .study/kernel.py claim add SYS-auth "verify_token rejects empty tokens" --anchor ANC-0001 --evidence EV-0001 --run RUN-0001
python .study/kernel.py finding "Expiry not checked" --severity medium --anchor ANC-0001 --run RUN-0001
python .study/kernel.py set F-0001 --status triaged --run RUN-0001
python .study/kernel.py check
python .study/kernel.py search token expiry
python .study/kernel.py run end --id RUN-0001 --summary "Mapped auth entry points" --next "Trace refresh"
```

| Command | Purpose |
|---|---|
| `init [--force]` | Install or refresh `.study/` from the directory the kernel lives in (what `emkit init` runs in place) |
| `orient` | Read-only survey: languages, build tools, tests, entry-point candidates |
| `run start --goal` / `run end --id --summary` | Open a run with a source snapshot; close it, verify source unchanged, write the handoff |
| `new system\|flow SLUG --title --run` | Create a document from a template |
| `finding TITLE --severity --run` | Record a potential issue (never a fix) |
| `claim add DOC "text" --anchor/--evidence` | Append a numbered, grounded claim to a system or flow |
| `set ID --status/--confidence [--note]` | Change status or confidence; logged in the document's Status log |
| `anchor add DOC PATH` | Bind a document to source lines with a fingerprint and the Git SHA |
| `evidence add SUBJECT --type --result` | Append an evidence record to the run |
| `show`, `list`, `search`, `graph` | Read records, links, backlinks and evidence |
| `coverage` | Anchored vs unanchored files (descriptive only) |
| `check` | Validate records, anchors and index rebuildability |
| `rebuild` | Rebuild `study.db` from the durable records |
| `status` | Counts, open runs, anchor states, items needing attention |

Global options: `--root`, `--study-dir`, `--agent` (or `STUDY_AGENT`), `--json`, `--max-event-bytes`. `STUDY_DISABLE_FTS=1` forces the non-FTS search path. Errors print `error: ...` to stderr and exit 1. The operating rules are in `.study/PROTOCOL.md`; a walkthrough is in `examples/minimal/README.md`.

## How it stays honest

- Markdown and JSONL are the source of truth. `study.db` can be deleted and rebuilt at any time.
- Anchors prove provenance, not correctness. `ok` means the file, symbol text and fingerprint still match, not that a claim is true.
- Stale and missing anchors are warnings (exit 0). Malformed records, duplicate IDs, unknown links and invalid evidence are errors (exit 1).
- `verified` confidence is reserved and rejected in V0.
- `run end` compares Git HEAD and working-tree state with the run's start snapshot and marks the run `source_changed` if they differ.

## Limits

- `uvx` provides package isolation, not a read-only sandbox for agents. It keeps `emkit` and its environment away from your project; it does nothing to stop an agent with shell access from editing source. The kernel's guardrails constrain `kernel.py` only, and `AGENTS.md` is a request, not enforcement. For real isolation, mount the source read-only, keep `.study/` writable elsewhere, disable the network, and omit credentials (see Future hardening in `.study/PROTOCOL.md`).
- Symbol lookup is a text heuristic, not a parser. Without a line range, a symbol anchor covers only its first matching line.
- Without Git there is no source-change guard, and freshness relies on fingerprints alone.
- `orient` is filename and layout heuristics; the entry points it lists are candidates.
- Concurrent writers are not coordinated beyond exclusive file creation; use one active run per writer.
- No Change or Verify modes.

## Development

Layout: `src/emkit/` holds the package (`cli.py` installer, `kernel.py`, and `resources/` with the protocol, agent rules, schema and templates). Each resource has exactly one canonical copy there. `examples/` is not packaged.

```bash
python -m unittest discover -s tests -v
```

The suite uses `unittest` only. Packaging tests build a wheel (needs `setuptools>=69`, from an index or already installed) and install it into a temporary virtualenv. The `uvx` tests need `uvx` and `git` and are skipped without them. Tests that need Git skip cleanly when it is missing.

Release: bump the version in both `pyproject.toml` and `src/emkit/__init__.py` (a test checks they match), then tag it.

```bash
git tag v0.1.0
git push origin v0.1.0
```
