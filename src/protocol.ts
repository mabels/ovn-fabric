// src/protocol.ts — the protocol of ovn-fabric: the one schema both
// languages agree on for the desired-state IR that crosses the
// TypeScript/Python boundary (toIR(), src/ir.ts, producing JSON ->
// deployer/, consuming it). This is the stabilizing factor for that
// boundary — the two sides are built, tested, and deployed
// independently, so nothing else guarantees a newer TS generator's IR
// shape still matches what an older Python deployer bundle expects.
//
// One ArkType schema per kind, unioned into IRNode — matches ADR 0002
// § Decision, point 4 exactly (already the plan recorded there for the
// reconciler's live-state side; this is the same mechanism applied to
// toIR()'s desired-state side).
//
// `key`/`data` are each their OWN named schema (InfraHostKey,
// InfraHostData, ...), not inlined into their Node schema — buildJsonSchema()
// below emits every one of these as its own named entry in the JSON
// Schema's $defs, referenced by $ref from its Node. A plain inlined
// shape has no name of its own in JSON Schema, so datamodel-codegen
// falls back to auto-numbering (Data, Data1, Data2, ...) — fragile:
// that numbering depends on definition order and shifts if a kind is
// ever added or reordered, silently breaking any hand-written Python
// that references e.g. `Data1` by name. Named $defs make every
// generated class name stable regardless of how many kinds exist or
// what order they're declared in.
//
// This schema is consumed two ways, never validated against directly
// at runtime on either side:
//   1. `deno run -A src/cli.ts generate-pytypes` calls buildJsonSchema()
//      and writes protocol/ir-nodes.schema.json.
//   2. `datamodel-codegen` (build-time-only, generator hardware only,
//      never ships to a router) turns that JSON Schema into plain
//      stdlib Python dataclasses (protocol/generated.py) — see that
//      file's own header for why a required-argument TypeError is the
//      intentional, sufficient failure mode for version skew, not a
//      hand-written validator.
//
// The IR is FULLY typed (2026-10-11): no `unknown`, and an open
// `{[string]: string}` map only where the keys really are author-chosen
// (a workload's env vars). Everything else — an interface binding, a
// router's OVN options, an LRP's RA configs — is a closed shape, so:
//   - src/ir.ts builds its nodes against the types INFERRED from these
//     schemas (the `export type X = typeof X.infer` twins below): an IR
//     node that doesn't match the protocol is a compile error;
//   - protocol/hydrate.py walks the generated dataclasses strictly: a
//     field the protocol doesn't declare is an error on the Python side;
//   - the deployer reads attributes of generated classes, never a raw
//     dict.
// A nested shape that should be a NAMED Python class is exported here
// and listed in buildJsonSchema()'s $defs; buildJsonSchema() then turns
// every inlined copy of it into a `$ref` (ArkType inlines nested types,
// which is what made datamodel-codegen invent positional names like
// Apps1/ServiceRef1 and emit the same shape twice).

import { type } from "arktype";

export const InfraHostKey = type({ host: "string" });
export const HostOs = type({ name: "string", version: "string" });
// The host's IPFIX flow-export config (2026-10-10), resolved from
// Host.monitoring.ipfix (types.ts, IpfixExport): the collector endpoint
// every bridge on this host samples into. `targets` is the one required
// field; the rest are OVS IPFIX table defaults when omitted. The
// per-leg ON/OFF lives with each binding (an `iface.flow` on the
// ovn.ls node), not here — this node says WHERE, the leg says WHETHER.
export const IpfixData = type({
  targets: "string",
  "sampling?": "number",
  "cacheActiveTimeout?": "number",
  "cacheMaxFlows?": "number",
});
export const InfraHostData = type({
  connectAddress: "string",
  "ovnRole?": "'central'|'chassis'",
  "encapIp?": "string",
  // Always resolved (assume Ubuntu when the config leaves it unset,
  // 2026-08-23) — the deployer installs the dependencies in this OS's
  // package form. No deinstall ever.
  os: HostOs,
  // The OS packages/requirements this host needs (ovs/ovn, iproute2/
  // iptables, dhcpcd/dhclient, wireguard-tools, zerotier, docker).
  "dependencies?": "string[]",
  // Flow export onto the collector — see IpfixData.
  "ipfix?": IpfixData,
});
export const InfraHostNode = type({
  id: "string",
  kind: "'infra.host'",
  key: InfraHostKey,
  data: InfraHostData,
});

