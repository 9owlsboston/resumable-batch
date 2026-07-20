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

### 2026-07-20 — SDLC Verify + confirmation rubber-duck (post-merge audit)

Ran the stage-3 `verifier` (read-only audit vs spec §4/§7) and a fresh
confirmation `rubber-duck` against merged `main` (aed3aeb). Verifier verdict:
**PASS** — all 8 binding acceptance criteria met with file:line evidence, ported
CUD suite byte-preserving (not weakened), reference unsplittable-OOM-fatal
(`batch_checkpoint.py:811-822`) retained. Rubber-duck raised 2 blocking + 2
non-blocking robustness findings on the read paths.

Fixed on branch `fix/store-robustness-verify`: (1) JSONL `_read_file` now catches
`UnicodeDecodeError`/`ValueError` → miss (a corrupt body never raises out of
`has()`); (2) Parquet `_read_file` guards the whole read incl. `to_pandas()` →
miss on any corruption; (3) Parquet `_canonical_bytes` normalizes through an Arrow
round-trip so non-scalar dtypes (list/Decimal) stay digest-stable (a fresh commit
is an immediate hit); (4) `_check` now binds manifest `byte_len`/`record_count` to
the payload too. Closed the verifier's gaps: added the §7 legacy-cache fixture test
(v1 `schema_version=1` cache wiped-not-corrupted on open), strengthened the
non-self-referential-digest test to actively mutate the header, and corrected the
stale test count (79 → 89) in CHANGELOG + current-state.

Verified: `python3 -m pytest` → **89 passed** on Linux; `compileall` clean. Both
Windows CI legs green after merge (see PR #2).
