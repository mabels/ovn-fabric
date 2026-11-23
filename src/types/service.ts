import type { RouterEndpointService } from "./endpoint-service.ts";
import type { OvnRouterEndpoint } from "./router-endpoint.ts";

// ── Service: a reusable WORKLOAD, and its network attachments ──────────
// A `service` (net.service(), define.ts) is WHAT runs — an image + command
// + optional build — declared ONCE and network-free. It is bound to
// networks by ATTACHING it at router endpoints: each attachment
// (endpoint.attachTo(sv, {...})) is one NIC on that endpoint's segment.
// One service attached at N endpoints = one container with N NICs (the
// k8s pod model, 2026-09-08).
/** One file of a Service's in-memory filesystem — see Service.files. */
export interface ServiceFile {
  /** Absolute path inside the container. */
  readonly path: string;
  /** The file's content, or its LINES — an array diffs far better in the IR
   * (one changed line instead of one long escaped string), so generated
   * config uses it; the deployer joins with newlines either way. */
  readonly content: string | readonly string[];
  /** Its owner, as the uid the container sees (the file is bind-mounted
   * from the host: a user NAME would be the host's, not the image's).
   * Default: root. */
  readonly user?: number;
  /** Its group, as a gid (as `user`). Default: root's. */
  readonly group?: number;
  /** Its permissions, octal (e.g. "600", "0640"). Default: the system's. */
  readonly mode?: string;
}

/** What a container may do beyond Docker's defaults — abstract; the
 * deployer (fabric/emit.py) turns each into its concrete docker flags.
 * - "TUN": make tun devices (a VPN client: /dev/net/tun, NET_ADMIN). */
export type SecurityEnable = "TUN";

/** What the fabric's own containers may do — a Service's, and (never in
 * the topology) "NET_CONFIG": configure the netns it lives in (dhcpcd
 * owning a router's interfaces: NET_ADMIN). */
export type AppSecurityEnable = SecurityEnable | "NET_CONFIG";

export interface Service {
  readonly name: string;
  readonly image: string;
  readonly cmd?: readonly string[];
  /** Files put INTO the workload's filesystem — an in-memory tree that
   * travels through the IR and is materialised by the deployer (written on
   * the host, bind-mounted read-only). CONFIG, not secret material: the IR
   * is plain JSON. Paths are absolute INSIDE the container, e.g.
   * "/etc/coredns/Corefile". */
  readonly files?: readonly ServiceFile[];
  /** Environment variables for the container. Same caveat as `files`:
   * ENVIRONMENT is CONFIG — it travels through the IR, so no secrets. */
  readonly env?: Readonly<Record<string, string>>;
  /** What it may do beyond Docker's defaults. Default: nothing special —
   * the container runs unprivileged. */
  readonly securityEnable?: readonly SecurityEnable[];
  readonly build?: {
    readonly from: string;
    readonly packages?: readonly string[];
    readonly dockerfile?: string;
  };
  /** ENDPOINT-REFERENCES — the `service.attach` entries that attach this
   * service (each is the endpoint's `{ ...service, endpoint }` tuple, so it
   * carries the endpoint AND the attachment's addressing), so the service
   * knows which endpoints reference it. Filled in by the endpoint builders
   * at attach time; mutable on purpose — populated during defineNetwork,
   * not authored (2026-09-08). */
  readonly endpointRefs: EndpointService<
    Extract<RouterEndpointService, { kind: "service.attach" }>
  >[];
}

/** INTERNAL — the EndpointBuilder injects `endpoint` into every `services[]`
 * entry, so each service references the endpoint it's attached to. The
 * generator reads `svc.endpoint` for the segment + addresses + routes to
 * wire the service's NIC (a `wireguard`/`zerotier` service uses the
 * endpoint's; `docker` carries its own). Added by the builder (define.ts),
 * never written by hand (2026-09-08). */
export type EndpointService<T> = T & {
  readonly endpoint: OvnRouterEndpoint;
};