// ── interfaces ────────────────────────────────────────────────────
// A real-world interface binding, one variant per InterfaceKind
// (types.ts), discriminated on `kind`. The base shapes are shared; each
// of the two places an interface appears adds its own IR-resolved facts:
//
//   ovn.ls `interfaces[].iface` (OvnLsIface*) — `shortName`: the real,
//   IFNAMSIZ-safe OVS bridge this domain binds to (src/ir.ts's
//   shortIfaceName, always resolved by the time toIR() emits it), and
//   `flow: false` when the leg opted out of flow export (absent = on).
//
//   kernel.router `ifaces[].iface` (KernelIface*) — the InterfaceKind as
//   declared; only the transit `veth` gains `shortName` (the suffix both
//   real leg names are built from) and `mac` (the netns leg's
//   deterministic MAC, src/addressing.ts's macFromAddrsOrName).
export const WireguardPeerConfig = type({
  publicKey: "string",
  endpoint: "string",
  allowedIps: "string",
  "persistentKeepalive?": "number",
});
export const WireguardAppConfig = type({
  privateKey: "string",
  address: "string",
  "listenPort?": "number",
  "dns?": "string",
  peer: WireguardPeerConfig,
});
// OVS Interface options:xdp-mode / options:n_rxq. src/ir.ts resolves the
// DSL's optional fields to their defaults before emitting the IR, so the
// IR always carries the final fact (same boundary as shortName/mac).
export const XdpMode = type(
  "'best-effort' | 'native-with-zerocopy' | 'native' | 'generic'",
);
const ifaceParentPhysical = { kind: "'physical'", name: "string" } as const;
const ifaceParentXdp = {
  kind: "'xdp'",
  name: "string",
  mode: XdpMode,
  nRxq: "number.integer",
} as const;
// A VLAN parent: a plain NIC (kernel `.${vlanId}` sub-interface) or an
// AF_XDP port (the VLAN split happens in OVS; no kernel child device).
const ifaceParent = type(ifaceParentPhysical).or(type(ifaceParentXdp));
const ifaceVlan = {
  kind: "'vlan'",
  vlanParent: ifaceParent,
  vlanId: "number.integer",
  "ifaceName?": "string",
} as const;
const ifacePhysical = ifaceParentPhysical;
const ifaceXdp = ifaceParentXdp;
const ifaceBridgePort = {
  kind: "'bridge-port'",
  bridge: "string",
  port: "string",
} as const;
const ifaceWireguard = {
  kind: "'wireguard'",
  ifaceName: "string",
  config: WireguardAppConfig,
} as const;
const ifaceZerotier = {
  kind: "'zerotier'",
  networkId: "string",
  "instanceDir?": "string",
} as const;
const ifaceDummy = { kind: "'dummy'" } as const;
const ifaceVeth = {
  kind: "'veth'",
  ifaceName: "string",
  peerName: "string",
} as const;

const lsBinding = { shortName: "string", "flow?": "false" } as const;
export const OvnLsIfaceVlan = type({ ...ifaceVlan, ...lsBinding });
export const OvnLsIfacePhysical = type({ ...ifacePhysical, ...lsBinding });
export const OvnLsIfaceXdp = type({ ...ifaceXdp, ...lsBinding });
export const OvnLsIfaceBridgePort = type({ ...ifaceBridgePort, ...lsBinding });
export const OvnLsIfaceWireguard = type({ ...ifaceWireguard, ...lsBinding });
export const OvnLsIfaceZerotier = type({ ...ifaceZerotier, ...lsBinding });
export const OvnLsIfaceDummy = type({ ...ifaceDummy, ...lsBinding });
export const OvnLsIfaceVeth = type({ ...ifaceVeth, ...lsBinding });
export const OvnLsIface = OvnLsIfaceVlan.or(OvnLsIfacePhysical).or(
  OvnLsIfaceXdp,
).or(OvnLsIfaceBridgePort).or(OvnLsIfaceWireguard).or(OvnLsIfaceZerotier)
  .or(OvnLsIfaceDummy).or(OvnLsIfaceVeth);
export const OvnLsInterface = type({ host: "string", iface: OvnLsIface });

