# Contributing to <repo>

Short version: **work in a git worktree, never edit the default branch directly,
ship via a PR that passes CI.**

## The workflow

1. **One workstream = one worktree = one branch = one PR.** Never run two editors
   / agent sessions in the same working directory — they share one `HEAD`/index
   and silently clobber each other. The status bar's folder path tells you which
   worktree (= which session) you're in.

2. **Keep the main checkout parked on the default branch; never edit it directly.**
   Treat the top-level clone as the integration anchor (`git fetch` / `git pull`
   there). Do all editing in a worktree:

   ```bash
   scripts/new-workstream.sh fix/<topic>        # bash / WSL / macOS / Linux
   scripts\new-workstream.ps1 fix/<topic>       # Windows PowerShell
   ```

   Both base off `origin/<default>` (fetched first) so you never branch off a
   stale anchor. Tear down when merged: `end-workstream <topic>`.

3. **Branch naming:** `fix/<topic>`, `feat/<topic>`, `docs/<topic>`,
   `chore/<topic>`, `sync/<topic>`, `noncodefix/<topic>` (non-code fast lane),
   `spike/<topic>` (exploration; never merges).

4. **Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/):**
   `<type>(<optional-scope>)!?: <subject>` — types: `feat`, `fix`, `docs`,
   `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert`. The
   `commit-msg` hook in `.githooks/` enforces the floor; optionally install
   `@commitlint/cli` and rename `commitlint.config.cjs.example` for richer
   enforcement (scope enums, body length, etc.).

5. **Ship via PR.** The default branch is branch-protected: **no direct pushes**
   (admins included), and CI must pass. Self-merge once green (or require review).

   ```bash
   git push -u origin <branch>
   gh pr create --fill
   ```

6. **Keep the durable trail in the same PR:** add a `CHANGELOG.md` `[Unreleased]`
   entry; write an ADR for non-obvious decisions; log deferrals in a parking-lot.
   Don't worry about CHANGELOG/parking-lot **merge conflicts** — `.gitattributes`
   marks them `merge=union`, so git concatenates parallel PRs' entries
   automatically.

7. **Docs impact / readability check.** Before you open the PR: does each changed
   doc **start with a summary (and a contextual diagram for non-trivial topics)**?
   Are non-obvious claims **cited** (or marked `unverified`)? Are the **action
   trail** (`docs/history/execution-log.md`) **and `docs/current-state.md`**
   updated if the change moved the current state?

## Local git hooks (auto-enabled)

Hooks under `.githooks/` (a secret scanner + a protected-branch / main-checkout
guard) are wired up automatically when you run your project's install step (e.g.
`npm install` via the `prepare` script, or `make setup`). To enable by hand:

```bash
node scripts/setup-hooks.js     # cross-platform; or: git config core.hooksPath .githooks
```

They are a **seatbelt** (bypassable with `--no-verify`); branch protection is the
authoritative gate.

## Optional: per-machine runtime hardening (AI CLI)

If you drive this repo with an AI CLI (e.g. GitHub Copilot CLI), you can make the
agent prompt before editing the main checkout: in the CLI's permissions config,
ensure the main-checkout location carries no blanket write approval. Approvals
are keyed by exact git root, so each worktree stays frictionless. This is
per-machine config (not in the repo) and a strong gate for your own sessions;
branch protection remains the universal floor.
