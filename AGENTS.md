# AGENTS.md — resumable-batch

Repo-specific operating contract for any AI coding agent (Copilot CLI, VS Code,
Claude, etc.) working here. This file is the **cross-tool source of truth** —
the sibling of `.github/copilot-instructions.md` (which stays thin and points
here).

> **Not here:** the SDLC (explore → plan → implement → verify → ship → **close-out**,
> the `planner`/`implementer`/`verifier` personas + the `explorer`/`rubber-duck`
> review capabilities, conventional commits, branching) lives in the **global**
> `~/.copilot/copilot-instructions.md` (SoT: `ai-tooling-config`) — this file does
> **not** repeat it. The full model + diagram is in
> [`dev-env-setup` `docs/guides/sdlc.md`](https://github.com/9owlsboston/dev-env-setup/blob/main/docs/guides/sdlc.md).
> Keep this file to what is *unique to this repo*.

## 1. What this repo is

A standalone Python **library** (category: engine) — a domain-agnostic checkpoint +
transient-retry orchestrator for long-running chunked work. **Implemented:** the
engine + Parquet/JSONL stores are ported behind the generalized API of
`docs/design/checkpoint-library-generalization.md` (v3). The reference
implementation it was extracted from is `acr-analytics`
(`scripts/recipes/batch_checkpoint.py` + `tests/test_batch_checkpoint.py`); that
CUD wiring remains the regression target. Package lives at `src/resumable_batch/`.

## 2. Hard rules (repo-specific)

- **The engine ships NO domain logic.** Everything domain-specific (`execute`,
  `content_key_fn`, `combine`/`reduce`, `is_oom`/`is_transient`, `split_policy`,
  `worker_setup`) is a **caller-injected function**. If a change teaches the engine
  about ADX, KQL, Graph, HTTP, or pandas, it's in the wrong layer.
- **Preserve the correctness contract** defined in the design doc — do not weaken
  it: content-addressed identity verified on resume; manifest as the atomic commit
  boundary; a `has()` hit is accepted only after full result digest/length/count
  **and** generation-id verification (never ship a wrong or incomplete result);
  unsplittable-OOM stays fatal; every split makes strict progress.
- **Cross-OS is a first-class requirement** — Linux **and** Windows. No unconditional
  `fcntl`/`libc.so.6`/`statfs`; those live behind `LockProvider` / `DurabilityPolicy`
  with a Windows implementation. CI runs a Linux + Windows matrix.
- **Serialization-agnostic** — results are an opaque `TypeVar`, never `pd.DataFrame`
  in the engine core; stores are pluggable (`ParquetResultStore`, `JsonlResultStore`).
- Pin new deps with a floor (`>=`), not `==`; keep the core dependency-light
  (pyarrow only in the parquet store, not the orchestrator).

## 3. Run / test

```bash
python3 -m pip install -e '.[test]'   # editable install + pytest/pandas/pyarrow
python3 -m pytest                      # full suite (ported CUD regression + features)
```

Runtime install with the parquet store: `pip install 'resumable-batch[parquet]'`.
The orchestrator core is stdlib-only; `pyarrow`/`pandas` come only via the
`parquet`/`test` extras. CI runs a **Linux + Windows matrix**
(`.github/workflows/ci.yml`) covering result-digest completeness,
generation-binding torn-write, Retry-After bounds, split-progress, and cross-OS
lock behavior (spec §7).

## 4. Where to write (docs map)

Pick the destination by the **kind** of content, not the topic:

| Kind of content | Goes in |
|---|---|
| How to run / use this repo | `README.md` |
| Rules for agents working here | `AGENTS.md` (this file) |
| Dated "where we are now" snapshot (current → future → gaps) | `docs/current-state.md` |
| **What commands actually ran / how verified** (action trail) | `docs/history/execution-log.md` |
| Durable working memory (issues/chores/decisions/**routines**/memories) | the **agent ledger** (`repo:<name>` scope; promote to `execution-log.md` when it earns a commit, or to `/kb` when generalizable) |
<!-- Optional: declare cross-repo PROJECT membership so `ledger recall` / `profile`
     union open items + facts across EVERY repo that declares the SAME (lowercase)
     project name. Copy the line below, DROP the `-example` suffix so it goes live,
     put it on its own line, and set your name (the `-example` form is inert): -->
<!-- ledger-project-example: your-project-name -->
| Architecture, proposals, decisions — the *why* (Diátaxis *explanation* / ADRs) | `docs/design/` |
| Consumer-facing change log (content) | `CHANGELOG.md` |
<!-- Uncomment as the repo grows into them (profile s+ / grow):
| How-to workflows and walkthroughs (Diátaxis *how-to* / *tutorial*) | `docs/guides/` |
| Stable technical reference — facts that don't expire (Diátaxis *reference*) | `docs/reference/` |
-->

## 5. Drift-rules

Facts that **must stay true** in this repo. `docs-drift` flags any doc/code hit
against a bad-substring below. Use a **live/actionable pattern** (a command or
import used *as if current*), NOT a bare noun — nouns appear in explanatory prose
and history and would just create noise. Add a row whenever a live path moves/renames.

```drift-rules
# <live-pattern>       →   <why it's wrong / what's correct now>
# (example) python old/path/x.py  →   moved to new/path (invoke via $X); <when/why>
```

## 6. Doc-lifecycle (pre / post — agent-enforced)

- **Pre** (session start): read this file + the relevant plan/design doc; run
  `docs-drift` before changing code.
- **Agent memory (ledger):** at session start **recall** the ledger; **apply a
  relevant routine before planning**; **log a routine after a notable success**
  (a routine is a distilled how-to with the five fields
  `goal:/applies-when:/preconditions:/steps:/pitfalls:`). The exact commands (and
  the OS-specific `python3`/`python` invocation) live in the usage guide — see the
  engine + how-to pointer below.
- **During**: update `docs/history/execution-log.md` *as part of* the change
  (what ran, how verified) — not after; keep any plan status honest. Docs change
  *with* code: if a change alters behavior, config, CLI, API, or deployment, the
  closest doc changes in the same change.
- **Post** (session/PR end): **close out the change** — squash-merge, then ff
  `main` (primary worktree) → remove the worktree → delete the local branch (`-D`,
  since a squash-merged branch isn't an ancestor of `main`) → run `docs-drift` →
  update refs on any move/rename (and the drift-rules above) → note residuals. If
  the change moved the current state, reconcile `docs/current-state.md` and bump
  its snapshot date as the **last step**.
- **Wrap review (current-state rubber-duck):** at the end of any change that
  touched product code or config, read `git diff` + `docs/current-state.md` +
  `README.md` + any touched topic docs, then explicitly report one of
  `current-state: updated / not-affected / needs-human-decision`. A read-only
  judgment pass — not a script.
- **Rubber-duck termination:** rubber-duck loops on **blocking** findings only,
  then terminates by **acceptance** (plan-stage) or the stage-3 `verifier` gate
  (diff-stage); round cap 2–3, open blockers at the cap → Open Questions, never
  dropped. Full rule:
  [`dev-env-setup` `docs/guides/sdlc.md`](https://github.com/9owlsboston/dev-env-setup/blob/main/docs/guides/sdlc.md).
- **Rubber-duck enforcement:** rubber-duck is **required** (not optional) at
  plan-stage and diff-stage for code/config changes — record a **ran-or-waived**
  attestation in the design doc's `## Review attestations` (PR body mirrors the
  diff-stage line). Carve-outs (`noncodefix`/`spike`/`release`) are exempt **unless**
  the change touches deps/CI/IaC/security/behavioral config. Full rule:
  [`dev-env-setup` `docs/guides/sdlc.md`](https://github.com/9owlsboston/dev-env-setup/blob/main/docs/guides/sdlc.md).

Full lifecycle spec: global `~/.copilot/copilot-instructions.md`.

**Agent-memory engine + how-to.** The ledger is
[`ledger.py`](https://github.com/9owlsboston/kb-tools/blob/main/ledger.py) in
`kb-tools` — **not** executable and **not** on PATH. Set `KB` for your shell, then
call the interpreter on the script:
- **POSIX:** `KB=~/ws/kb-tools` → `python3 "$KB/ledger.py" <verb>`
- **PowerShell (Windows):** `$KB = "$env:USERPROFILE\ws\kb-tools"` → `python "$KB\ledger.py" <verb>`

How-to:
[agent-memory-usage.md](https://github.com/9owlsboston/dev-env-setup/blob/main/docs/guides/agent-memory-usage.md).
Design:
[agent-memory-ledger.md](https://github.com/9owlsboston/dev-env-setup/blob/main/docs/design/agent-memory-ledger.md).

## 7. Documentation output style

When writing or editing any doc, follow this output contract so a human can trust
and skim it (full rationale: the ecosystem's *AI documentation output contract*
design doc).

**Structure**

- **Summary first.** Open with a plain-English summary a non-author grasps in one
  read: *what this is, who it's for, when to use it.* For non-trivial topics, lead
  the summary with a high-level **contextual diagram** (source in `docs/diagrams/`,
  Mermaid/drawio/excalidraw) — the diagram *is* the summary.
- **Why before how.** State purpose/value before implementation detail.
- **One doc, one intent.** Route by Diátaxis (tutorial / how-to / reference /
  explanation) and the where-to-write map (§4); don't mix intents. *(Exception:
  `docs/current-state.md` is a deliberate rollup/index.)*
- **Link, don't duplicate.** Point to the source-of-truth doc instead of copying
  it; on conflict, the linked topic doc wins.

**Prose discipline (the anti-machine rules)**

- **Don't restate code.** If the code/signature already says it, link to it —
  don't narrate it.
- **No filler, no narration of the obvious.** Cut "In this section we will…" and
  ceremony.
- **Cite or flag.** Every non-obvious behavioral claim must trace to code, a test,
  an ADR, or a linked source — otherwise mark it **`unverified`**.
- **Mark assumptions explicitly.** Never present an assumption as a fact.
- **Length discipline (soft).** Summaries stay short (a few sentences / ≤ ~8
  lines); depth goes in the detail sections below.
