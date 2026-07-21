# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial engine implementation ported from `acr-analytics/scripts/recipes/batch_checkpoint.py`
  behind the generalized API: `run_checkpointed` orchestrator, pluggable
  `ResultStore` (`ParquetResultStore` + `JsonlResultStore`) on an
  `AtomicManifestStore` base, cross-OS `LockProvider` (`PosixFlockLock` /
  `WindowsLock` via `make_lock`) and `DurabilityPolicy`, per-commit generation
  binding + result-content digest, domain-defined split trigger with strict-progress
  + depth cap, bounded/validated `Retry-After` hook, and optional incremental
  `reduce`/`finalize` combine.
- `pyproject.toml` (package `resumable_batch`; `parquet`/`test` extras) and a
  Linux + Windows GitHub Actions CI matrix.
- Ported CUD regression suite + generalized-feature tests (89 tests).
- Newcomer documentation on-ramp: `docs/guides/getting-started.md` (runnable job +
  crash/resume demo), `docs/reference/concepts.md` (vocabulary + store selection),
  `docs/reference/api.md` (full API reference), `docs/guides/handling-failures.md`
  (classifiers/retry/split); README restructured to lead with a runnable example.

### Changed
- Repo grown from profile `xs` to `m` (git hooks + collaborative scaffolding).
- On-disk `SCHEMA_VERSION` bumped 1 → 2 (adds result digest + generation binding);
  old caches invalidate + wipe cleanly on read.

### Fixed
- Store read paths (`JsonlResultStore`, `ParquetResultStore`) now treat a
  readable-but-corrupt body (invalid UTF-8, unconvertible pandas metadata) as a
  benign cache miss instead of letting the exception escape `has()`/`load()`.
- Parquet result digest normalizes through an Arrow round-trip so non-scalar
  columns (lists, Decimals) stay round-trip-stable — a freshly committed result is
  an immediate cache hit rather than failing its own `has()` check.
- `has()` now also binds the manifest entry's `byte_len`/`record_count` to the
  payload (defense-in-depth alongside the generation binding).

### Removed
### Security

<!--
HOW TO USE THIS FILE
====================
Every PR (except `noncodefix/*`, `spike/*`, `release/*` branches) must add at
least one row under `## [Unreleased]`. The `.githooks/pre-push` hook checks
this locally; CI re-checks it on the server.

Pick the right section:
  Added      — new features
  Changed    — changes in existing functionality
  Deprecated — soon-to-be-removed features
  Removed    — now-removed features
  Fixed      — bug fixes
  Security   — vulnerabilities

Format:  `- <one-line summary> (#<pr-number>)`
Example: `- JWT refresh tokens with 7-day TTL (#142)`

Merge conflicts on this file are handled by `.gitattributes`
(`CHANGELOG.md merge=union`) — parallel PRs' entries concatenate automatically.
-->