// A kernel router's VLANs live in a netns, so their parent is always a
// plain kernel NIC — an AF_XDP port is OVS-owned and cannot be moved
// there.
export const KernelIfaceVlan = type({
  ...ifaceVlan,
  vlanParent: ifaceParentPhysical,
});
export const KernelIfacePhysical = type(ifacePhysical);
export const KernelIfaceBridgePort = type(ifaceBridgePort);
export const KernelIfaceWireguard = type(ifaceWireguard);
export const KernelIfaceZerotier = type(ifaceZerotier);
export const KernelIfaceDummy = type(ifaceDummy);
export const KernelIfaceVeth = type({
  ...ifaceVeth,
  shortName: "string",
  // Optional only so an IR written before the generator resolved one
  // still validates; the deployer then leaves the kernel's random MAC.
  "mac?": "string",
});
export const KernelIface = KernelIfaceVlan.or(KernelIfacePhysical).or(
  KernelIfaceBridgePort,
).or(KernelIfaceWireguard).or(KernelIfaceZerotier).or(KernelIfaceDummy).or(
  KernelIfaceVeth,
);
export const KernelRouterInterface = type({
  host: "string",
  iface: KernelIface,
});
export const OvnLsKey = type({ name: "string" });
export const OvnLsData = type({
  interfaces: OvnLsInterface.array(),
});
export const OvnLsNode = type({
  id: "string",
  kind: "'ovn.ls'",
  key: OvnLsKey,
  data: OvnLsData,
});

// ovn.lr: the Logical_Router ITSELF (2026-10-11) — until now a router
// only existed in the IR as the group of its ovn.lrp nodes, which left
// nowhere to put a fact about the router as a whole. `options` are the
// FINAL Logical_Router.options, resolved by src/ir.ts's routerToIR from
// Router.options (types.ts): a CLOSED shape whose field names are OVN's
// own option keys, so the deployer can apply every field as
// `options:<field>=<value>` without knowing any of them, and an option
// the protocol doesn't declare can't reach `ovn-nbctl`. The id
// (`ovnrouter:<name>`) is the prefix every ovn.lrp/route id of that
// router already starts with.
export const OvnLrKey = type({ name: "string" });
export const OvnLrOptions = type({
  mac_binding_age_threshold: "number.integer",
});
export const OvnLrData = type({
  options: OvnLrOptions,
});
export const OvnLrNode = type({
  id: "string",
  kind: "'ovn.lr'",
  key: OvnLrKey,
  data: OvnLrData,
});

export const OvnLrpKey = type({
  ovnrouter: "string",
  // The endpoint's own identity (EndpointBase.name, src/types.ts) — the
  // SAME key shape the reconciler uses for a live port
  // (`ovnrouter:<router>|lrp:<port name>`, reconciler/ovn/reconcile.py),
  // which is what makes desired-vs-live merge possible at all.
  name: "string",
});
// (KernelRouterRoute + ServiceRef live here, before OvnLrpData, because
// OvnLrpData.serviceRefs uses ServiceRef at module-eval time.)
export const KernelRouterRoute = type({
  dst: "string",
  "via?": "string",
  // Preference among equal-prefix routes (lower wins) — resolved by the
  // generator from the route/domain/attachment metric, or left unset for
  // the deployer's execution-order fallback (2026-09-19).
  "metric?": "number",
});
// A workload NIC's DNS resolver, already resolved by the generator
// (attachment > control-plane global > CoreDNS). The deployer renders it as
// `docker run --dns …` (2026-09-19).
export const Resolver = type({ nameservers: "string[]" });
export const ServiceRef = type({
  service: "string",
  // The in-container NIC name — RESOLVED by the generator (src/ir.ts's
  // shortIfaceName/fnv1a32: the segment name when it fits IFNAMSIZ, else a
  // hashed short form). The deployer uses it verbatim, never shortens.
  name: "string",
  // The NIC's MAC — RESOLVED by the generator (src/ir.ts,
  // macFromAddrsOrName): a pure function of the NIC's own address, so
  // the segment router's MAC_Binding survives the container being
  // recreated. Optional only so an IR written by an older build still
  // validates; the deployer leaves the kernel's random MAC when absent.
  "mac?": "string",
  "ipaddrs?": "string[]",
  "routes?": KernelRouterRoute.array(),
  "resolver?": Resolver,
});
// Logical_Router_Port.ipv6_ra_configs — a CLOSED shape whose field names
// are OVN's own keys and whose values are already the strings OVN stores
// (src/ir.ts's resolveIpv6RaConfigs); the deployer applies every field
// that is set as `ipv6_ra_configs:<field>=<value>`.
export const Ipv6RaConfigs = type({
  "address_mode?": "'slaac'",
  "send_periodic?": "'true'",
  "min_interval?": "string",
  "max_interval?": "string",
});
export const OvnLrpData = type({
  // `ls:<name>` — the referenced ovn.ls node's own id, not the bare
  // domain name (src/ir.ts's own routerEndpointToIR — every cross-node
  // reference in the IR is an id, confirmed live 2026-08-12).
  l2Segment: "string",
  addresses: "string[]",
  // Always resolved by the time toIR() emits it (src/ir.ts's own
  // resolveMac) — not optional here, so a JSON file missing it (e.g.
  // generated by a stale pre-mac-resolution build) fails protocol
  // validation instead of reaching the deployer at all.
  mac: "string",
  // `host:<name>` — the referenced infra.host node's own id, same
  // reasoning as l2Segment above.
  "gatewayChassis?": "string",
  // Already-resolved ipv6_ra_configs (src/ir.ts's own
  // resolveIpv6RaConfigs, from RouterEndpoint.services — see
  // RouterEndpointService, types.ts) — undefined means no RA service
  // was declared, not "set zero keys."
  "ipv6RaConfigs?": Ipv6RaConfigs,
  // Workloads ATTACHED at this endpoint (endpoint.attachTo) — each is one
  // NIC for the referenced kernel.app.container on this endpoint's segment
  // (2026-09-08).
  "serviceRefs?": ServiceRef.array(),
});
export const OvnLrpNode = type({
  id: "string",
  kind: "'ovn.lrp'",
  key: OvnLrpKey,
  data: OvnLrpData,
});

