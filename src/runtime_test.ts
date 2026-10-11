// src/runtime_test.ts — runtime(): what a topology learns about the run
// loading it (src/runtime.ts).

import { assertEquals, assertThrows } from "jsr:@std/assert@1";
import { parseRuntimeArgs, runtime, setRuntimeArgs } from "./runtime.ts";

Deno.test("parseRuntimeArgs: key=val pairs, split at the FIRST `=`", () => {
  assertEquals(parseRuntimeArgs([]), {});
  assertEquals(
    parseRuntimeArgs(["site=hh", "expr=a=b", "empty="]),
    { site: "hh", expr: "a=b", empty: "" },
  );
});

Deno.test("parseRuntimeArgs: a repeated key keeps its last value", () => {
  assertEquals(parseRuntimeArgs(["debug=0", "debug=1"]), { debug: "1" });
});

Deno.test("parseRuntimeArgs: an entry without `=` or with an empty key throws", () => {
  assertThrows(() => parseRuntimeArgs(["debug"]), Error, "expected key=val");
  assertThrows(() => parseRuntimeArgs(["=1"]), Error, "non-empty key");
});

Deno.test("runtime(): args are what the loader set, env is the process environment", () => {
  try {
    Deno.env.set("OVN_FABRIC_RUNTIME_TEST", "from-env");
    setRuntimeArgs({ site: "hh", debug: "1" });
    const rt = runtime();
    assertEquals(rt.args, { site: "hh", debug: "1" });
    assertEquals(rt.args["missing"], undefined);
    assertEquals(rt.env["OVN_FABRIC_RUNTIME_TEST"], "from-env");
    assertEquals(rt.env["OVN_FABRIC_RUNTIME_TEST_UNSET"], undefined);
  } finally {
    Deno.env.delete("OVN_FABRIC_RUNTIME_TEST");
    setRuntimeArgs({});
  }
});

Deno.test("runtime(): no --runtime given means empty args, not undefined", () => {
  setRuntimeArgs({});
  assertEquals(runtime().args, {});
});

Deno.test("runtime(): a snapshot — frozen, and unaffected by later changes", () => {
  try {
    const given = { site: "hh" };
    setRuntimeArgs(given);
    const before = runtime();
    assertEquals(Object.isFrozen(before.args), true);
    assertEquals(Object.isFrozen(before.env), true);
    // setRuntimeArgs REPLACES (no merge), and neither a later call nor a
    // caller editing the object it passed in reaches an earlier snapshot.
    given.site = "edited";
    setRuntimeArgs({ debug: "1" });
    assertEquals(before.args, { site: "hh" });
    assertEquals(runtime().args, { debug: "1" });
  } finally {
    setRuntimeArgs({});
  }
});
