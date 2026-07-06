#!/usr/bin/env node
// bin/ovn-fabric.js — the npx/npm entry point.
//
// ovn-fabric itself is a Deno/TypeScript CLI (see src/cli.ts, exported
// as "./cli" from deno.json) — this shim is the only piece of the npm
// distribution that runs under Node. It locates the `deno` executable
// (installed automatically as this package's own npm dependency — see
// https://www.npmjs.com/package/deno, the official, Deno-team-
// maintained npm distribution of the Deno runtime itself) and hands
// off to it, running the CANONICAL, JSR-published module rather than a
// bundled local copy.
//
// Why not just bundle src/ in this npm package and run it directly?
// Because Deno refuses to type-strip .ts files that live inside any
// node_modules directory (mirrors Node's own restriction on its native
// TypeScript support) — once this package is npm-installed, its own
// files necessarily live under node_modules/@adviser/ovn-fabric/, so
// handing deno a local .ts path there fails with
// ERR_UNSUPPORTED_NODE_MODULES_TYPE_STRIPPING. Delegating to the JSR
// copy sidesteps that entirely (JSR-fetched modules are cached outside
// node_modules) and, as a bonus, keeps exactly one published source of
// truth instead of two copies that can drift out of sync.
//
// The JSR fetch is pinned to this exact installed npm version (see
// package.json's own "version", kept in lockstep with deno.json by CI
// — see scripts/patch-version.mjs) — this is a specific, already-
// decided version the user just chose by installing this npm package,
// not a "floating, could be anything" dependency, so bypassing Deno's
// minimum-dependency-age check for THIS fetch only is a narrow,
// justified override rather than a blanket safety bypass.
"use strict";

const { spawnSync } = require("child_process");
const pkg = require("../package.json");

// deno/bin.cjs is itself a small Node launcher (see the `deno` npm
// package) that locates/spawns the real deno binary and forwards
// argv — resolved via require.resolve so this works regardless of how
// npm/pnpm/yarn hoisted node_modules.
const denoLauncher = require.resolve("deno/bin.cjs");
const moduleSpecifier = `jsr:@adviser/ovn-fabric@${pkg.version}/cli`;

const result = spawnSync(
  process.execPath,
  [
    denoLauncher,
    "run",
    "--allow-read",
    "--allow-env",
    "--minimum-dependency-age=0",
    moduleSpecifier,
    ...process.argv.slice(2),
  ],
  { stdio: "inherit" },
);

if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}
process.exit(result.status ?? 1);
