# Engineering Study Kit V0: Protocol

Study mode only. This file is the operating contract for humans and agents using `.study/kernel.py`. Read it once, then run `status` and start an explicit run.

## 1. Purpose and non-goals

Purpose: inspect an existing repository, map subsystems, trace execution flows, record source-grounded claims and potential findings, and hand work off between sessions, without modifying the target source.

Non-goals in V0: source changes or fixes, Change or Verify modes, git worktrees, multiple agent personas, model or provider integration, embeddings, language-server or tree-sitter integration, containers or network isolation, signatures or attestations, remote services, and any automatic promotion to `verified`.

## 2. Authority and storage model

| Thing | Authority | Notes |
|---|---|---|
| Target source and Git | Code state | Never edited by the kernel. |
| Markdown artifacts | Reviewed study knowledge | `systems/`, `flows/`, `findings/`, `runs/RUN-NNNN/summary.md`. |
| `evidence.jsonl` | Observations about a revision and environment | Append-only, per run. |
| `events.jsonl` | Cold diagnostic history | Not engineering truth. Not searched. Read only with `show RUN-NNNN --events --limit N`. |
| `study.db` | Disposable index | Deleting it loses nothing. Rebuild with `rebuild`. |

Layout installed by `init`:

```text
.study/
  kernel.py  PROTOCOL.md  schema.json  templates/{system,flow,finding,run}.md
  systems/  flows/  findings/
  runs/RUN-NNNN/{summary.md, evidence.jsonl, events.jsonl}
  study.db  (+ -wal/-shm)   disposable, excluded from Git
  scratch/                  disposable, excluded from Git
```

Install with `emkit init PATH` (preferred; also writes `.study/VERSION` and the root `AGENTS.md`) or with the packaged `kernel.py init --root PATH`. Either way `.study/kernel.py` is a standalone copy: it needs only Python and never imports the `emkit` package.

`emkit init` adds `.study/` to `.git/info/exclude`, so by default the workspace stays out of Git. To keep the knowledge in version control, delete that line and commit `systems/`, `flows/`, `findings/`, `runs/`, the templates and the kit files. The kernel's own `init` appends four narrower patterns to the same file (`.study/study.db`, `-wal`, `-shm`, `.study/scratch/`). Those appends, and the marked Study block that `emkit init` adds to the root `AGENTS.md`, are the only writes the tools make outside `.study/`.

## 3. Artifacts and IDs

| Artifact | ID | Where it lives |
|---|---|---|
| Run | `RUN-NNNN` | `runs/RUN-NNNN/summary.md`: bounded investigation and handoff |
| System | `SYS-<slug>` | `systems/<slug>.md`: purpose, boundaries, interfaces, invariants |
| Flow | `FLOW-<slug>` | `flows/<slug>.md`: end-to-end execution trace |
| Finding | `F-NNNN` | `findings/F-NNNN.md`: potential defect or risk |
| Claim | `CLM-NNNN` | A list line under `## Claims` in a System or Flow |
| Anchor | `ANC-NNNN` | A JSON line inside the `study:anchors` comment block of a document |
| Evidence | `EV-NNNN` | A line in a run's `evidence.jsonl` |

Slugs are lowercase letters, digits and single hyphens, up to 64 characters. Numbered IDs are never reused; the kernel scans existing files for the highest number.

Frontmatter is a small YAML-like subset: `key: value` scalars and inline lists such as `anchors: [ANC-0001]`. No nested maps, no multiline values. The kernel parses it; no YAML library is needed.

### Claims

A claim is a one-line, numbered statement in a System or Flow, grounded in anchors and/or evidence:

```bash
python .study/kernel.py claim add SYS-auth "verify_token rejects empty tokens" \
    --anchor ANC-0001 --evidence EV-0001 --run RUN-0001
```

This allocates the next `CLM-NNNN` (unique across the study) and appends a line under `## Claims`:

```text
- CLM-0001: verify_token rejects empty tokens (ANC-0001, EV-0001)
```

Rules: at least one `--anchor` or `--evidence` is required, and every ID must already exist. Something you cannot ground yet belongs in `## Open questions` or in a finding. Add `--inference` to prefix the statement with `Inference:`. Claims are indexed, searchable, and can be named as an evidence subject (`evidence add CLM-0001 ...`). `check` reports duplicate claim IDs. A claim line is a record, so do not edit its ID; to retract one, say so in the document body. Hand-written claim lines still parse, but do not put example claim lines in prose outside HTML comments.

### Status and confidence changes

Findings start `open`. Change a document with `set`:

```bash
python .study/kernel.py set F-0001 --status triaged --run RUN-0001
python .study/kernel.py set F-0001 --status dismissed --note "intended; see ADR-7" --run RUN-0001
python .study/kernel.py set SYS-auth --status reviewed --confidence observed --run RUN-0001
```

