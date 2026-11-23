import type { Host } from "./host.ts";
import type { InterfaceKind } from "./interface.ts";

// ── collision domain: a bare OVN logical switch ─────────────────────
// The L2-only primitive underneath Uplink/Segment — those are a
// SUPERSET (a collision domain plus addressing, uplink selection,
// routes, NAT: real L3 concerns, deliberately out of scope here). A
// collision domain is just a name plus whichever real interfaces have
// been attached to it — matches OVN's own minimal Logical_Switch shape
// (everything else — ports, ACLs, DHCP options — hangs off it as
// separate, related objects, not fields on the switch itself).
//
// Deliberately NOT Host-scoped, unlike Uplink/Segment: a real OVN
// logical switch is a cluster-wide object, not owned by any one
// chassis — confirmed live (ADR 0003): sw-test's two ports lived on
// two different chassis, at two different physical sites, with no
// single chassis "owning" the switch itself.
//
// addInterface(), not an `if` field: physical attachment is additive,
// not a fixed one-shot property of the domain — a real logical switch
// can carry more than one localnet port (e.g. redundant physical
// attachment across chassis), and there's no reason to special-case
// "the first/only one" as a constructor field while every subsequent
// one needs a different mechanism. A class with mutable internal state
// (not a plain readonly interface), matching how ManualUplink already
// holds mutable state (`switchTo`) elsewhere in this file — a bare
// data literal can't expose a method.
export interface CollisionDomainOptions {
  /** Absent = cluster-wide OVN logical switch (an ovn.ls IR node).
   * Present = host-local OVS bridge only (a bridge IR node): no OVN
   * logical switch, no localnet port, no bridge mapping. */
  readonly host?: Host;
}

/** A logical OVN router port attached to this domain. `host`/`iface`
 * are its optional physical binding; a purely logical port has neither. */
export interface CollisionDomainOvnPort {
  readonly kind: "ovn";
  readonly router: string;
  readonly endpoint: string;
  readonly host?: Host;
  readonly iface?: InterfaceKind;
  /** Per-leg flow switch (types.ts RouterEndpointInputBase.flow): true
   * when this port's segment traffic must not be exported. Resolved by
   * buildOvnRouterEndpoint, like the iface itself. */
  readonly flowOff?: boolean;
}

/** A physical port the bridge owns directly (VLAN leg, ...). */
export interface CollisionDomainHostPort {
  readonly kind: "host";
  readonly host: Host;
  readonly iface: InterfaceKind;
}

export type CollisionDomainPort =
  | CollisionDomainOvnPort
  | CollisionDomainHostPort;

function collisionDomainPortKey(port: CollisionDomainPort): string {
  if (port.kind === "host") {
    return `host:${port.host.name}:${JSON.stringify(port.iface)}`;
  }
  return `ovn:${port.router}:${port.endpoint}:${port.host?.name ?? ""}:${
    port.iface ? JSON.stringify(port.iface) : ""
  }`;
}

// A collision domain is the L2 primitive: a cluster-wide OVN logical
// switch by default, or a host-local bridge when constructed with a
// `host`. Its PORTS are the single source of truth for what is plugged
// into it — buildOvnRouterEndpoint() adds one port per attached logical
// router endpoint, and addInterface() adds physical ports the bridge
// itself owns. collisionDomainToIR() serializes this, it does not scan
// routers again.
export class CollisionDomain {
  readonly name: string;
  readonly host?: Host;
  private readonly ports = new Map<string, CollisionDomainPort>();

  constructor(name: string, options: CollisionDomainOptions = {}) {
    this.name = name;
    this.host = options.host;
  }

  get hostOnly(): boolean {
    return this.host !== undefined;
  }

  get allPorts(): readonly CollisionDomainPort[] {
    return [...this.ports.values()];
  }

  get ovnPorts(): readonly CollisionDomainOvnPort[] {
    return this.allPorts.filter(
      (port): port is CollisionDomainOvnPort => port.kind === "ovn",
    );
  }

  get hostPorts(): readonly CollisionDomainHostPort[] {
    return this.allPorts.filter(
      (port): port is CollisionDomainHostPort => port.kind === "host",
    );
  }

  /** The one mutation point. A host-only bridge can carry only host
   * ports on its own host; a cluster domain can carry OVN ports on any
   * host. */
  addPort(port: CollisionDomainPort): CollisionDomainPort {
    if (this.hostOnly && port.kind === "ovn") {
      throw new Error(
        `collision domain "${this.name}" is host-only and cannot carry ` +
          `OVN router port ${port.router}:${port.endpoint}`,
      );
    }
    if (this.host !== undefined && port.host?.name !== this.host.name) {
      throw new Error(
        `collision domain "${this.name}" is on host "${this.host.name}", ` +
          `but port ${
            port.kind === "ovn" ? port.endpoint : "host"
          } is on "${port.host?.name}"`,
      );
    }
    const key = collisionDomainPortKey(port);
    if (this.ports.has(key)) {
      throw new Error(
        `collision domain "${this.name}" already has port ${key}`,
      );
    }
    this.ports.set(key, port);
    return port;
  }

  /** Explicit physical port. Host defaults to the domain's own host for
   * a host-only bridge; a cluster domain requires it. Returns the same
   * iface, so it can be used inline. */
  addInterface(
    iface: InterfaceKind,
    host: Host = this.requiredHost(),
  ): InterfaceKind {
    this.addPort({ kind: "host", host, iface });
    return iface;
  }

  private requiredHost(): Host {
    if (this.host === undefined) {
      throw new Error(
        `collision domain "${this.name}" is cluster-wide; pass the host ` +
          `for an explicit physical port`,
      );
    }
    return this.host;
  }
}
