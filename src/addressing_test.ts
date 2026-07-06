// src/addressing_test.ts — covers segmentNet()'s resolution rules for
// the suffix/suffix6/ipv4/ipv6 combination space (see the doc comment
// directly above segmentNet in addressing.ts for the rules themselves).
// Deliberately exercises every combination by hand rather than just the
// happy path, since this is exactly the kind of "which field wins"
// logic that's easy to get subtly wrong and hard to spot in a generated
// script's output after the fact.

import { assertEquals, assertThrows } from "jsr:@std/assert@1";
import { segmentNet } from "./addressing.ts";

Deno.test("segmentNet: suffix only -> both families fold from it", () => {
  const net = segmentNet(130, { suffix: 5 });
  assertEquals(net.ipv4.to_string(), "192.168.130.5/24");
  assertEquals(net.ipv6.to_string(), "fd00:192:168:130::5/64");
});

Deno.test("segmentNet: suffix + suffix6 -> families fold independently", () => {
  const net = segmentNet(130, { suffix: 2, suffix6: 9 });
  assertEquals(net.ipv4.to_string(), "192.168.130.2/24");
  assertEquals(net.ipv6.to_string(), "fd00:192:168:130::9/64");
});

Deno.test("segmentNet: suffix + ipv4 (no ipv6) -> ipv4 literal wins, ipv6 folds from suffix", () => {
  const net = segmentNet(130, { suffix: 7, ipv4: "10.0.0.99/28" });
  assertEquals(net.ipv4.to_string(), "10.0.0.99/28");
  assertEquals(net.ipv6.to_string(), "fd00:192:168:130::7/64");
});

Deno.test("segmentNet: suffix + ipv6 (no ipv4) -> ipv6 literal wins, ipv4 folds from suffix", () => {
  const net = segmentNet(130, { suffix: 7, ipv6: "fd00:dead:beef::99" });
  assertEquals(net.ipv4.to_string(), "192.168.130.7/24");
  assertEquals(net.ipv6.to_string(), "fd00:dead:beef::99/64");
});

Deno.test("segmentNet: ipv4 only -> ipv4 literal used, ipv6 transfers the host-id out of it", () => {
  const net = segmentNet(130, { ipv4: "192.168.130.5/28" });
  assertEquals(net.ipv4.to_string(), "192.168.130.5/28");
  // transferred: "5" extracted from the ipv4 literal's last octet,
  // folded into the standard v6 pattern.
  assertEquals(net.ipv6.to_string(), "fd00:192:168:130::5/64");
});

Deno.test("segmentNet: ipv6 only -> ipv6 literal used, ipv4 transfers the host-id out of it", () => {
  const net = segmentNet(130, { ipv6: "fd00:192:168:130::5" });
  // transferred: "5" extracted from the ipv6 literal's last group,
  // folded into the standard v4 pattern.
  assertEquals(net.ipv4.to_string(), "192.168.130.5/24");
  assertEquals(net.ipv6.to_string(), "fd00:192:168:130::5/64");
});

Deno.test("segmentNet: ipv4 + ipv6 both given (no suffix) -> both literals used as-is, no transfer", () => {
  const net = segmentNet(130, {
    ipv4: "10.0.0.1/28",
    ipv6: "fd00:dead:beef::1",
  });
  assertEquals(net.ipv4.to_string(), "10.0.0.1/28");
  assertEquals(net.ipv6.to_string(), "fd00:dead:beef::1/64");
});

Deno.test("segmentNet: nothing given at all -> throws", () => {
  assertThrows(
    () => segmentNet(130, {}),
    Error,
    'segment 130 needs one of "suffix", "ipv4", or "ipv6"',
  );
});

Deno.test("segmentNet: bare literal (no /prefix) gets this segment's default prefix, not IPAddress's /32-/128 host route", () => {
  const net = segmentNet(130, { ipv4: "10.0.0.1", ipv6: "fd00:dead:beef::1" });
  assertEquals(net.ipv4.to_string(), "10.0.0.1/24");
  assertEquals(net.ipv6.to_string(), "fd00:dead:beef::1/64");
});

Deno.test("segmentNet: id()/vlan() still reflect the segment id, regardless of literal overrides", () => {
  const net = segmentNet(130, { ipv4: "10.0.0.1/28", ipv6: "fd00::1" });
  assertEquals(net.id(), 130);
  assertEquals(net.vlan(), 130);
});
