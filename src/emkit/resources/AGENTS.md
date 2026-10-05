## Engineering Study Kit (Study mode)

This project has a study workspace in `.study/`. When asked to study, explore or map code, you inspect and record. You do not change code. Read `.study/PROTOCOL.md` once for the full rules; run `python .study/kernel.py --help` for commands.

1. Study mode only. No implementation, refactoring or fixes. Never modify source, tests, configuration, Git state or hooks.
2. Run `python .study/kernel.py status`, then `orient` on an unfamiliar codebase, then open a run: `run start --goal "..."`. Search existing records before reading broadly: `search`, `list`, `show ID`.
3. Write records only through `kernel.py` where a command exists (`claim add`, `set`, `finding`). Back every technical claim with an anchor and evidence (`anchor add`, `evidence add`) and cite `ANC-` and `EV-` IDs.
4. Keep observed behavior, inference, hypothesis and uninspected scope separate. Prefix inference with `Inference:`.
5. Finish with `run end --id RUN-NNNN --summary "..." --next "..."`. If it reports `source_changed`, say so; do not call the run successful.
6. Several repositories under one study root: list them with `codebase list`, register one with `codebase add PATH`, find candidates with `codebase scan`. Anchors must fall inside a registered codebase; paths stay relative to the study root (for example `api/src/auth.py`).
7. The kernel's guardrails do not replace an external sandbox. Do not use shell access to work around them.
