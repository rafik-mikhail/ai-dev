# Minimal example

A two-function fixture (`src/auth.py`) for trying the kit end to end. Run from the repository root of this project (a source checkout). The script copies the fixture into a throwaway Git repository, so nothing in the kit directory changes.

```bash
set -e
KIT="$(pwd)"
TARGET="$(mktemp -d)"
cp -r examples/minimal/src "$TARGET/src"
git -C "$TARGET" init -q
git -C "$TARGET" -c user.name=demo -c user.email=demo@example.invalid add -A
git -C "$TARGET" -c user.name=demo -c user.email=demo@example.invalid commit -qm "fixture"

PYTHONPATH="$KIT/src" python -m emkit init "$TARGET"     # or: emkit init "$TARGET"
cd "$TARGET"
S="python .study/kernel.py --agent demo"

$S orient
$S status
$S run start --goal "Map token verification"                     # prints RUN-0001
$S new system auth --title "Authentication" --area src --run RUN-0001
$S anchor add SYS-auth src/auth.py --symbol verify_token --start-line 4 --end-line 7 --run RUN-0001
$S evidence add ANC-0001 --type source-inspection \
    --result "verify_token returns False for empty input and checks an 'ok:' prefix" \
    --limitation "token issuance not inspected" --run RUN-0001
$S claim add SYS-auth "verify_token returns False for empty input" --anchor ANC-0001 --evidence EV-0001 --run RUN-0001
$S finding "Token check is a prefix test only" --severity low --anchor ANC-0001 --run RUN-0001
$S set F-0001 --status triaged --note "confirmed by reading verify_token" --run RUN-0001
$S check
$S search authentication

# the index is disposable
rm .study/study.db
$S rebuild
$S search authentication

$S run end --id RUN-0001 --summary "Mapped verify_token" --next "Trace refresh_token" \
    --covered SYS-auth
$S coverage
```

Expected: `emkit init` ends with `installed emkit ...`, `check` exits 0 with at most a warning about the open run, `search authentication` returns `SYS-auth` before and after the rebuild, and `run end` prints `ended RUN-0001`.

Source-change detection: start a run, edit `src/auth.py` in the target, then end the run. `run end` marks the run `source_changed`, prints the state diff to stderr and exits 1.

```bash
$S run start --goal "guard demo"          # RUN-0002
echo "# touched" >> src/auth.py
$S run end --id RUN-0002 --summary "x"    # exit 1, status source_changed
```
