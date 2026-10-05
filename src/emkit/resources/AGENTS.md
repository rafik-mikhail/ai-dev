## Engineering Study Kit (Study mode)

This project has a study workspace in `.study/`. When asked to study, explore or map code, you inspect and record. You do not change code. Read `.study/PROTOCOL.md` once for the full rules, and run `python .study/kernel.py commands` once for the syntax of every command; do not probe commands one by one with `--help`.

1. Study mode only. No implementation, refactoring or fixes. Never modify source, tests, configuration, Git state or hooks.
2. Run `python .study/kernel.py status`, then `orient` on an unfamiliar codebase, then open a run: `run start --goal "..."`. Search existing records before reading broadly: `search`, `list`, `show ID`.
3. Write records only through `kernel.py` where a command exists (`claim add`, `set`, `finding`). Back every technical claim with an anchor and evidence (`anchor add`, `evidence add`) and cite `ANC-` and `EV-` IDs.
4. Keep observed behavior, inference, hypothesis and uninspected scope separate. Prefix inference with `Inference:`.
5. Findings go out of date. When `check` or `status` reports `finding-needs-recheck`, re-read the source, record `evidence add F-NNNN ...`, then `set F-NNNN --status resolved|obsolete|dismissed --note "..." --evidence EV-NNNN`, or leave it open. Never close a finding from memory or a commit message.
6. Finish with `run end --id RUN-NNNN --summary "..." --next "..."`. If it reports `source_changed`, say so; do not call the run successful.
7. Several repositories under one study root: list them with `codebase list`, register one with `codebase add PATH`, find candidates with `codebase scan`. Anchors must fall inside a registered codebase; paths stay relative to the study root (for example `api/src/auth.py`).
8. Tool calls. If your harness gives you `study_*` tools (`study_finding`, `study_set`, `study_claim_add`, and so on), call them instead of the shell. Arguments are JSON with the same names as the command options, and each result is `{ok, exit_code, output, error}`. Without such tools, use the shell. To run one tool from the shell, or to register them in a harness, use `tools call study_NAME --args '{...}'` and `tools list`.
9. The kernel's guardrails do not replace an external sandbox. Do not use shell access to work around them.
