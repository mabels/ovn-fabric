#!/usr/bin/env node
// scripts/patch-version.mjs — derive the release version from the
// triggering git ref (GITHUB_REF, e.g. "refs/tags/v1.2.3") and write it
// into one or more package.json-shaped files' "version" field.
//
// Mirrors adviser/cement's src/cli/utils.ts#getVersion +
// patch-version-cmd.ts, scaled down to just what ovn-fabric needs (no
// build/pubdir step here — this repo ships plain TS via both npm and
// JSR). The point of doing it this way rather than hand-editing
// package.json/deno.json before tagging: the tag is the single source
// of truth for the release version, so package.json/deno.json in git
// just carry a "0.0.0" placeholder that CI overwrites right before
// publish — nothing to keep in sync by hand, nothing to forget.
//
// Usage: node scripts/patch-version.mjs package.json deno.json
//   GITHUB_REF=refs/tags/v1.2.3 node scripts/patch-version.mjs package.json deno.json
"use strict";

import { readFileSync, writeFileSync } from "node:fs";
import { execSync } from "node:child_process";

function shortSha() {
  try {
    return execSync("git rev-parse --short HEAD").toString().trim();
  } catch {
    return "unknown";
  }
}

// e.g. "refs/tags/v1.2.3" -> "v1.2.3" -> "1.2.3". The leading-char
// strip matches [vdsp] (not just "v") for parity with cement's own
// tagging scheme (v=version, docs/etc. tags) — harmless here since our
// own CI only ever triggers this script from a "v*" tag anyway.
function getVersion() {
  const ref = process.env.GITHUB_REF;

  if (!ref) {
    // Not running in CI (or GITHUB_REF unset) — a local, obviously-dev
    // version, never meant to be published for real.
    return `0.0.0-dev-local-${Date.now()}`;
  }

  const lastPart = ref.split("/").slice(-1)[0].replace(/^[vdsp]/, "");
  if (/^\d+\.\d+\.\d+/.test(lastPart)) {
    return lastPart;
  }
  // Ref didn't look like a version tag (e.g. a plain branch push) —
  // dev version keyed off the commit, still installable/publishable to
  // a "dev" dist-tag if you want, but never mistaken for a release.
  return `0.0.0-dev-ci-${shortSha()}`;
}

function main() {
  const files = process.argv.slice(2);
  if (files.length === 0) {
    console.error("usage: patch-version.mjs <file.json> [file2.json ...]");
    process.exit(1);
  }

  const version = getVersion();
  for (const file of files) {
    const json = JSON.parse(readFileSync(file, "utf-8"));
    json.version = version;
    writeFileSync(file, JSON.stringify(json, null, 2) + "\n");
    console.log(`Patched ${file} version to ${version} (GITHUB_REF=${process.env.GITHUB_REF ?? "unset"})`);
  }
}

main();