// ipv4.route / ipv6.route: computed by src/ir.ts's computeRoutes() and
// computeInterconnectRoutes() from net.routingDomain() declarations
// (see RoutingDomain, types.ts) — one node per router per resolved
// route. Same key/data shape for both families (only `kind` differs),
// so one pair of schemas covers both rather than duplicating an
// identical shape twice.
export const RouteKey = type({ ovnrouter: "string", prefix: "string" });
// `domain`: the net.routingDomain() name that produced this route —
// every route belongs to exactly one domain (interconnect routes only
// exist between a domain's own participants, confirmed live
// 2026-08-12; see src/ir.ts's computeInterconnectRoutes). Lets the
// deployer group a router's routes into labeled blocks instead of one
// flat, unlabeled dump.
export const RouteData = type({
  nexthop: "string",
  masq: "boolean",
  domain: "string",
});
export const Ipv4RouteNode = type({
  id: "string",
  kind: "'ipv4.route'",
  key: RouteKey,
  data: RouteData,
});
export const Ipv6RouteNode = type({
  id: "string",
  kind: "'ipv6.route'",
  key: RouteKey,
  data: RouteData,
});

// kernel.router: THREE nodes per KernelRouter (types.ts) — a real Linux
// netns, not an OVN kind at all (src/ir.ts's kernelRouterToIR/
// kernelRouterSideToIR). `side` absent means the router-level node
// (`key: {name}` only) — just the host reference (`host:<name>`, the
// referenced infra.host node's own id, same as OvnLrpData.
// gatewayChassis); `side` present means one of the two per-side nodes
// (same shape convention as ovn.lrp: `key: {name, side}`, `data` fields
// flat), which still repeat `host` too — both live in the same netns,
// and the router-level node is additive, not a replacement for that.
// `ipaddrs`/`routes` are only present on a per-side node: `ipaddrs` is
// already a fully resolved address list, `routes` only the entries with
// a real nexthop to apply (a route with none means "handled elsewhere,"
// e.g. real-world RA/DHCP — see kernelRouterSideToIR's own comment) —
// no real iface binding yet (deliberately deferred, its own next step),
// so the deployer's job for this kind is "create the netns on `host`,
// then a dummy device per side to carry these addresses/routes."
export const KernelRouterKey = type({
  name: "string",
  "side?": "'left'|'right'",
});
// KernelApp — a fully-resolved kernel-netns application (types.ts): the
// `kernel.app.*` service kinds resolved at declaration time. An `or`
// union DISCRIMINATED on `kind` (2026-08-23) — each variant carries
// exactly its own fields, so a docker app can't accidentally gain a
// `style` and a dhcp-client can't carry `image` (the flat-schema shape
// before this couldn't express that).
// (WireguardPeerConfig/WireguardAppConfig live with the interface
// shapes above — a "wireguard" interface carries the same config.)
// A docker image the deployer builds on first use when it is missing.
export const DockerBuild = type({
  from: "string",
  "packages?": "string[]",
  "dockerfile?": "string",
});
export const DhcpClientApp = type({
  kind: "'dhcp-client'",
  style: "'dhclient'|'dhcpcd'",
});
export const WireguardApp = type({
  kind: "'wireguard'",
  ifaceName: "string",
  config: WireguardAppConfig,
  masq: "('ipv4'|'ipv6')[]",
});
export const ZerotierApp = type({
  kind: "'zerotier'",
  networkId: "string",
  instanceDir: "string",
  masq: "('ipv4'|'ipv6')[]",
  // Via-less routes the tunnel carries out the mesh — applied over the
  // runtime-named interface by the wire script (types.ts). Carried as
  // plain {dst} objects; the deployer adds `ip route add <dst> dev <var>`.
  "routes?": KernelRouterRoute.array(),
});
export const DockerApp = type({
  kind: "'docker'",
  image: "string",
  "build?": DockerBuild,
  name: "string",
  "cmd?": "string[]",
  "ip?": "string",
  "routerIp?": "string",
  "vethName?": "string",
});
// The CONFIG-side `kernel.app.docker` service kind (types.ts's
// RouterEndpointService): a docker container the kernel host runs. `image`
// defaults to the router name at resolve time (define.ts); the kind is
// resolved into the IR `docker` app (DockerApp) on the KernelRouter.
export const KernelAppDockerService = type({
  kind: "'kernel.app.docker'",
  "image?": "string",
  "build?": DockerBuild,
  "name?": "string",
  "cmd?": "string | string[]",
});
export const KernelApp = DhcpClientApp.or(WireguardApp).or(ZerotierApp).or(
  DockerApp,
);
export const KernelRouterData = type({
  host: "string",
  "ipaddrs?": "string[]",
  "routes?": KernelRouterRoute.array(),
  // Real physical attachment(s) for this side — {host, iface}, the
  // iface a KernelIface (see the interface shapes above). Present on the
  // `right` side node today (2026-08-18 — the OVN-side endpoint keeps
  // its own copy so the transit domain's bridge binding survives, see
  // buildKernelRouterEndpoint); absent = bind to a dummy device.
  "ifaces?": KernelRouterInterface.array(),
  // The `security.group` node's name (`masq-<router>` for the masq
  // shortcut) attached to THIS side's interface (right side only in
  // practice) — the attachment point for the rules that group carries;
  // see SecurityGroupKey below.
  "securityGroup?": "string",
  // The tunnel router's UPSTREAM peer address(es) — the OTHER end of the
  // upstream veth (right side only; tunnelRouterEndpoint, define.ts). The
  // deployer brings that root-side peer leg up and assigns it these
  // addresses so the netns's default route (via the peer) has a live
  // gateway (2026-08-23).
  "upstreamPeerAddrs?": "string[]",
  // Applications running inside the netns on this side's real interface
  // (right side only in practice — src/ir.ts's kernelRouterSideToIR,
  // from KernelApp, types.ts). The deployer turns each into its
  // `ip netns exec` service script and MUST stop/release it on delete.
  "apps?": KernelApp.array(),
});
export const KernelRouterNode = type({
  id: "string",
  kind: "'kernel.router'",
  key: KernelRouterKey,
  data: KernelRouterData,
});

