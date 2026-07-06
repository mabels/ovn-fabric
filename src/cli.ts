// src/cli.ts — the CLI entry point.
// Library code: defines the command surface. Does not know about any
// concrete topology — the config module path is supplied at the
// command line, imported dynamically. The config module must export a
// NetworkDefinition (the return value of defineNetwork()), under any
// export name — the loader looks for the first export matching that
// shape rather than requiring a specific name, since defineNetwork
// already collects allUplinks/allSegments and a second, separately
// named re-export of the same data would just be duplication.

import {
  command,
  positional,
  run,
  string,
  subcommands,
} from "npm:cmd-ts@0.13.0";

import { generateOvnScripts } from "./generate-ovn.ts";
import type { NetworkDefinition } from "./define.ts";
import type { Host, InterfaceKind } from "./types.ts";

function describeAccess(host: Host): string {
  return host.access.method === "ssh"
    ? `ssh ${host.access.user}@${host.address}`
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
    case "wireguard":
      return `wireguard ${ifc.ifaceName} -> ${ifc.config.peer.endpoint}`;
    case "dummy":
      return `dummy (placeholder, no real interface)`;
  }
}

function isNetworkDefinition(value: unknown): value is NetworkDefinition {
  return (
    typeof value === "object" &&
    value !== null &&
    "name" in value &&
    "allUplinks" in value &&
    "allSegments" in value &&
    Array.isArray((value as NetworkDefinition).allUplinks) &&
    Array.isArray((value as NetworkDefinition).allSegments)
  );
}

async function loadConfig(configPath: string): Promise<NetworkDefinition> {
  const resolved = await Deno.realPath(configPath);
  const mod = await import(`file://${resolved}`);

  const found = Object.values(mod).find(isNetworkDefinition);
  if (found === undefined) {
    throw new Error(
      `${configPath} does not export a NetworkDefinition ` +
        `(the return value of defineNetwork(...))`,
    );
  }
  return found;
}

const generate = command({
  name: "generate",
  description:
    "Load a topology config and report what it declares.",
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
    console.log(`  uplinks: ${net.allUplinks.length}`);
    for (const u of net.allUplinks) {
      const addrSummary = u.addresses
        .map((a) => `${a.ipv4.to_string()} / ${a.ipv6.to_string()}`)
        .join(", ");
      console.log(
        `    - ${u.name} (if=${describeInterface(u.if)}, ` +
          `addresses=[${addrSummary}], host=${u.host.name} [${describeAccess(u.host)}])`,
      );
    }
    console.log(`  segments: ${net.allSegments.length}`);
    for (const s of net.allSegments) {
      // undefined uplink = deliberately isolated, no egress yet (see
      // Segment.uplink, types.ts) — not a missing-data bug.
      const selectorKind = s.uplink?.constructor.name ?? "(none)";
      const uplinkName = s.uplink?.resolve().name ?? "(isolated, no uplink)";
      const addrSummary = s.addresses
        .map((a) => `${a.ipv4.to_string()} / ${a.ipv6.to_string()}`)
        .join(", ");
      console.log(
        `    - ${s.name} (if=${describeInterface(s.if)}, ` +
          `addresses=[${addrSummary}], uplink=${uplinkName}, ` +
          `selector=${selectorKind}, host=${s.host.name} [${describeAccess(s.host)}])`,
      );
    }
  },
});

const generateOvn = command({
  name: "generate-ovn",
  description:
    "Emit ONE self-installing shell script per host: copy it to the " +
    "host and run it. Sets up OVS bridges/interfaces (no netplan), " +
    "configures the full OVN topology, and installs itself as a " +
    "boot-time systemd unit (see generate-ovn.ts header comment). " +
    "Pure text output; does not execute anything itself.",
  args: {
    configPath: positional({
      type: string,
      displayName: "config-path",
      description: "Path to a topology config module (e.g. config/topology.ts)",
    }),
  },
  handler: async ({ configPath }) => {
    const net = await loadConfig(configPath);
    const scripts = generateOvnScripts(net);
    // The separator must NEVER be the first line of the combined
    // output: each per-host script is meant to be saved as-is and run
    // directly (including by systemd's ExecStart, which execs the file
    // itself rather than piping it through a shell) — a leading
    // comment line before "#!/bin/sh" breaks that exec entirely
    // (ENOEXEC), silently no-op'ing the whole setup. Confirmed live,
    // 2026-07-06: the installed copy had this marker as line 1, so the
    // boot-time systemd unit never ran a single command, and nothing
    // it was meant to create (e.g. br-bd-4) ever existed. `sh
    // script.sh` tolerates it fine (comments are skipped) — only
    // direct exec doesn't — so this only ever shows up in the one path
    // this generator is actually designed around. Only print the
    // separator BETWEEN scripts (today, with one host declared, it
    // never prints at all).
    let first = true;
    for (const [hostName, script] of scripts) {
      if (!first) console.log(`# ===== host: ${hostName} =====`);
      first = false;
      console.log(script);
    }
  },
});

const app = subcommands({
  name: "ovn-fabric",
  description: "Declarative OVN/OVS topology generator CLI",
  cmds: { generate, "generate-ovn": generateOvn },
});

if (import.meta.main) {
  await run(app, Deno.args);
}
