// Raw inventory schema (live-system facts), deliberately SEPARATE from the
// desired-state IR in protocol.ts. Nothing here is diffed or applied; the
// semantic reconciler consumes a RawSnapshot and emits IR nodes.
//
// Each graph node is FLAT: identity fields (id/host/time/kind) and the
// subsystem payload live on the same object — there is no `collection`
// wrapper to unwrap. Cross-node relationships are id references.

import { type } from "arktype";

import { refNamedShapes, shape } from "./protocol.ts";

export const GLOBAL_NETNS = "*global*";

/** Whose a node is: `ovn-fabric` — ours (what a run fulfils, or else
 * cleans up); any other — found to belong to someone else (by asking
 * docker, OVS, OVN, or from the host's config): never touched. */
export const Owner = type(
  "'ovn-fabric'|'host'|'kernel'|'docker'|'openvswitch'|'ovn-controller'",
);

const base = {
  id: "string",
  host: "string",
  time: "string",
  owner: Owner,
} as const;

function nodeType<K extends string, F extends Record<string, unknown>>(
  kind: K,
  fields: F,
) {
  return type({ ...base, kind: `'${kind}'`, ...fields } as never);
}

// ── host ────────────────────────────────────────────────────────────
const hostFields = {
  hostname: "string",
  "os?": "string",
  "kernel?": "string",
} as const;
export const HostData = type(hostFields);
export const HostCollection = nodeType("host", hostFields);

// ── netns / links / addrs / routes ──────────────────────────────────
const netnsFields = {
  /** "global" for the root namespace; "isolated" otherwise. */
  scope: "'global'|'isolated'",
  name: "string",
  linkRefs: "string[]",
  routeRefs: "string[]",
  ruleRefs: "string[]",
} as const;
export const NetnsData = type(netnsFields);
export const NetnsCollection = nodeType("netns", netnsFields);

export const LinkKind = type(
  "'physical'|'vlan'|'veth'|'bridge'|'internal'|'patch'|'tun'|'loopback'|'wireguard'|'other'",
);
const linkFields = {
  netnsRef: "string",
  ifname: "string",
  ifindex: "number.integer",
  linkKind: LinkKind,
  "mac?": "string",
  /** The hardware's own MAC (`permaddr`, else its MAC) — a NIC's natural
   * key (ADR 0010). */
  "permMac?": "string",
  /** A VLAN's ID — with its parent's natural key, the VLAN's own. */
  "vlanId?": "number.integer",
  /** A WireGuard link's identity: a hash of its private key, never the key. */
  "wgKeyHash?": "string",
  "mtu?": "number.integer",
  "linkRef?": "string",
  "peerRefs?": "string[]",
  "masterRef?": "string",
  "addrRefs?": "string[]",
  adminUp: "boolean",
  "operState?": "string",
} as const;
export const LinkData = type(linkFields);
export const LinkCollection = nodeType("link", linkFields);

const addrFields = {
  netnsRef: "string",
  linkRef: "string",
  family: "'ipv4'|'ipv6'",
  address: "string",
  prefixlen: "number.integer",
  "scope?": "string",
  /** Assigned by the kernel (SLAAC, link-local), not configured. */
  "dynamic?": "boolean",
} as const;
export const AddrData = type(addrFields);
export const AddrCollection = nodeType("addr", addrFields);

const routeFields = {
  netnsRef: "string",
  family: "'ipv4'|'ipv6'",
  prefix: "string",
  "devRef?": "string",
  "via?": "string",
  "metric?": "number.integer",
  "table?": "string",
  "protocol?": "string",
} as const;
export const RouteData = type(routeFields);
export const RouteCollection = nodeType("route", routeFields);

// ── OVS ─────────────────────────────────────────────────────────────
const ovsBridgeFields = {
  name: "string",
  "datapathType?": "string",
  "failMode?": "string",
  portRefs: "string[]",
  externalIds: "Record<string, string>",
} as const;
export const OvsBridgeData = type(ovsBridgeFields);
export const OvsBridgeCollection = nodeType("ovs.bridge", ovsBridgeFields);

const ovsPortFields = {
  bridgeRef: "string",
  name: "string",
  "tag?": "number.integer",
  trunks: "number.integer[]",
  ifaceRefs: "string[]",
  externalIds: "Record<string, string>",
} as const;
export const OvsPortData = type(ovsPortFields);
export const OvsPortCollection = nodeType("ovs.port", ovsPortFields);

const ovsIfaceFields = {
  portRef: "string",
  "netnsRef?": "string",
  name: "string",
  "type?": "string",
  "mac?": "string",
  options: "Record<string, string>",
  externalIds: "Record<string, string>",
} as const;
export const OvsIfaceData = type(ovsIfaceFields);
export const OvsIfaceCollection = nodeType("ovs.iface", ovsIfaceFields);

/** One Open_vSwitch external_ids entry (the host's own OVN settings:
 * ovn-remote, ovn-encap-type, ovn-encap-ip, …). The bridge mappings are
 * not one of them: they are a field of each bridge. */
