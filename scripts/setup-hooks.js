#!/usr/bin/env node
/*
 * setup-hooks.js — point this clone's git at the tracked .githooks/ directory.
 *
 * Closes the "hooks are opt-in" hole: instead of every contributor remembering
 * `git config core.hooksPath .githooks`, wire this into a lifecycle step
 * everyone runs — e.g. package.json:  "scripts": { "prepare": "node ./scripts/setup-hooks.js || true" }
 * (npm runs `prepare` on every `npm install`). For non-Node repos, call it from
 * a `make setup` target. Can also be run by hand: `node scripts/setup-hooks.js`.
 *
 * Idempotent and NEVER throws — a missing .git (Docker build, tarball, CI
 * without history) or missing git binary is a silent no-op so it can't break an
 * install. The hooks are a fast-feedback seatbelt; branch protection is the floor.
 */
"use strict";
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

function main() {
  const repoRoot = path.resolve(__dirname, "..");
  const hooksDir = path.join(repoRoot, ".githooks");
  if (!fs.existsSync(hooksDir)) return;
  try {
    execFileSync("git", ["rev-parse", "--is-inside-work-tree"], { cwd: repoRoot, stdio: "ignore" });
  } catch { return; }
  try {
    execFileSync("git", ["config", "core.hooksPath", ".githooks"], { cwd: repoRoot, stdio: "ignore" });
    if (process.platform !== "win32") {
      for (const name of ["pre-commit", "pre-push"]) {
        const p = path.join(hooksDir, name);
        if (fs.existsSync(p)) { try { fs.chmodSync(p, 0o755); } catch { /* non-fatal */ } }
      }
    }
    console.log("[setup-hooks] core.hooksPath -> .githooks (protected-branch + secret guards active)");
  } catch { /* never fail an install over a convenience hook */ }
}
main();
