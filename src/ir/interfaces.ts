// src/ir/interfaces.ts — Interface resolution: AF_XDP defaults and VLAN
// parents, as the IR carries them.

import type {
  InterfaceKind,
  InterfaceParent,
  XdpIface,
  XdpMode,
} from "../types.ts";

// ── interface resolution ─────────────────────────────────────────────
// The DSL leaves AF_XDP's `mode`/`nRxq` optional and accepts a bare
// string as a VLAN parent (every pre-XDP topology). The IR carries the
// FINAL fact, so both are resolved here — same boundary as shortName,
// mac and the LRP options above.
const XDP_MODE_DEFAULT: XdpMode = "best-effort";

const XDP_NRXQ_DEFAULT = 1;

type ResolvedXdp = {
  kind: "xdp";
  name: string;
  mode: XdpMode;
  nRxq: number;
};

type ResolvedParent = { kind: "physical"; name: string } | ResolvedXdp;

type ResolvedIface =
  | Exclude<InterfaceKind, { kind: "vlan" } | { kind: "xdp" }>
  | (Omit<Extract<InterfaceKind, { kind: "vlan" }>, "vlanParent"> & {
    vlanParent: ResolvedParent;
  })
  | ResolvedXdp;

function resolveXdp(
  iface: Pick<XdpIface, "name" | "mode" | "nRxq">,
): ResolvedXdp {
  return {
    kind: "xdp",
    name: iface.name,
    mode: iface.mode ?? XDP_MODE_DEFAULT,
    nRxq: iface.nRxq ?? XDP_NRXQ_DEFAULT,
  };
}

export function resolveParent(parent: InterfaceParent): ResolvedParent {
  if (typeof parent === "string") return { kind: "physical", name: parent };
  if (parent.kind === "xdp") return resolveXdp(parent);
  return { kind: "physical", name: parent.name };
}

export function resolveIface(iface: InterfaceKind): ResolvedIface {
  if (iface.kind === "vlan") {
    return { ...iface, vlanParent: resolveParent(iface.vlanParent) };
  }
  if (iface.kind === "xdp") return resolveXdp(iface);
  return iface;
}