const ovsExternalIdFields = {
  key: "string",
  value: "string",
} as const;
export const OvsExternalIdData = type(ovsExternalIdFields);
export const OvsExternalIdCollection = nodeType(
  "ovs.external-id",
  ovsExternalIdFields,
);

// ── OVN northbound ──────────────────────────────────────────────────
const ovnLsFields = {
  name: "string",
  portRefs: "string[]",
  externalIds: "Record<string, string>",
} as const;
export const OvnLsData = type(ovnLsFields);
export const OvnLsCollection = nodeType("ovn.ls", ovnLsFields);

const ovnLspFields = {
  switchRef: "string",
  name: "string",
  "type?": "string",
  addresses: "string[]",
  options: "Record<string, string>",
  externalIds: "Record<string, string>",
} as const;
export const OvnLspData = type(ovnLspFields);
export const OvnLspCollection = nodeType("ovn.lsp", ovnLspFields);

const ovnLrFields = {
  name: "string",
  options: "Record<string, string>",
  portRefs: "string[]",
  routeRefs: "string[]",
  natRefs: "string[]",
  aclRefs: "string[]",
  externalIds: "Record<string, string>",
} as const;
export const OvnLrData = type(ovnLrFields);
export const OvnLrCollection = nodeType("ovn.lr", ovnLrFields);

const ovnLrpFields = {
  routerRef: "string",
  name: "string",
  mac: "string",
  networks: "string[]",
  gatewayChassis: "string[]",
  ipv6RaConfigs: "Record<string, string>",
  externalIds: "Record<string, string>",
} as const;
export const OvnLrpData = type(ovnLrpFields);
export const OvnLrpCollection = nodeType("ovn.lrp", ovnLrpFields);

const ovnRouteFields = {
  routerRef: "string",
  prefix: "string",
  nexthop: "string",
  "policy?": "string",
  externalIds: "Record<string, string>",
} as const;
export const OvnRouteData = type(ovnRouteFields);
export const OvnRouteCollection = nodeType("ovn.route", ovnRouteFields);

export const OvnNatType = type("'snat'|'dnat'|'dnat_and_snat'");
const ovnNatFields = {
  routerRef: "string",
  type: OvnNatType,
  externalIp: "string",
  logicalIp: "string",
  "externalMac?": "string",
  "logicalPort?": "string",
  externalIds: "Record<string, string>",
} as const;
export const OvnNatData = type(ovnNatFields);
export const OvnNatCollection = nodeType("ovn.nat", ovnNatFields);

const ovnAclFields = {
  switchRef: "string",
  direction: "'from-lport'|'to-lport'",
  priority: "number.integer",
  match: "string",
  action: "string",
  externalIds: "Record<string, string>",
} as const;
export const OvnAclData = type(ovnAclFields);
export const OvnAclCollection = nodeType("ovn.acl", ovnAclFields);

/** Where an OVN database listens (its Connection rows): one node per
 * database, the targets as `ovn-nbctl get-connection` lists them. */
const ovnConnectionFields = {
  db: "'nb'|'sb'",
  targets: "string[]",
} as const;
export const OvnConnectionData = type(ovnConnectionFields);
export const OvnConnectionCollection = nodeType(
  "ovn.connection",
  ovnConnectionFields,
);

export const IptablesAction = type(
  "'*'|'ACCEPT'|'DROP'|'REJECT'|'JUMP'|'MASQUERADE'|'SNAT'|'DNAT'|'REDIRECT'|'RETURN'|'CONTINUE'|'GOTO'",
);
export const IptablesRuleFields = type({
  "src?": "string",
  "dst?": "string",
  "iif?": "string",
  "oif?": "string",
  "proto?": "string",
  "sport?": "string",
  "dport?": "string",
});
const iptablesRuleFields = {
  action: IptablesAction,
  fields: IptablesRuleFields,
  /** The chain a JUMP/GOTO goes to. */
  "target?": "string",
} as const;
export const IptablesRuleSpec = type(iptablesRuleFields);

const iptablesFields = {
  netnsRef: "string",
  family: "'ipv4'|'ipv6'",
  table: "string",
  chain: "string",
  rule: IptablesRuleSpec,
  "comment?": "string",
  "handle?": "string",
  /** nft's handle of the rule: how it is deleted exactly. */
  "nftHandle?": "number.integer",
} as const;
export const IptablesRuleData = type(iptablesFields);
export const IptablesRuleCollection = nodeType(
  "iptables.rule",
  iptablesFields,
);

// ── docker ──────────────────────────────────────────────────────────
const dockerFields = {
  containerId: "string",
  name: "string",
  image: "string",
  pid: "number.integer",
  state: "string",
  netnsRef: "string",
  /** What it declares itself to be (its `ovn-fabric.natural-key` label,
   * ADR 0010), e.g. a ZeroTier instance's `zt:<node address>`. */
  "naturalKeyLabel?": "string",
} as const;
export const DockerContainerData = type(dockerFields);
export const DockerContainerCollection = nodeType(
  "docker.container",
  dockerFields,
);

