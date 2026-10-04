# AGENTS.md: Study mode

This repository uses the Engineering Study Kit V0 in `.study/`. Study mode only: you inspect and record. You do not change code.

Installed by `emkit init`. If this repository already had an `AGENTS.md`, merge these rules into it.

## Rules

1. Study mode only. No implementation, refactoring or fixes.
2. Read `.study/PROTOCOL.md` once. Then run `python .study/kernel.py status` and start an explicit run: `python .study/kernel.py run start --goal "..."`.
3. On an unfamiliar repository, run `orient` first. Search existing artifacts before reading broadly: `search WORD ...`, `list`, `show ID`. Treat `orient` output as candidates, not facts.
4. Never modify target source, tests, configuration, Git state or hooks.
5. Write study records only through `kernel.py` where a command exists: `claim add` for claims, `set` for status and confidence. Edit Markdown bodies only in the prose sections the templates mark as agent-written.
6. Back every technical claim with an anchor (`anchor add`) and evidence (`evidence add`). Cite `ANC-` and `EV-` IDs.
7. Keep observed behavior, inference, hypothesis and uninspected scope separate. Prefix inference with `Inference:`. Fill in every `Not inspected` section.
8. Record suspicious behavior with `finding`. Do not apply a fix.
9. Finish with `run end --id RUN-NNNN --summary "..." --next "..."` so the next session can resume. If it reports `source_changed`, say so; do not call the run successful.
10. The kernel's guardrails do not replace an external sandbox. Do not use shell access to work around them.

## Commands

```text
python .study/kernel.py status
python .study/kernel.py orient
python .study/kernel.py run start --goal "..."
python .study/kernel.py new system|flow SLUG --title "..." [--area PATH] --run RUN-NNNN
python .study/kernel.py anchor add DOC_ID PATH [--symbol NAME] [--start-line N --end-line N] --run RUN-NNNN
python .study/kernel.py evidence add SUBJECT --type TYPE --result "..." [--anchor ANC-NNNN] [--limitation "..."] --run RUN-NNNN
python .study/kernel.py claim add DOC_ID "statement" --anchor ANC-NNNN [--evidence EV-NNNN] [--inference] --run RUN-NNNN
python .study/kernel.py set ID [--status S] [--confidence C] [--note "..."] --run RUN-NNNN
python .study/kernel.py finding "TITLE" --severity low|medium|high|critical [--anchor ANC-NNNN] --run RUN-NNNN
python .study/kernel.py check
python .study/kernel.py run end --id RUN-NNNN --summary "..."
```

Also available: `show`, `list`, `search`, `graph`, `coverage`, `rebuild`. Add `--agent NAME` (or set `STUDY_AGENT`) so records name their author. Add `--json` for machine-readable output.
