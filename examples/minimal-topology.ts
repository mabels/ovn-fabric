// examples/minimal-topology.ts — the smallest possible topology.
// One host, one physical WAN uplink (masqueraded), one physical LAN
// segment behind it. Copy this file as a starting point for your own
// config — see the README for the full model (segments, uplinks,
// backdoors, WireGuard, etc). This file is also used as a CI smoke
// test (see .github/workflows/ci.yaml), so keep it runnable.

import { defineNetwork, segmentPhysical, uplinkPhysical, ManualUplink } from "../src/mod.ts";

export const network = defineNetwork("minimal", (net) => {
  const host = net.localHost("this-host");

  const wan = net.uplink("wan", uplinkPhysical({
    id: "1",
    name: "eth0",
    nat: { ipv4: [{ kind: "masq" }], ipv6: [{ kind: "masq" }] },
    host,
  }));

  net.segment("lan", segmentPhysical({
    id: "10",
    name: "eth1",
    uplink: new ManualUplink(wan),
    gatewaySuffix: 2,
    slaac: false,
    host,
  }));
});
