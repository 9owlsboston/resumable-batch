# Documentation layout

This repo uses a four-folder docs convention. Pick the folder that matches the **kind** of document you're writing, not the topic.

| Folder | Holds |
|---|---|
| `design/` | Proposals, architecture, ADRs — the *why* and the *what we decided* (Diátaxis **explanation**). |
| `guides/` | Day-to-day workflows and how-tos — the *how you do this* (Diátaxis **how-to** / **tutorial**). |
| `reference/` | Stable technical reference material — facts that don't expire (Diátaxis **reference**). |
| `history/` | Execution logs, migration records, organization history — *what happened, when*. |

Route by the four Diátaxis **intents** (tutorial / how-to / reference /
explanation) — but map them onto these four existing folders. Don't add new
per-intent subfolders (`tutorials/`, `how-to/`, …); the intent is the *vocabulary*,
not new structure.

If you can't decide between `design/` and `guides/`: design is forward-looking ("we're going to…"); guides are present-tense ("you do this by…").

> Seeded by `bootstrap-repo.sh --with-docs-layout`. Inspired by the layout in [`9owlsboston/dev-env-setup`](https://github.com/9owlsboston/dev-env-setup).
