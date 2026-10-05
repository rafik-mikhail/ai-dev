# Engineering Memory Kit

Portable, evidence-backed codebase exploration for humans and AI agents. The kit installs a self-contained `.study/` workspace into any repository: a standard-library Python kernel that records subsystem maps, execution flows, source-anchored evidence, claims and potential findings as Markdown and JSONL, with a disposable SQLite index for search. Study mode only: it never edits the target source. The one tracked file it can touch is `AGENTS.md`, by adding a marked block, and you can opt out.

## Quick start

Initialize the current repository:

```bash
uvx --from git+https://github.com/rafik-mikhail/ai-dev@v0.1.0 emkit init .
```

Then use the installed local kernel:

```bash
python .study/kernel.py --help       # ends with every command and its arguments
python .study/kernel.py commands     # the same table on its own
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
emkit init [PATH] [--force] [--dry-run] [--no-agents] [--no-skills] [--codebase DIR ...] [--detect]
emkit doctor [PATH]
```

`emkit init` copies the packaged kernel, protocol, schema, agent rules and templates into `PATH/.study/`, writes `.study/VERSION`, adds the Study rules to `AGENTS.md` at the root of `PATH` and installs an agent skill (see below), creates the artifact directories, then runs the installed kernel's `init` and `check`. It prints every path it creates, replaces, skips or finds unchanged, and exits non-zero if the installation is incomplete.

```text
PATH/
├── AGENTS.md              # created, or a marked Study block appended; see below
├── .claude/skills/engineering-study/SKILL.md
├── .agents/skills/engineering-study/SKILL.md
└── .study/
    ├── kernel.py  PROTOCOL.md  schema.json  AGENTS.md  VERSION
    ├── templates/{system,flow,finding,run}.md
    ├── systems/  flows/  findings/  runs/  scratch/
    └── study.db           # disposable index, excluded from Git
```

- Nothing is overwritten by default. A managed file that exists and differs is skipped and reported.
- `--force` replaces managed files that differ. It never touches records, runs, the index, the codebase registry, or files it does not manage, and never deletes anything.
- `--dry-run` prints what would be created or replaced and writes nothing, not even a missing destination directory.
- Writes use a temporary file and an atomic rename. Symlinked destinations are refused.
- Re-running `init` on an existing installation is safe: unchanged files are left alone, the index is rebuilt from your records.
- There is no upgrade or merge command yet. To move to a newer kit, run a newer `emkit init --force` and review the diff in Git.
- In a Git repository, `init` adds `.study/` and the two skill folders to `.git/info/exclude` (creating the file if it is missing, appending otherwise, never duplicating), so the workspace stays out of `git status` and out of commits. The file is local to your clone and is not shared. To version the records or the skill instead, delete the matching line. The kernel also appends its four narrower patterns for the index and scratch files. Source files are never touched, and `init` fails if the working tree changes.

### The root AGENTS.md

The root `AGENTS.md` belongs to the repository, not to the kit. `emkit` only ever manages one block inside it, wrapped in `<!-- emkit:begin -->` and `<!-- emkit:end -->` markers:

| Situation | What `init` does |
|---|---|
| No `AGENTS.md` | Creates one containing the block |
| `AGENTS.md` exists, no block | Appends the block after your text. Nothing above it changes; CRLF files stay CRLF |
| Block present and current | Leaves it (`unchanged`) |
| Block present but different | Skips and reports; `--force` rewrites only the text between the markers |
| Unbalanced markers, or not UTF-8 | Skips and reports; never modified, not even with `--force` |
| `--no-agents` | Leaves the file alone; the same rules are always installed as `.study/AGENTS.md` |

The block is a short rule summary that points to `.study/PROTOCOL.md`. Whenever a file is kept, `init` says `skip` and what to do about it: managed files and a differing block say to use `--force`; a file it cannot safely edit says to fix it by hand. Agents read the nearest `AGENTS.md` in the directory tree and the closest one wins, so a repository's own file is never displaced by a workspace-level one. Appending changes a tracked file, which shows up in `git diff`; use `--no-agents` when you do not want that, for example in a repository you do not own.

### The agent skill

