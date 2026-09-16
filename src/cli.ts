// src/cli.ts — the CLI entry point.
// Library code: defines the command surface. Does not know about any
// concrete topology — the config module path is supplied at the
// command line, imported dynamically. The config module must export a
// NetworkDefinition (the return value of defineNetwork()), under any
// export name — the loader looks for the first export matching that
// shape rather than requiring a specific name, since defineNetwork
// already collects the hosts/routers/collision-domains and a second,
// separately named re-export of the same data would just be duplication.

import {
  command,
  positional,
  run,
  string,
  subcommands,
} from "npm:cmd-ts@0.13.0";
import { pathToFileURL } from "node:url";

import { toIR } from "./ir.ts";
import { buildJsonSchema } from "./protocol.ts";
import type { NetworkDefinition } from "./define.ts";
import type { Host, InterfaceKind } from "./types.ts";

function describeAccess(host: Host): string {
  // host.connectAddress (a derived string), not host.address (a
  // HostAddress object) — template-literal interpolation of an object
  // is valid TypeScript (implicit toString()) but silently wrong at
  // runtime ("[object Object]"), not something the type checker flags.
  return host.access.method === "ssh"
    ? `ssh ${host.access.user}@${host.connectAddress}`
    : `local`;
}

function describeInterface(ifc: InterfaceKind): string {
  switch (ifc.kind) {
    case "vlan":
      return `vlan ${ifc.vlanId} on ${ifc.vlanParent}` +
        (ifc.ifaceName ? ` (as ${ifc.ifaceName})` : "");
    case "physical":
      return `physical ${ifc.name}`;
    case "bridge-port":
      return `bridge-port ${ifc.port} on ${ifc.bridge}`;
    case "veth":
      return `veth ${ifc.ifaceName} <-> ${ifc.peerName}`;
    case "wireguard":
      return `wireguard ${ifc.ifaceName} -> ${ifc.config.peer.endpoint}`;
    case "zerotier":
      return `zerotier network ${ifc.networkId} (home ${ifc.instanceDir})`;
    case "dummy":
      return `dummy (placeholder, no real interface)`;
  }
}

function isNetworkDefinition(value: unknown): value is NetworkDefinition {
  return (
    typeof value === "object" &&
    value !== null &&
    "name" in value &&
    "allHosts" in value &&
    "allRouters" in value &&
    "allCollisionDomains" in value &&
    Array.isArray((value as NetworkDefinition).allHosts) &&
    Array.isArray((value as NetworkDefinition).allRouters) &&
    Array.isArray((value as NetworkDefinition).allCollisionDomains)
  );
}

async function loadConfig(configPath: string): Promise<NetworkDefinition> {
  const resolved = await Deno.realPath(configPath);
  // pathToFileURL (not a hand-rolled "file://" + string template) is
  // deliberate: something in the JSR publish pipeline appears to
  // rewrite a literal `` `file://${resolved}` `` dynamic-import
  // specifier into a broken relative path (confirmed live, 2026-07-06
  // — v0.1.3/v0.1.4 were both published with that exact line silently
  // corrupted into `../../../../../../${resolved}`, despite the
  // committed source and a fresh git clone both showing the correct
  // "file://" form — the JSR manifest's own recorded file size for
  // src/cli.ts, 6020 bytes, matched the broken variant to the byte,
  // not the correct 6009-byte original). Constructing the URL via the
  // standard API instead of a string literal that merely starts with
  // "file://" sidesteps whatever static pattern that transform keys on.
  const mod = await import(pathToFileURL(resolved).href);

  const found = Object.values(mod).find(isNetworkDefinition);
  if (!found) {
    throw new Error(
      `${configPath} does not export a NetworkDefinition ` +
        `(the return value of defineNetwork(...))`,
    );
  }
  return found;
}

const generate = command({
  name: "generate",
  description: "Load a topology config and report what it declares.",
  args: {
    configPath: positional({
      type: string,
      displayName: "config-path",
      description: "Path to a topology config module (e.g. config/topology.ts)",
    }),
  },
  handler: async ({ configPath }) => {
    const net = await loadConfig(configPath);

    console.log(`Loaded network: ${net.name} (from ${configPath})`);
    console.log(`  hosts: ${net.allHosts.length}`);
    for (const h of net.allHosts) {
      console.log(`    - ${h.name} [${describeAccess(h)}]`);
    }
    console.log(`  collision domains: ${net.allCollisionDomains.length}`);
    for (const d of net.allCollisionDomains) {
      console.log(`    - ${d.name}`);
    }
    console.log(`  routers: ${net.allRouters.length}`);
    for (const r of net.allRouters) {
      console.log(`    - ${r.name}`);
      for (const side of ["left", "right"] as const) {
        const ep = r[side];
        const addrs = ep.ipaddrs.map((a) => a.to_string());
        const ifaces = (ep.ifaces ?? [])
          .map((hi) => describeInterface(hi.iface))
          .join(", ");
        console.log(
          `        ${side}: ls=${ep.l2Segment.name}` +
            (addrs.length > 0 ? ` addrs=[${addrs.join(", ")}]` : "") +
            (ifaces.length > 0 ? ` ifaces=[${ifaces}]` : ""),
        );
      }
    }
    if (net.allKernelRouters.length > 0) {
      console.log(
        `  kernel routers: ${net.allKernelRouters.length} ` +
          `(${net.allKernelRouters.map((k) => k.name).join(", ")})`,
      );
    }
  },
});

