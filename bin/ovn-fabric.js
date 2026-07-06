#!/usr/bin/env node
// bin/ovn-fabric.js — the npx/npm entry point.
//
// ovn-fabric itself is a Deno/TypeScript CLI (see src/cli.ts) — this
// shim is the only piece of the npm distribution that runs under Node.
// It does nothing but locate the `deno` executable (installed
// automatically as this package's own npm dependency — see
// https://www.npmjs.com/package/deno, the official, Deno-team-
// maintained npm distribution of the Deno runtime itself) and hand off
// to it with the bundled src/cli.ts as the entrypoint. `deno` handles
// picking the right prebuilt binary for the current OS/arch on its own
// (via its optionalDependencies + postinstall) — nothing here needs to
// know or care what platform it's running on.
"use strict";

const path = require("path");
const { spawnSync } = require("child_process");

// deno/bin.cjs is itself a small Node launcher (see the `deno` npm
// package) that locates/spawns the real deno binary and forwards
// argv — resolved via require.resolve so this works regardless of how
// npm/pnpm/yarn hoisted node_modules.
const denoLauncher = require.resolve("deno/bin.cjs");
const cliEntry = path.join(__dirname, "..", "src", "cli.ts");

const result = spawnSync(
  process.execPath,
  [
    denoLauncher,
    "run",
    "--allow-read",
    "--allow-env",
    cliEntry,
    ...process.argv.slice(2),
  ],
  { stdio: "inherit" },
);

if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}
process.exit(result.status ?? 1);