Agents that support the open [Agent Skills](https://agentskills.io/specification) format (a folder with a `SKILL.md` that has `name` and `description` frontmatter) load the skill when a study task starts. `init` writes the same file to `.claude/skills/engineering-study/` (Claude) and `.agents/skills/engineering-study/` (Codex and other tools that scan `.agents/skills`). It is generated at install time from one packaged template plus the same Study block that goes into `AGENTS.md`, so there is no second copy of the rules to keep in sync.

- It tells the agent to run `python .study/kernel.py commands` once for the syntax of every command, instead of probing each command with `--help`, and how to use typed `study_*` tools when the harness provides them.
- It follows the same rules as every managed file: created if missing, left alone if identical, kept with a `skip ... use --force` line if it differs, replaced only with `--force`. Other skills in those folders are never touched, and a symlinked parent that leaves the repository is refused.
- `--no-skills` leaves it out. `emkit doctor` reports whether the skill is current (a warning, not a failure, when it is missing or different).

### One folder, several repositories (workspace mode)

Install into the folder that contains your repositories and tell the kit which ones to study:

```bash
cd ~/work                                   # contains api/, web/, notes/
uvx --from git+https://github.com/rafik-mikhail/ai-dev@v0.1.0 emkit init . --detect
python .study/kernel.py codebase list
```

`--detect` registers every Git work tree found up to three levels down. `--codebase api --codebase web` (relative to `PATH`) registers specific ones. Later, from the kernel:

```bash
python .study/kernel.py codebase scan           # list candidates (read-only)
python .study/kernel.py codebase add web         # start studying a repository
python .study/kernel.py codebase remove web      # stop; nothing is deleted
python .study/kernel.py codebase list            # HEAD and working-tree state per codebase
```

The registry is `.study/codebases.json`. With no codebases registered the kit behaves exactly as in a single repository. Once any is registered:

- Anchors must fall inside a registered codebase. Paths stay relative to the study root (`api/src/auth.py`), and each anchor records its codebase in `repository` and that repository's own HEAD in `commit`.
- Freshness and the source-change guard are checked per codebase. Editing a file in `web/` makes only `web/` anchors stale, and `run end` names the codebase that changed. Folders that are not registered (such as `notes/`) are ignored.
- `status`, `orient`, `coverage` and `check` report and cover registered codebases only.
- Removing a codebase unregisters it. Its anchors and records stay, and `check` warns that they belong to an unregistered codebase. A registered folder that disappears is also a warning, not an error.
- The map cannot change while a run is open, because the guard compares the same set of repositories at start and end.

`emkit doctor` reports Python compatibility, whether the directory and `.study/kernel.py` exist, the installed kit version, missing or differing managed files, readable templates, whether `AGENTS.md` carries the current Study block, whether the agent skill is current, the registered codebases, whether `kernel.py --help` runs, and Git availability and status (Git is optional). It does not judge your uncommitted changes; the no-source-change guarantee is enforced during `init` itself. It exits zero only when the installation is usable. A file that differs from the packaged copy is an error when the recorded kit version matches (corrupted or edited), and a warning when it comes from another version.

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
| `set ID --status/--confidence [--note] [--evidence]` | Change status or confidence; logged in the document's Status log. Findings close as `resolved`, `obsolete` or `dismissed` |
| `anchor add DOC PATH` | Bind a document to source lines with a fingerprint and the Git SHA of its codebase |
| `codebase add\|remove\|list\|scan` | Choose which repositories under the study root are studied (workspace mode) |
| `evidence add SUBJECT --type --result` | Append an evidence record to the run |
| `show`, `list`, `search`, `graph` | Read records, links, backlinks and evidence |
| `coverage` | Anchored vs unanchored files (descriptive only) |
| `check` | Validate records, anchors and index rebuildability |
| `rebuild` | Rebuild `study.db` from the durable records |
| `status` | Counts, open runs, anchor states, items needing attention |
| `commands` | Every command with its arguments, generated from the kernel's own parser |
| `tools list\|call` | Export the commands as agent tool definitions; run one from JSON arguments (see below) |

Global options: `--root`, `--study-dir`, `--agent` (or `STUDY_AGENT`), `--json`, `--max-event-bytes`. `STUDY_DISABLE_FTS=1` forces the non-FTS search path. Errors print `error: ...` to stderr and exit 1. The operating rules are in `.study/PROTOCOL.md`; a walkthrough is in `examples/minimal/README.md`.

### As agent tools

Agents with a shell run the commands above (a wrong call prints the correct usage under the error). For a harness that wants typed tool calls instead (Claude, OpenAI function calling, MCP wrappers, your own loop), the kernel exports its commands as tool definitions and can run one from JSON, with no shell involved:

```bash
python .study/kernel.py tools list                    # JSON array: name, description, input_schema
python .study/kernel.py tools list --format openai    # {"type": "function", "function": {...}}
python .study/kernel.py tools call study_finding --args '{"title": "Expiry not checked", "severity": "medium", "anchor": ["ANC-0001"], "run": "RUN-0001"}'
echo '{"words": ["token"]}' | python .study/kernel.py tools call study_search --args -
```

- There is one tool per command, named `study_` plus the command path: `study_run_start`, `study_finding`, `study_set`, `study_claim_add`, `study_codebase_add`, and so on, 22 in all. They are generated from the kernel's own argument parser, so they cannot drift from the CLI. `init` and `tools` are not offered.
- Argument names are the long option names with underscores (`start_line`, `open_question`). Options that repeat take a JSON array. Positional arguments, such as `document_id` and `text`, are plain properties.
- `tools call` validates the arguments against the schema, runs the command with `--json`, and always prints one envelope: `{"ok": true, "exit_code": 0, "output": ..., "error": ""}`. `output` is the command's JSON, or its text when the command has no JSON form. Validation failures and unknown tools use the same envelope with `ok: false`. Exit codes mirror the command's.
- Values are passed as separate arguments, never through a shell, so quotes, newlines and leading dashes in titles and claims are safe. `--root`, `--study-dir`, `--agent` and `--max-event-bytes` given to `tools call` are passed through; they are not tool arguments, so the model cannot redirect the kernel.
- Giving an agent only these tools, with no general shell, is a way to let it record study notes without being able to edit source. This narrows what the agent can do, but it is not a sandbox: the harness has to enforce that the agent has no other way to touch the files.

## Methodology

The kit supports one discipline, Study mode: understand a codebase and record what you learned without changing it. The loop is:

1. Orient: survey the layout (`orient`), then search existing records before reading broadly.
2. Open a run (`run start --goal ...`). The kernel snapshots source state so it can prove nothing changed.
3. Map systems and flows (`new system|flow`), and bind every technical statement to source with an anchor and evidence.
4. Record claims with an explicit confidence (hypothesis, inferred, observed). Keep observed behavior, inference and uninspected scope separate.
5. Log suspicious behavior as findings. Never apply a fix.
6. End the run with a handoff summary and next steps, so the next session or agent resumes where this one stopped.

The full rules, vocabulary and record formats are in [PROTOCOL.md](src/emkit/resources/PROTOCOL.md), which `emkit init` installs as `.study/PROTOCOL.md`.

## How it stays honest

- Markdown and JSONL are the source of truth. `study.db` can be deleted and rebuilt at any time.
- Anchors prove provenance, not correctness. `ok` means the file, symbol text and fingerprint still match, not that a claim is true.
- Stale and missing anchors are warnings (exit 0). Malformed records, duplicate IDs, unknown links and invalid evidence are errors (exit 1).
- Records, templates and logs checked out with CRLF line endings, or saved with a BOM, are read normally. The kernel writes LF.
- `verified` confidence is reserved and rejected in V0.
- Findings are closed by observation, never by inference. `resolved` (the problem is gone), `obsolete` (the code was removed or rewritten) and `dismissed` (not a problem) all need a note, and the first two need evidence from a re-inspection in the same run; the log records the commit the source was at. When a finding's anchors drift, `check` and `status` flag it as `finding-needs-recheck`. The kernel never closes a finding by itself, and there is no `fixed` status because Study mode cannot know who fixed what.
- `run end` compares Git HEAD and working-tree state with the run's start snapshot and marks the run `source_changed` if they differ.

## Limits

- `uvx` provides package isolation, not a read-only sandbox for agents. It keeps `emkit` and its environment away from your project; it does nothing to stop an agent with shell access from editing source. The kernel's guardrails constrain `kernel.py` only, and `AGENTS.md` is a request, not enforcement. For real isolation, mount the source read-only, keep `.study/` writable elsewhere, disable the network, and omit credentials (see Future hardening in `.study/PROTOCOL.md`).
- Symbol lookup is a text heuristic, not a parser. Without a line range, a symbol anchor covers only its first matching line.
- Without Git there is no source-change guard, and freshness relies on fingerprints alone. In workspace mode a codebase registered with `--no-git` has the same limit.
- Workspace mode checks each registered repository, not Git submodules or worktrees inside it, and an evidence record that cites anchors from several codebases stores a null commit.
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
