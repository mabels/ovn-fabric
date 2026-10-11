// src/runtime.ts — what a topology module can learn about the RUN it is
// being loaded in: the process environment and the `--runtime key=val`
// arguments of the CLI invocation that is loading it.
//
// A topology is plain TypeScript evaluated at import time, so there is
// no call to hand it parameters through. runtime() is that hand-over:
//
//   const rt = runtime();
//   const debug = rt.args["debug"] === "1";
//   const collector = rt.env["IPFIX_COLLECTOR"] ?? "192.0.2.10:4739";
//
// The CLI stores the parsed `--runtime` pairs BEFORE it imports the
// config (src/cli.ts's loadConfig). They live in a slot on `globalThis`
// keyed by a registered symbol, not in a module variable: the CLI and the
// topology may each resolve "@adviser/ovn-fabric" to a DIFFERENT module
// instance (the npx shim runs the JSR-published CLI while the topology
// imports whatever version it pins), and a module variable set by one
// instance is invisible to the other.

import process from "node:process";

/** What runtime() returns. Both maps are string-valued, and a key that is
 * not set reads as `undefined`. */
export interface Runtime {
  /** The process environment, as it was when runtime() was called. */
  readonly env: Readonly<Record<string, string | undefined>>;
  /** The `--runtime key=val` arguments of the CLI invocation loading this
   * topology — empty when it is loaded any other way (a test, a script). */
  readonly args: Readonly<Record<string, string | undefined>>;
}

const ARGS_SLOT = Symbol.for("@adviser/ovn-fabric/runtime-args");

function isStringRecord(value: unknown): value is Record<string, string> {
  return typeof value === "object" && value !== null &&
    Object.values(value).every((item) => typeof item === "string");
}

/** The run a topology is being loaded in — see this file's header. */
export function runtime(): Runtime {
  const stored: unknown = Reflect.get(globalThis, ARGS_SLOT);
  return Object.freeze({
    env: Object.freeze({ ...process.env }),
    args: Object.freeze(isStringRecord(stored) ? { ...stored } : {}),
  });
}

/** Set what runtime().args returns from now on — for whoever LOADS a
 * topology (the CLI; a test that wants to exercise a parameterised
 * config). Replaces the previous arguments, it does not merge. */
export function setRuntimeArgs(args: Readonly<Record<string, string>>): void {
  Reflect.set(globalThis, ARGS_SLOT, Object.freeze({ ...args }));
}

/** `key=val` strings (one per `--runtime`) -> a record. The value is
 * everything after the FIRST `=`, so it may itself contain `=`, and may be
 * empty (`key=`). A repeated key keeps its last value. Throws on an entry
 * with no `=` or an empty key. */
export function parseRuntimeArgs(
  pairs: readonly string[],
): Record<string, string> {
  const args: Record<string, string> = {};
  for (const pair of pairs) {
    const at = pair.indexOf("=");
    if (at <= 0) {
      throw new Error(
        `--runtime "${pair}": expected key=val` +
          (at === 0 ? " with a non-empty key" : ""),
      );
    }
    args[pair.slice(0, at)] = pair.slice(at + 1);
  }
  return args;
}