// security.group: an IMPLEMENTATION-ABSTRACT named set of rules
// (src/ir.ts's securityGroupToIR, from KernelRouterSide.services/
// securityGroup, types.ts). The name is just a label (derived from the
// owning router today, `sg-<router>`, in buildKernelRouterEndpoint,
// define.ts — never author-chosen); the ATTACHMENT lives on whichever
// node carries a `securityGroup` reference (today a kernel.router side
// node, later an ovn.lrp or anything else). Rules use a generic
// vocabulary (kind `masq` = masquerade, family = address family) that
// each implementation maps to its own concrete command; future kernel
// services (docker containers, wireguard) extend this same rules list.
export const SecurityGroupRule = type({
  family: "'ipv4'|'ipv6'",
  kind: "'masq'",
});
export const SecurityGroupKey = type({ name: "string" });
export const SecurityGroupData = type({
  rules: SecurityGroupRule.array(),
});
export const SecurityGroupNode = type({
  id: "string",
  kind: "'security.group'",
  key: SecurityGroupKey,
  data: SecurityGroupData,
});

// A reusable WORKLOAD (net.service) a kernel HOST runs — the image + cmd +
// optional build, network-free. It has NO network of its own: it is bound
// by the ENDPOINTS that reference it (OvnLrpData.serviceRefs) — one NIC per
// referencing endpoint. One service referenced by N endpoints = one
// container with N NICs (the k8s pod model, 2026-09-08).
export const KernelServiceKey = type({ name: "string" });
// A service's WORKLOAD is its NODE KIND — exactly TWO generic kinds, NOT
// nested inside a `kernel.service` envelope. The IR (and the deployer) know
// NOTHING about wireguard/zerotier: those are CONFIG-side shortcuts
// (types.ts/define.ts) that map onto `kernel.app.service` with their
// command/configs/deps. `host` is the `host:<name>` id. A workload carries
// NO network of its own — its NICs come from the endpoints that reference
// it (OvnLrpData.serviceRefs).
//
// `kernel.app.container` — a docker container the host runs; the deployer
// performs the netns magic (veth injection) on it.
// A workload's in-memory FILESYSTEM entry — {path, content}. Shared by both
// app kinds: containers (Service.files, materialised into the container) and
// services (files written before `up`). CONFIG, not secrets — it travels
// through the IR as plain JSON.
export const KernelAppServiceFile = type({
  path: "string",
  // A string, or its LINES — an array diffs far better in the IR (one
  // changed line vs one long escaped string), so generated config uses it.
  content: "string | string[]",
});
// Environment variables for a workload (Service.env) — CONFIG, not
// secrets, since it travels through the IR as plain JSON.
export const AppEnv = type({
  "[string]": "string",
});
export const KernelAppContainerData = type({
  host: "string",
  image: "string",
  "cmd?": "string[]",
  // Environment variables, e.g. ADMIN_PASSWORD / DNS_SERVER_DOMAIN.
  "env?": AppEnv,
  // Files put into the container's filesystem (Service.files); the deployer
  // writes them on the host and bind-mounts each one read-only.
  "files?": KernelAppServiceFile.array(),
  "build?": DockerBuild,
  "deps?": "string[]",
});
export const KernelAppServiceData = type({
  host: "string",
  // Start/stop argv, run INSIDE the service's netns.
  up: "string[]",
  "down?": "string[]",
  // Files written before `up` (e.g. a wg conf) — {path, content}.
  "files?": KernelAppServiceFile.array(),
  // ABSTRACT requirements (e.g. "wireguard-tools", "zerotier-one") — the
  // deployer maps these to the host OS's concrete packages.
  "deps?": "string[]",
});
export const KernelAppContainerNode = type({
  id: "string",
  kind: "'kernel.app.container'",
  key: KernelServiceKey,
  data: KernelAppContainerData,
});
export const KernelAppServiceNode = type({
  id: "string",
  kind: "'kernel.app.service'",
  key: KernelServiceKey,
  data: KernelAppServiceData,
});