| Kind | Allowed `--status` |
|---|---|
| finding | `open`, `triaged`, `dismissed` |
| system, flow | `draft`, `reviewed`, `deprecated` |

`dismissed` and `deprecated` require `--note`. `--confidence` accepts only `hypothesis`, `inferred` or `observed`; the reserved levels are refused. Every change is appended to a `## Status log` section in the document (timestamp, agent, run, old and new value, note), so the history is durable Markdown, not just cold events. Runs cannot be changed with `set`; their status is managed by `run start` and `run end`. `fixed` is deliberately not a V0 status, because V0 never changes source.

## 4. Confidence vocabulary

```text
hypothesis < inferred < observed < corroborated < executed < verified
```

V0 commands create records only at `hypothesis`, `inferred` or `observed`. `finding` sets `observed` when anchors are supplied, otherwise `hypothesis`. You may edit `confidence` in a System, Flow or Finding up to `observed`. `corroborated` and `executed` are allowed vocabulary, but nothing in V0 earns them automatically. `verified` is reserved for a future independent-verification extension, and `check` reports it as an error. Agent-authored or retrieved text is never automatically verified.

## 5. Anchors and freshness

An anchor binds a document to source text: repository-relative path, optional symbol, a line span, a SHA-256 fingerprint of those lines, and the Git HEAD at creation (null when unavailable).

Mechanical states reported by `check`, `show` and `status`:

| State | Meaning |
|---|---|
| `ok` | File exists, symbol text found, lines in bounds, fingerprint matches, file not dirty in the working tree |
| `stale` | Lines out of bounds, fingerprint differs, or the file has uncommitted changes |
| `missing_file` | Path no longer exists |
| `missing_symbol` | Symbol text no longer appears in the file |
| `unchecked` | Path unsafe or unreadable |

Limits you must keep in mind:

- An anchor proves provenance, not correctness. `ok` never means the claim is true or understood.
- Symbol lookup is a conservative text heuristic, not a parser. With no line range, the first plausible definition line is used, giving a one-line span. Ambiguity is reported as a warning. Pass `--start-line` and `--end-line` for a meaningful span.
- Stale and missing anchors are warnings, so `check` still exits 0. Structural problems are errors and exit 1.
- Without Git, freshness rests on fingerprints alone, and `check` says so.
- Moving code without keeping its location and fingerprint is reported `stale`, never `ok`.

### Several codebases under one study root

When the study root is a folder that holds several repositories, register the ones in scope: `codebase scan` finds candidates, `codebase add PATH` registers one, `codebase remove PATH` unregisters it (records stay), `codebase list` shows each one's HEAD and working-tree state. The registry is `.study/codebases.json`. With nothing registered, the root is the single codebase and nothing below applies.

- An anchor must fall inside a registered codebase. Its `path` stays relative to the study root, its `repository` field names the codebase, and its `commit` is that repository's HEAD.
- Freshness is judged against the anchor's own repository, and `run start` / `run end` snapshot every registered codebase separately. A change in any one marks the run `source_changed` and names it.
- Unregistered folders are outside the study: they cannot be anchored and are ignored by `orient` and `coverage`.
- The map cannot change while a run is open. Removing a codebase keeps its anchors, and `check` warns about anchors whose codebase is not registered.
- Evidence takes its commit from the single codebase its anchors belong to; it is null when it cites anchors from several, or none.

## 6. Study workflow

1. Orient: `status`, then `list` and `search WORD ...` to reuse existing records before reading the code broadly. On a repository you have not seen, run `orient` first (section 10).
2. Start: `run start --goal "..."`. Record the printed `RUN-NNNN`. A run snapshots Git HEAD and working-tree state.
3. Map: `new system SLUG --title ... --area PATH --run RUN-NNNN`.
4. Trace: `new flow SLUG --title ... --run RUN-NNNN`.
5. Investigate and anchor: `anchor add DOC_ID PATH [--symbol NAME] [--start-line N --end-line N] --run RUN-NNNN`. Then edit the document body to describe what you saw, citing `ANC-` and `EV-` IDs.
6. Record: `evidence add SUBJECT --type TYPE --result TEXT [--anchor ANC-NNNN] [--limitation TEXT] [--command TEXT] [--exit-code N] --run RUN-NNNN`. Suspicious behavior becomes `finding TITLE --severity LEVEL [--anchor ...] --run RUN-NNNN`. Never apply a fix.
7. Check: `check`. Resolve errors; review warnings.
8. Hand off: `run end --id RUN-NNNN --summary TEXT [--next TEXT] [--open-question TEXT ...] [--covered DOC_ID ...]`.

Evidence types: `source-inspection`, `configuration-inspection`, `test-run`, `static-analysis`, `runtime-observation`, `manual-reproduction`. The `--command` text is recorded only; the kernel never runs it. If you ran a command yourself, record it and its exit code, and say what you did not inspect in `--limitation`.

