import { assertEquals, assertThrows } from "jsr:@std/assert@1";
import { defineService, dockerAppOf } from "./service.ts";

Deno.test("defineService: nothing special unless it may do more", () => {
  const plain = defineService("plain", { image: "img", cmd: "a b" });
  assertEquals(plain.securityEnable, undefined);
  assertEquals(plain.cmd, ["a", "b"]);
  const vpn = defineService("vpn", { image: "img", securityEnable: ["TUN"] });
  assertEquals(vpn.securityEnable, ["TUN"]);
});

Deno.test("dockerAppOf: a router's container from its Service, TUN included", () => {
  const service = defineService("home-vpn-tailscale", {
    image: "ovn-fabric-home-vpn-tailscale-tailscale",
    cmd: ["/usr/local/bin/tailscale-up"],
    build: { from: "ubuntu:24.04", dockerfile: "FROM ubuntu:24.04" },
    securityEnable: ["TUN"],
  });
  assertEquals(dockerAppOf(service), {
    image: "ovn-fabric-home-vpn-tailscale-tailscale",
    cmd: ["/usr/local/bin/tailscale-up"],
    build: { from: "ubuntu:24.04", dockerfile: "FROM ubuntu:24.04" },
    securityEnable: ["TUN"],
  });
});

Deno.test("dockerAppOf: a workload's files are refused, not dropped", () => {
  const service = defineService("x", {
    image: "img",
    files: [{ path: "/etc/x", content: "x" }],
  });
  assertThrows(
    () => dockerAppOf(service),
    Error,
    "files/env are not supported",
  );
});