// ── the snapshot ────────────────────────────────────────────────────
export const RawCollection = HostCollection
  .or(NetnsCollection)
  .or(LinkCollection)
  .or(AddrCollection)
  .or(RouteCollection)
  .or(OvsBridgeCollection)
  .or(OvsPortCollection)
  .or(OvsIfaceCollection)
  .or(OvsExternalIdCollection)
  .or(OvnLsCollection)
  .or(OvnLspCollection)
  .or(OvnLrCollection)
  .or(OvnLrpCollection)
  .or(OvnRouteCollection)
  .or(OvnNatCollection)
  .or(OvnAclCollection)
  .or(OvnConnectionCollection)
  .or(IptablesRuleCollection)
  .or(DockerContainerCollection);

export const RawSnapshot = type({
  host: "string",
  time: "string",
  collections: RawCollection.array(),
});

export type RawSnapshot = typeof RawSnapshot.infer;
export type Owner = typeof Owner.infer;
export type RawCollection = typeof RawCollection.infer;
export type NetnsData = typeof NetnsData.infer;
export type LinkData = typeof LinkData.infer;
export type AddrData = typeof AddrData.infer;
export type RouteData = typeof RouteData.infer;
export type OvsBridgeData = typeof OvsBridgeData.infer;
export type OvsPortData = typeof OvsPortData.infer;
export type OvsIfaceData = typeof OvsIfaceData.infer;
export type OvsExternalIdData = typeof OvsExternalIdData.infer;
export type OvnLsData = typeof OvnLsData.infer;
export type OvnLspData = typeof OvnLspData.infer;
export type OvnLrData = typeof OvnLrData.infer;
export type OvnLrpData = typeof OvnLrpData.infer;
export type OvnRouteData = typeof OvnRouteData.infer;
export type OvnNatData = typeof OvnNatData.infer;
export type OvnAclData = typeof OvnAclData.infer;
export type OvnConnectionData = typeof OvnConnectionData.infer;
export type IptablesRuleData = typeof IptablesRuleData.infer;
export type DockerContainerData = typeof DockerContainerData.infer;

// ── JSON Schema for the Python codegen ──────────────────────────────
export function buildRawJsonSchema(): Record<string, unknown> {
  const shapes = refNamedShapes({
    Owner: shape(Owner),
    HostData: shape(HostData),
    HostCollection: shape(HostCollection),
    NetnsData: shape(NetnsData),
    NetnsCollection: shape(NetnsCollection),
    LinkKind: shape(LinkKind),
    LinkData: shape(LinkData),
    LinkCollection: shape(LinkCollection),
    AddrData: shape(AddrData),
    AddrCollection: shape(AddrCollection),
    RouteData: shape(RouteData),
    RouteCollection: shape(RouteCollection),
    OvsBridgeData: shape(OvsBridgeData),
    OvsBridgeCollection: shape(OvsBridgeCollection),
    OvsPortData: shape(OvsPortData),
    OvsPortCollection: shape(OvsPortCollection),
    OvsIfaceData: shape(OvsIfaceData),
    OvsIfaceCollection: shape(OvsIfaceCollection),
    OvsExternalIdData: shape(OvsExternalIdData),
    OvsExternalIdCollection: shape(OvsExternalIdCollection),
    OvnLsData: shape(OvnLsData),
    OvnLsCollection: shape(OvnLsCollection),
    OvnLspData: shape(OvnLspData),
    OvnLspCollection: shape(OvnLspCollection),
    OvnLrData: shape(OvnLrData),
    OvnLrCollection: shape(OvnLrCollection),
    OvnLrpData: shape(OvnLrpData),
    OvnLrpCollection: shape(OvnLrpCollection),
    OvnRouteData: shape(OvnRouteData),
    OvnRouteCollection: shape(OvnRouteCollection),
    OvnNatType: shape(OvnNatType),
    OvnNatData: shape(OvnNatData),
    OvnNatCollection: shape(OvnNatCollection),
    OvnAclData: shape(OvnAclData),
    OvnAclCollection: shape(OvnAclCollection),
    OvnConnectionData: shape(OvnConnectionData),
    OvnConnectionCollection: shape(OvnConnectionCollection),
    IptablesAction: shape(IptablesAction),
    IptablesRuleFields: shape(IptablesRuleFields),
    IptablesRuleSpec: shape(IptablesRuleSpec),
    IptablesRuleData: shape(IptablesRuleData),
    IptablesRuleCollection: shape(IptablesRuleCollection),
    DockerContainerData: shape(DockerContainerData),
    DockerContainerCollection: shape(DockerContainerCollection),
    RawCollection: shape(RawCollection),
    RawSnapshot: shape(RawSnapshot),
  });
  return {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$defs": shapes,
    "$ref": "#/$defs/RawSnapshot",
  };
}
