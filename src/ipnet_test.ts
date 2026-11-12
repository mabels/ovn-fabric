import { assertEquals, assertThrows } from "jsr:@std/assert@1";
import { IpNet, prefixOf, toAddr } from "./ipnet.ts";
import { IPv4 } from "./ip.ts";
import { transitNetwork } from "./addressing.ts";

const lan = new IpNet({
  ipv4: "192.168.128.0/24",
  ipv6: "2600:70ff:b3c0:128::/64",
});

Deno.test("IpNet: a host number is the same number in both families, numeric in IPv6", () => {
  assertEquals(lan.host(1).ipaddrs().map((a) => a.toString()), [
    "192.168.128.1/24",
    "2600:70ff:b3c0:128::1/64",
  ]);
  assertEquals(lan.host(10).asIpv6().toString(), "2600:70ff:b3c0:128::a/64");
});

Deno.test("IpNet: an object host lists exactly the families the address has", () => {
  const v4only = lan.host({ ipv4: 93 });
  assertEquals(v4only.ipaddrs().map((a) => a.pinned()!.to_s()), [
    "192.168.128.93",
  ]);
  assertThrows(() => v4only.asIpv6(), Error, "has no ipv6");
  assertEquals(
    lan.host({ ipv4: 5, ipv6: 7 }).ipaddrs().map((a) => a.pinned()!.to_s()),
    ["192.168.128.5", "2600:70ff:b3c0:128::7"],
  );
});

Deno.test("IpNet: first/last per family, as a transit uses them", () => {
  const t = new IpNet({ ipv4: "10.12.80.1/28", ipv6: "fd00::10:12:80:1/124" });
  assertEquals(t.host("first").ipaddrs().map((a) => a.pinned()!.to_s()), [
    "10.12.80.1",
    "fd00::10:12:80:1",
  ]);
  assertEquals(t.host("last").ipaddrs().map((a) => a.pinned()!.to_s()), [
    "10.12.80.14",
    "fd00::10:12:80:f",
  ]);
  const link = transitNetwork(t);
  assertEquals(link.right.ipv4?.toString(), "10.12.80.14/28");
});

Deno.test("IpNet: compared by value — the written host bits don't matter", () => {
  assertEquals(
    new IpNet("10.12.80.1/28").id(),
    new IpNet("10.12.80.0/28").id(),
  );
  assertEquals(
    new IpNet({ ipv4: "10.0.0.0/24", allocator: { ipv6: "high-low" } }).id(),
    "ipnet:10.0.0.0/24||low-high,high-low",
  );
});

Deno.test("IpNet: bad hosts fail where they are written", () => {
  assertThrows(() => lan.host(0), Error, "not a host number");
  assertThrows(() => lan.host(256), Error, "outside the net");
  assertThrows(
    () => new IpNet({ ipv4: "10.0.0.0/24" }).host(1).asIpv6(),
    Error,
    "has no ipv6",
  );
});

Deno.test("prefixOf: a route destination has exactly one family", () => {
  assertEquals(prefixOf(new IpNet("0.0.0.0/0")).to_string(), "0.0.0.0/0");
  assertThrows(() => prefixOf(lan), Error, "exactly one family");
});

Deno.test("IpNet: an allocated address knows its net and family, not its number", () => {
  const [v4, v6] = lan.allocated().ipaddrs();
  assertEquals(v4!.pinned(), undefined);
  assertEquals(v4!.network().to_string(), "192.168.128.0/24");
  assertEquals(v6!.is_ipv4(), false);
  assertEquals(v4!.toString(), "192.168.128.0/24#auto");
});

Deno.test("Addr.from: a written address is pinned on the net its prefix gives", () => {
  const a = toAddr(IPv4.parse("192.168.132.93/24"));
  assertEquals(a.host, 93);
  assertEquals(a.network().to_string(), "192.168.132.0/24");
  // a next hop without a prefix: a single-address net, the address host 0
  assertEquals(
    toAddr(IPv4.parse("192.168.132.1")).pinned()!.to_s(),
    "192.168.132.1",
  );
});

Deno.test("IpNet: IPv6 needs at least as many host bits as IPv4 (one number for both)", () => {
  assertThrows(
    () => new IpNet({ ipv4: "10.0.0.0/16", ipv6: "fd00::/120" }),
    Error,
    "fewer host bits",
  );
  new IpNet({ ipv4: "10.0.0.0/24", ipv6: "fd00::/120" }); // 8 and 8: fine
});