const generateIr = command({
  name: "generate-ir",
  description: "Emit the desired-state IR (toIR(), src/ir.ts) as JSON: one " +
    "{id, kind, key, data} node per host/collision-domain/router-port, " +
    "the same envelope shape the reconciler produces from live state. " +
    "Feed this into the Python deployer (deployer/cli.py) to convert " +
    "it into shell scripts.",
  args: {
    configPath: positional({
      type: string,
      displayName: "config-path",
      description: "Path to a topology config module (e.g. config/topology.ts)",
    }),
  },
  handler: async ({ configPath }) => {
    const net = await loadConfig(configPath);
    const nodes = toIR(net);
    console.log(JSON.stringify(Object.values(nodes), null, 2));
  },
});

// ── generate-pytypes: the protocol, TS -> Python ────────────────────
// src/protocol.ts's ArkType schema is the one contract both languages
// agree on for the desired-state IR crossing the boundary (toIR()'s
// JSON -> deployer/ir_to_shell.py) — the stabilizing factor against
// version skew between the two sides, built and deployed independently.
// This command regenerates BOTH artifacts from that one schema:
// protocol/ir-nodes.schema.json (JSON Schema, draft 2020-12) and
// protocol/generated.py (plain stdlib dataclasses via datamodel-
// codegen) — bootstrapping a build-only .venv-build/ with
// datamodel-code-generator installed if it isn't already there.
//
// .venv-build/ is build-time-only tooling on GENERATOR hardware, never
// shipped to a router — same "zero pip-installed dependencies on the
// router itself" boundary the eventual single-file deployer engine
// needs (ADR 0002, "Type stability across the TypeScript/Python
// boundary": the generated dataclasses module imports only
// dataclasses/enum/typing from stdlib).

const BUILD_VENV = ".venv-build";
const SCHEMA_PATH = "protocol/ir-nodes.schema.json";
const GENERATED_PY_PATH = "protocol/generated.py";

async function pathExists(path: string): Promise<boolean> {
  try {
    await Deno.stat(path);
    return true;
  } catch {
    return false;
  }
}

async function runCommand(cmd: string, args: string[]): Promise<void> {
  const { success, stderr } = await new Deno.Command(cmd, {
    args,
    stderr: "piped",
  }).output();
  if (!success) {
    throw new Error(
      `${cmd} ${args.join(" ")} failed:\n${new TextDecoder().decode(stderr)}`,
    );
  }
}

/** Bootstraps .venv-build/ (creates it, installs datamodel-code-
 * generator) only when it isn't already usable — every subsequent
 * `generate-pytypes` run reuses the same venv instead of reinstalling. */
async function ensureDatamodelCodegen(): Promise<string> {
  const codegenPath = `${BUILD_VENV}/bin/datamodel-codegen`;
  if (await pathExists(codegenPath)) return codegenPath;

  if (!await pathExists(BUILD_VENV)) {
    console.error(
      `# creating build-only venv at ${BUILD_VENV}/ (never shipped to a router)`,
    );
    await runCommand("python3", ["-m", "venv", BUILD_VENV]);
  }

  console.error(`# installing datamodel-code-generator into ${BUILD_VENV}/`);
  await runCommand(`${BUILD_VENV}/bin/pip`, [
    "install",
    "--quiet",
    "datamodel-code-generator",
  ]);

  return codegenPath;
}

const generatePytypes = command({
  name: "generate-pytypes",
  description: "Regenerate the protocol between ovn-fabric's TypeScript and " +
    "Python halves (src/protocol.ts's ArkType schema for the " +
    "desired-state IR): writes protocol/ir-nodes.schema.json, then " +
    "runs datamodel-codegen (bootstrapping a build-only .venv-build/ " +
    "with it installed, if needed) to produce protocol/generated.py " +
    "— plain stdlib dataclasses, build-time-only, never shipped to a " +
    "router.",
  args: {},
  handler: async () => {
    const schema = buildJsonSchema();

    await Deno.mkdir("protocol", { recursive: true });
    await Deno.writeTextFile(
      SCHEMA_PATH,
      JSON.stringify(schema, null, 2) + "\n",
    );
    console.error(`wrote ${SCHEMA_PATH}`);

    const codegen = await ensureDatamodelCodegen();
    await runCommand(codegen, [
      "--input",
      SCHEMA_PATH,
      "--input-file-type",
      "jsonschema",
      "--output",
      GENERATED_PY_PATH,
      "--output-model-type",
      "dataclasses.dataclass",
      "--target-python-version",
      "3.11",
      "--disable-timestamp",
    ]);
    console.error(`wrote ${GENERATED_PY_PATH}`);
  },
});

const app = subcommands({
  name: "ovn-fabric",
  description: "Declarative OVN/OVS topology generator CLI",
  cmds: {
    generate,
    "generate-ir": generateIr,
    "generate-pytypes": generatePytypes,
  },
});

if (import.meta.main) {
  await run(app, Deno.args);
}
