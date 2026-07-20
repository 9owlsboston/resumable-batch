# Execution log — resumable-batch

Chronological record of **what was executed** against this repo — commands run,
changes made, and how they were verified. Distinct from `CHANGELOG.md` (which
records *content* changes for consumers); this log records **action** — especially
important because AI agents execute on our behalf.

Append newest-last. Preserve dates, commands, and verification notes; use
completed-state language (record what happened, not what to do).

---

<!-- Template (copy per entry):

### YYYY-MM-DD — <short title>

<what was done + why, in a sentence or two>. Verified: <how — command output,
test, diff, byte-identity, etc.>.
-->

### 2026-07-20 — Repo bootstrapped (profile xs) + design doc seeded

Created the standalone repo for the resumable batch-checkpoint library. Ran
`dev-env-setup/scripts/bootstrap-repo.sh --profile xs` (MVRS floor: README,
AGENTS.md, .gitattributes/.editorconfig/.gitignore, docs/current-state.md,
execution-log, minimal agent contract). Added `docs/design/` and seeded the
accepted spec `checkpoint-library-generalization.md` (v3, carried over from
acr-analytics where the design cycle ran). Filled the README/AGENTS/current-state
placeholders grounding them in the spec (spec stage; no product code yet). Next:
a separate implementation session ports acr-analytics'
`scripts/recipes/batch_checkpoint.py` behind the generalized API. Verified: bootstrap
dry-run then apply reported 0 need-merge conflicts; zero TODO placeholders remain.

### 2026-07-20 — Grew to profile m + ported the engine behind the generalized API

Grew the repo `xs` → `m` via `dev-env-setup/scripts/bootstrap-repo.sh --profile m
--enable-hooks` (added `.githooks/{pre-commit,pre-push,commit-msg}`, worktree
scripts, `CONTRIBUTING.md`, `.github/CODEOWNERS`, docs layout; set
`core.hooksPath=.githooks`); discarded the `*.toolkit-new` re-seeds since the
existing README/AGENTS/current-state/execution-log were already filled. Authored
`.github/workflows/ci.yml` (Linux + Windows × py3.10/3.12 pytest matrix).

Ported `acr-analytics/scripts/recipes/batch_checkpoint.py` into the package
`src/resumable_batch/` behind the spec's generalized API: `engine.run_checkpointed`;
a pluggable `ResultStore` on an `AtomicManifestStore` base with `ParquetResultStore`
+ `JsonlResultStore`; cross-OS `locking` (`PosixFlockLock`/`WindowsLock` +
`make_lock`) and `durability` (statfs allowlist + Windows `GetDriveType`, NTFS
durable). Added the v3 strengthenings: per-commit generation binding + non-self-
referential result-content digest/byte-len/record-count with `has()` reading the
body (C1); domain-defined split trigger with strict-progress + depth cap (B2/C2);
bounded/validated `Retry-After` hook (B7/C3); optional `reduce`/`finalize`
incremental combine (B6/C4). Bumped `SCHEMA_VERSION` 1 → 2.

Verified: `python3 -m pip install -e '.[test]'` then `python3 -m pytest` → **79
passed** on Linux (50 ported CUD-regression tests + 29 generalized-feature tests);
`python3 -m compileall src tests` clean; `python3 -m build --wheel` succeeded
(artifacts removed). Windows leg is authored but exercised only by CI (not this
host).
