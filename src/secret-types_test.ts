import { assertEquals, assertThrows } from "jsr:@std/assert@1";
import { transitBlocks } from "./secret-types.ts";

const POOL = {
  ipv4: "10.99.0.0/24",
  ipv6: "2001:db8:1:5452:414e:5349:5400:0/112",
};

Deno.test("transitBlocks: the next /28 + /124, in call order", () => {
  const next = transitBlocks(POOL);
  assertEquals(next(), {
    ipv4: "10.99.0.0/28",
    ipv6: "2001:db8:1:5452:414e:5349:5400::/124",
  });
  assertEquals(next(), {
    ipv4: "10.99.0.16/28",
    ipv6: "2001:db8:1:5452:414e:5349:5400:10/124",
  });
});

Deno.test("transitBlocks: an unaligned pool starts at its network", () => {
  // 10.99.2.0/22 is 10.99.0.0/22 — the first block is .0.0, not .2.0
  const next = transitBlocks({ ...POOL, ipv4: "10.99.2.0/22" });
  assertEquals(next().ipv4, "10.99.0.0/28");
});

Deno.test("transitBlocks: the smaller pool is the limit", () => {
  const next = transitBlocks(POOL); // a /24 holds 16 blocks, the /112 4096
  for (let i = 0; i < 16; i++) next();
  assertThrows(() => next(), Error, "used up after 16 blocks");
});

Deno.test("transitBlocks: a pool that is no net is refused", () => {
  assertThrows(
    () => transitBlocks({ ...POOL, ipv4: "not-a-net" }),
    Error,
    "not-a-net",
  );
});
