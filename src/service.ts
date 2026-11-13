// A WORKLOAD: what runs in a container — image, command, build, files,
// environment, and what it may do beyond Docker's defaults
// (`securityEnable`). The one way a container is defined: `net.service()`
// for the topology's own workloads, and the fabric's own containers (a VPN
// gateway's tailscaled / zerotier-one) through the same function — so a
// permission is set in ONE place, the Service, carried in the IR, and
// translated to docker flags by the Python side (fabric/emit.py).

import type { ServiceBuilder } from "./builders.ts";
import type { KernelApp, Service } from "./types.ts";

/** A Service from its builder (an object, or a function returning one). */
export function defineService(
  name: string,
  specOrBuild: ServiceBuilder | ((svc: ServiceBuilder) => ServiceBuilder),
): Service {
  const build = typeof specOrBuild === "function"
    ? specOrBuild
    : (svc: ServiceBuilder) => ({ ...svc, ...specOrBuild });
  const svc = build({ image: "" });
  if (svc.image === "") {
    throw new Error(`service "${name}": builder must set an image`);
  }
  const cmd = typeof svc.cmd === "string"
    ? svc.cmd.trim().split(/\s+/).filter(Boolean)
    : svc.cmd;
  return {
    name,
    image: svc.image,
    ...(cmd && cmd.length > 0 ? { cmd } : {}),
    ...(svc.build ? { build: svc.build } : {}),
    ...(svc.files && svc.files.length > 0 ? { files: svc.files } : {}),
    ...(svc.env && Object.keys(svc.env).length > 0 ? { env: svc.env } : {}),
    ...(svc.securityEnable && svc.securityEnable.length > 0
      ? { securityEnable: svc.securityEnable }
      : {}),
    // Filled in by the endpoint builders when this service is attached.
    endpointRefs: [],
  };
}

type DockerApp = Extract<KernelApp, { kind: "docker" }>;

/** The container part of a kernel router's docker app — the container
 * that IS the router's netns (a VPN gateway) — from its Service: image,
 * command, build, and what it may do. Its state (volumes, state files)
 * and its tunnel are the endpoint's, set next to it. A Service's `files`
 * and `env` are a workload's (bind-mounted files, `-e`): refused here
 * rather than dropped. */
export function dockerAppOf(
  service: Service,
): Pick<DockerApp, "image" | "cmd" | "build" | "securityEnable"> {
  if (service.files || service.env) {
    throw new Error(
      `service "${service.name}": files/env are not supported for a ` +
        `router's own container (its state comes from its endpoint)`,
    );
  }
  return {
    image: service.image,
    ...(service.cmd ? { cmd: [...service.cmd] } : {}),
    ...(service.build ? { build: service.build } : {}),
    ...(service.securityEnable
      ? { securityEnable: [...service.securityEnable] }
      : {}),
  };
}