Hand-editing is allowed in Markdown bodies for the prose sections the templates mark as agent-written (Purpose, Steps, Conclusions, Limitations, Not inspected, and so on). Use the commands for everything that has one: claims (`claim add`), status and confidence (`set`), anchors, evidence. Do not hand-edit frontmatter IDs, claim IDs, the `study:anchors` block, the Status log, `evidence.jsonl`, or `events.jsonl`; `check` will flag malformed records.

Writing rules for technical text:

- Label inference with `Inference:`.
- Reference stable `ANC-` and `EV-` IDs for technical claims.
- Never infer behavior from file or symbol names alone.
- State what was not inspected. Every template has a `Not inspected` section; do not leave it implicit.

## 7. Read-only rules

Kernel-level guarantees:

- All writes go through one filesystem layer that canonicalizes each path and refuses anything outside the study directory, including through symlinks.
- Document slugs, anchor paths and areas reject absolute paths, backslashes, NUL bytes and `..`. Anchors cannot point into `.git` or `.study`.
- No command edits source, runs a shell command, or accepts a command to execute. `orient` reads file names, directory layout and, for `package.json`, a few keys; it writes nothing and works before `init`. Git is invoked only for `rev-parse` and `status`, with optional locks disabled.
- `run start` snapshots Git HEAD and porcelain status (study directory excluded). `run end` compares. If anything differs, the run is marked `source_changed`, the command exits non-zero, and the diff (state only, no file contents) is printed. Pre-existing dirty state that is unchanged at the end is tolerated.
- Without Git there is no source-change guard; the kernel says so.

The agent rules are stricter than the kernel's: do not modify target source, tests, configuration, Git state or hooks, and write study records only through `kernel.py` wherever a command exists. These kernel guardrails constrain `kernel.py`. They do not constrain an agent that also has a general shell or filesystem tool. Real enforcement needs the isolation described in section 11.

## 8. Logging and privacy

- `events.jsonl` records each kernel action (run start/end, artifact creation, anchors, evidence, duplicates skipped) with timestamp, agent and short detail. Strings are truncated to 16384 bytes by default (`--max-event-bytes`, `STUDY_EVENT_MAX_BYTES`).
- Events never contain source file contents. Anchors store a hash and line numbers, not the text. Evidence `result` and `limitations` are whatever you type, so do not paste secrets or credentials into them.
- The source-change diff prints Git porcelain lines (paths and status codes) only.
- Events are cold data. `search` ignores them, and `show` reads them only on request and bounded by `--limit` (default 50, maximum 1000).
- If the study directory is committed or shared, review `runs/*/evidence.jsonl` and summaries first.

## 9. Index rebuildability

`study.db` is derived state: documents, anchors, links, runs, evidence, event metadata, and an FTS5 table when SQLite has it (otherwise search uses `LIKE`). Rebuild runs in a single transaction. A parse error, duplicate ID or injected failure leaves the previous database usable. `check` also builds the index in memory to prove it can be rebuilt. If deleting `study.db` would lose knowledge, that is a bug.

Search matches document titles and bodies with AND semantics across words. Body text excludes HTML comments, so template instructions are not indexed. It does not search events.

## 10. Commands, exit codes and output

```text
init  orient  run start|end  new system|flow  finding  claim add  set
anchor add  evidence add  show  list  search  graph  coverage  check  rebuild  status
```

`orient` surveys the repository: file count, languages by extension, build tools by marker file, test directories and test-named files, entry-point candidates by filename convention (plus `main`/`bin`/`scripts` keys from a root `package.json`), root docs, CI files, and top-level directory sizes, alongside the current study counts. It excludes the same directories as `coverage`. Its output is filename and layout heuristics: candidates to investigate, never evidence that code does anything. Anchor what you rely on.

Global options: `--root PATH`, `--study-dir PATH`, `--agent NAME` (or `STUDY_AGENT`), `--json`, `--max-event-bytes N`. Set `STUDY_DISABLE_FTS=1` to force the fallback search path. Errors go to stderr as `error: ...` with exit 1. With `--json`, stdout carries only JSON; warnings go to stderr. `coverage` is descriptive: anchored does not mean understood or correct. `init` refuses to overwrite existing kit files without `--force`, and never overwrites durable records.

## 11. Future hardening

Strong isolation belongs outside the kernel. A harness should mount the target source read-only, give the study directory a separate writable location, provide a disposable scratch directory, disable network access, omit remote credentials and secrets, and run each session in a throwaway sandbox. V0 implements none of this; it only makes the in-kernel guarantees above.

## 12. Future extensions (not in V0)

Change records (`CHG-*`) with isolated git worktrees and candidate commits, an independent verifier identity with commit-bound validation evidence (the only path to `verified`), SCIP or language-server symbol identities, OpenTelemetry export, and signed in-toto-style evidence. The artifact model and schema leave room for these without a redesign.