export const IRNode = InfraHostNode.or(OvnLsNode).or(OvnLrNode).or(OvnLrpNode)
  .or(
    Ipv4RouteNode,
  ).or(
    Ipv6RouteNode,
  ).or(
    KernelRouterNode,
  ).or(
    SecurityGroupNode,
  ).or(
    KernelAppContainerNode,
  ).or(
    KernelAppServiceNode,
  );

// ── the IR, as TypeScript types ───────────────────────────────────
// Inferred from the schemas above — the types src/ir.ts builds its nodes
// against, so the generator cannot drift from the protocol without a
// compile error. Deeply readonly: an IR node is a finished fact, never
// edited in place. (Type aliases, not interfaces: they are derived from
// the schema values and IRNode is a union.)
type DeepReadonly<T> = T extends readonly (infer Item)[]
  ? readonly DeepReadonly<Item>[]
  : T extends object ? { readonly [Key in keyof T]: DeepReadonly<T[Key]> }
  : T;
export type InfraHostNode = DeepReadonly<typeof InfraHostNode.infer>;
export type OvnLsNode = DeepReadonly<typeof OvnLsNode.infer>;
export type OvnLsIface = DeepReadonly<typeof OvnLsIface.infer>;
export type OvnLsInterface = DeepReadonly<typeof OvnLsInterface.infer>;
export type OvnLrNode = DeepReadonly<typeof OvnLrNode.infer>;
export type OvnLrpNode = DeepReadonly<typeof OvnLrpNode.infer>;
export type Ipv6RaConfigs = DeepReadonly<typeof Ipv6RaConfigs.infer>;
export type ServiceRef = DeepReadonly<typeof ServiceRef.infer>;
export type KernelRouterRoute = DeepReadonly<typeof KernelRouterRoute.infer>;
export type Ipv4RouteNode = DeepReadonly<typeof Ipv4RouteNode.infer>;
export type Ipv6RouteNode = DeepReadonly<typeof Ipv6RouteNode.infer>;
export type RouteNode = Ipv4RouteNode | Ipv6RouteNode;
export type KernelRouterNode = DeepReadonly<typeof KernelRouterNode.infer>;
export type KernelIface = DeepReadonly<typeof KernelIface.infer>;
export type KernelRouterInterface = DeepReadonly<
  typeof KernelRouterInterface.infer
>;
export type KernelApp = DeepReadonly<typeof KernelApp.infer>;
export type SecurityGroupNode = DeepReadonly<typeof SecurityGroupNode.infer>;
export type KernelAppContainerNode = DeepReadonly<
  typeof KernelAppContainerNode.infer
