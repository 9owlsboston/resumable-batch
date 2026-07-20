# Copilot instructions — resumable-batch

> The cross-tool source of truth is **`AGENTS.md`** at the repo
> root. This file is Copilot-specific and intentionally thin.

Read `AGENTS.md` first — it is the **cross-tool repo contract** (repo-specific
hard rules, where-to-write map, drift-rules, doc-lifecycle). The SDLC itself is
**not** repeated there: the 5-stage flow (Plan → Implement → Verify → Ship →
**close-out**), carve-outs (`noncodefix`, `spike`, `release`), commit-message
format, and change-logging convention live in the **global**
`~/.copilot/copilot-instructions.md` (SoT: `ai-tooling-config`), with the full
model + diagram in
[`dev-env-setup` `docs/guides/sdlc.md`](https://github.com/9owlsboston/dev-env-setup/blob/main/docs/guides/sdlc.md).

## When in doubt, switch personas

- Researching → `explorer`
- Planning a change → `planner`
- Writing code → `implementer`
- Auditing a PR → `verifier`
- A typo / doc-only fix → `noncodefix`
- Cutting a release → `release`

Personas live in `~/.copilot/agents/` (personal) or `.github/agents/` (repo).

## Repo-specific rules

<!-- Add your repo-wide rules here. Examples:
- TypeScript only; no plain JS.
- Use early returns; avoid nested conditionals.
- Don't edit files under `vendor/` (generated).
-->