>;
export type KernelAppServiceNode = DeepReadonly<
  typeof KernelAppServiceNode.infer
>;
export type IRNode =
  | InfraHostNode
  | OvnLsNode
  | OvnLrNode
  | OvnLrpNode
  | RouteNode
  | KernelRouterNode
  | SecurityGroupNode
  | KernelAppContainerNode
  | KernelAppServiceNode;

// ── JSON Schema ────────────────────────────────────────────────────
type JsonSchema = { readonly [key: string]: JsonValue };
type JsonValue =
  | string
  | number
  | boolean
  | null
  | readonly JsonValue[]
  | JsonSchema;

function isSchemaObject(value: JsonValue): value is JsonSchema {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// Key-order-independent text of a schema — two shapes are "the same
// shape" exactly when this is equal.
function canonical(value: JsonValue): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (isSchemaObject(value)) {
    return `{${
      Object.keys(value).sort().map((key) =>
        `${JSON.stringify(key)}:${canonical(value[key]!)}`
      ).join(",")
    }}`;
  }
  return JSON.stringify(value);
}

// ArkType's toJsonSchema() INLINES every nested type, so a shape used in
// three places is emitted three times and datamodel-codegen has to
// invent a class name for each copy. Give every named shape back its
// name: wherever a def's own schema appears inside ANOTHER def, replace
// that copy with a `$ref` to it. Matching runs top-down against the
// ORIGINAL (fully inlined) schemas, so the outermost named shape wins and
// a def nested in a def is still found inside it.
//
// Two defs may share one shape (every `{name}` key does) — each keeps its
// own name at the top level. Only a NESTED copy of such a shape has no
// single name to point at, and that throws rather than picking one.
function refNamedShapes(
  shapes: Readonly<Record<string, JsonSchema>>,
): Record<string, JsonSchema> {
  const namesByShape = new Map<string, string[]>();
  for (const [name, schema] of Object.entries(shapes)) {
    const key = canonical(schema);
    namesByShape.set(key, [...(namesByShape.get(key) ?? []), name]);
  }
  const walk = (value: JsonValue, owner: string, top: boolean): JsonValue => {
    if (Array.isArray(value)) {
      return value.map((item) => walk(item, owner, false));
    }
    if (!isSchemaObject(value)) return value;
    const names = top ? undefined : namesByShape.get(canonical(value));
    if (names !== undefined) {
      if (names.length > 1) {
        throw new Error(
          `protocol: a shape nested in "${owner}" matches ${
            names.join(" and ")
          } — give it one exported name, or it cannot become one Python class`,
        );
      }
      return { "$ref": `#/$defs/${names[0]}` };
    }
    return Object.fromEntries(
      Object.entries(value).map((
        [key, item],
      ) => [key, walk(item, owner, false)]),
    );
  };
  return Object.fromEntries(
    Object.entries(shapes).map((
      [name, schema],
    ) => [name, walk(schema, name, true) as JsonSchema]),
  );
}

// Every node kind's envelope has the exact same shape (id: string,
// kind: const, key: $ref, data: $ref) — generated once here instead of
// by hand per kind, so a fifth field added to the envelope someday only
// needs changing in one place.
function nodeSchema(kind: string, keyDef: string, dataDef: string) {
  return {
    type: "object",
    properties: {
      id: { type: "string" },
      kind: { const: kind },
      key: { "$ref": `#/$defs/${keyDef}` },
      data: { "$ref": `#/$defs/${dataDef}` },
    },
    required: ["id", "kind", "key", "data"],
  };
}

// One named shape's own JSON Schema — without the `$schema` marker
// ArkType puts on every root (a nested copy carries none, so it would
// never compare equal to its own definition).
function shape(schema: { toJsonSchema(): unknown }): JsonSchema {
  const { $schema: _dialect, ...rest } = schema.toJsonSchema() as {
    readonly $schema?: string;
    readonly [key: string]: JsonValue | undefined;
  };
  return rest as JsonSchema;
}

/** The full protocol as JSON Schema (draft 2020-12), named $defs
 * throughout — see this file's header comment for why. */
export function buildJsonSchema(): Record<string, unknown> {
  // Every shape that becomes its OWN Python class (or alias), under the
  // name it has in this file — the generated class carries that name.
  const shapes = refNamedShapes({
    InfraHostKey: shape(InfraHostKey),
    HostOs: shape(HostOs),
    IpfixData: shape(IpfixData),
    InfraHostData: shape(InfraHostData),
    WireguardPeerConfig: shape(WireguardPeerConfig),
    WireguardAppConfig: shape(WireguardAppConfig),
    OvnLsIfaceVlan: shape(OvnLsIfaceVlan),
    OvnLsIfacePhysical: shape(OvnLsIfacePhysical),
    OvnLsIfaceBridgePort: shape(OvnLsIfaceBridgePort),
    OvnLsIfaceWireguard: shape(OvnLsIfaceWireguard),
    OvnLsIfaceZerotier: shape(OvnLsIfaceZerotier),
    OvnLsIfaceDummy: shape(OvnLsIfaceDummy),
    OvnLsIfaceVeth: shape(OvnLsIfaceVeth),
    OvnLsIface: shape(OvnLsIface),
    OvnLsInterface: shape(OvnLsInterface),
    OvnLsKey: shape(OvnLsKey),
    OvnLsData: shape(OvnLsData),
    OvnLrKey: shape(OvnLrKey),
    OvnLrOptions: shape(OvnLrOptions),
    OvnLrData: shape(OvnLrData),
    OvnLrpKey: shape(OvnLrpKey),
    KernelRouterRoute: shape(KernelRouterRoute),
    Resolver: shape(Resolver),
    ServiceRef: shape(ServiceRef),
    Ipv6RaConfigs: shape(Ipv6RaConfigs),
    OvnLrpData: shape(OvnLrpData),
    RouteKey: shape(RouteKey),
    RouteData: shape(RouteData),
    KernelRouterKey: shape(KernelRouterKey),
    KernelIfaceVlan: shape(KernelIfaceVlan),
    KernelIfacePhysical: shape(KernelIfacePhysical),
    KernelIfaceBridgePort: shape(KernelIfaceBridgePort),
    KernelIfaceWireguard: shape(KernelIfaceWireguard),
    KernelIfaceZerotier: shape(KernelIfaceZerotier),
    KernelIfaceDummy: shape(KernelIfaceDummy),
    KernelIfaceVeth: shape(KernelIfaceVeth),
    KernelIface: shape(KernelIface),
    KernelRouterInterface: shape(KernelRouterInterface),
    DockerBuild: shape(DockerBuild),
    DhcpClientApp: shape(DhcpClientApp),
    WireguardApp: shape(WireguardApp),
    ZerotierApp: shape(ZerotierApp),
    DockerApp: shape(DockerApp),
    KernelApp: shape(KernelApp),
    KernelRouterData: shape(KernelRouterData),
    SecurityGroupKey: shape(SecurityGroupKey),
    SecurityGroupRule: shape(SecurityGroupRule),
    SecurityGroupData: shape(SecurityGroupData),
    KernelServiceKey: shape(KernelServiceKey),
    KernelAppServiceFile: shape(KernelAppServiceFile),
    AppEnv: shape(AppEnv),
    KernelAppContainerData: shape(KernelAppContainerData),
    KernelAppServiceData: shape(KernelAppServiceData),
  });
  const defs: Record<string, unknown> = {
    ...shapes,
    InfraHostNode: nodeSchema("infra.host", "InfraHostKey", "InfraHostData"),
    OvnLsNode: nodeSchema("ovn.ls", "OvnLsKey", "OvnLsData"),
    OvnLrNode: nodeSchema("ovn.lr", "OvnLrKey", "OvnLrData"),
    OvnLrpNode: nodeSchema("ovn.lrp", "OvnLrpKey", "OvnLrpData"),
    Ipv4RouteNode: nodeSchema("ipv4.route", "RouteKey", "RouteData"),
    Ipv6RouteNode: nodeSchema("ipv6.route", "RouteKey", "RouteData"),
    KernelRouterNode: nodeSchema(
      "kernel.router",
      "KernelRouterKey",
      "KernelRouterData",
    ),
    SecurityGroupNode: nodeSchema(
      "security.group",
      "SecurityGroupKey",
      "SecurityGroupData",
    ),
    KernelAppContainerNode: nodeSchema(
      "kernel.app.container",
      "KernelServiceKey",
      "KernelAppContainerData",
    ),
    KernelAppServiceNode: nodeSchema(
      "kernel.app.service",
      "KernelServiceKey",
      "KernelAppServiceData",
    ),
  };
  return {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$defs": defs,
    "oneOf": [
      "InfraHostNode",
      "OvnLsNode",
      "OvnLrNode",
      "OvnLrpNode",
      "Ipv4RouteNode",
      "Ipv6RouteNode",
      "KernelRouterNode",
      "SecurityGroupNode",
      "KernelAppContainerNode",
      "KernelAppServiceNode",
    ].map((
      name,
    ) => ({
      "$ref": `#/$defs/${name}`,
    })),
  };
}
