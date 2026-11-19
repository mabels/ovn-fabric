#!/usr/bin/env -S deno run -A
// verify-pair.ts — verify one EC2 pair created by create-pair.ts.
//
//   deno run -A scripts/ec2-pair/verify-pair.ts --json pair.json
//   deno run -A scripts/ec2-pair/verify-pair.ts --json pair.json --ssh-key /tmp/key
//
// Asserts: both cloud-inits finished AND the Geneve port exists, L2 ping both
// directions, IPv4 egress. Each check runs with a bounded ping window and is
// retried (default 6 attempts, 5 s apart) so a just-booted bridge doesn't flake.

import {
  command,
  number,
  option,
  optional,
  run,
  string,
} from "npm:cmd-ts@0.13.0";
import type { Info } from "./create-pair.ts";

export interface PairJson {
  pair?: string;
  tester: Info;
  tested: Info;
}

export interface VerifyResult {
  ok: boolean;
  line: string;
  testerOs: string;
  testedOs: string;
  geneveTester: number;
  geneveTested: number;
  l2Fwd: boolean;
  l2Rev: boolean;
  ipv4: boolean;
  attempts: number;
}

export interface VerifyOptions {
  json: string | PairJson;
  sshKey?: string;
  readyTimeoutSec?: number;
  attempts?: number;
  retryDelaySec?: number;
  log?: (line: string) => void;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function sshRun(
  ip: string,
  remote: string,
  key: string | undefined,
  timeoutMs = 30_000,
): Promise<string> {
  const args: string[] = [];
  if (key) args.push("-i", key, "-o", "IdentitiesOnly=yes");
  args.push(
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "ConnectTimeout=8",
    `root@${ip}`,
    remote,
  );
  const res = await new Deno.Command("ssh", {
    args,
    stdout: "piped",
    stderr: "piped",
    signal: AbortSignal.timeout(timeoutMs),
  }).output();
  const dec = new TextDecoder();
  if (!res.success) {
    const err = dec.decode(res.stderr).trim().slice(-300);
    throw new Error(`ssh ${ip} failed (${res.code}): ${err}`);
  }
  return dec.decode(res.stdout).trim();
}

// Ready = cloud-init done AND the Geneve port exists (pair-setup may still be
// running when /var/run/pair-ready is not yet written, and vice versa).
const READY_CMD =
  "test -f /var/run/pair-ready && ovs-vsctl show 2>/dev/null | grep -q 'type: geneve'";

async function waitReady(
  ip: string,
  key: string | undefined,
  deadline: number,
): Promise<boolean> {
  while (Date.now() < deadline) {
    try {
      await sshRun(ip, READY_CMD, key, 20_000);
      return true;
    } catch {
      await sleep(5_000);
    }
  }
  return false;
}

const TESTER_CMD = [
  'r=$(test -f /var/run/pair-ready && echo 1 || echo 0)',
  'g=$(ovs-vsctl show 2>/dev/null | grep -c "type: geneve")',
  'l=$(ping -c5 -i1 -W2 10.222.0.2 >/dev/null 2>&1 && echo ok || echo fail)',
  'e=$(ping -4 -c2 -W2 1.1.1.1 >/dev/null 2>&1 && echo ok || echo fail)',
  'o=$(grep ^PRETTY_NAME /etc/os-release | cut -d= -f2- | tr -d \'"\')',
  'echo "$r|$g|$l|$e|$o"',
].join("; ");

const TESTED_CMD = [
  'r=$(test -f /var/run/pair-ready && echo 1 || echo 0)',
  'g=$(ovs-vsctl show 2>/dev/null | grep -c "type: geneve")',
  'l=$(ping -c5 -i1 -W2 10.222.0.1 >/dev/null 2>&1 && echo ok || echo fail)',
  'o=$(grep ^PRETTY_NAME /etc/os-release | cut -d= -f2- | tr -d \'"\')',
  'echo "$r|$g|$l|$o"',
].join("; ");

function failed(line: string, attempts: number): VerifyResult {
  return {
    ok: false,
    line,
    testerOs: "?",
    testedOs: "?",
    geneveTester: 0,
    geneveTested: 0,
    l2Fwd: false,
    l2Rev: false,
    ipv4: false,
    attempts,
  };
}

export async function verifyPair(o: VerifyOptions): Promise<VerifyResult> {
  const data: PairJson = typeof o.json === "string"
    ? JSON.parse(await Deno.readTextFile(o.json))
    : o.json;
  const A = data.tester.publicIpv4;
  const B = data.tested.publicIpv4;
  const deadline = Date.now() + (o.readyTimeoutSec ?? 480) * 1000;
  const attempts = Math.max(1, o.attempts ?? 6);
  const retryDelay = Math.max(0, o.retryDelaySec ?? 5) * 1000;

  const readyA = await waitReady(A, o.sshKey, deadline);
  const readyB = await waitReady(B, o.sshKey, deadline);
  if (!readyA || !readyB) {
    const line = `not ready (cloud-init+geneve): tester=${readyA} tested=${readyB}`;
    o.log?.(line);
    return failed(line, 0);
  }

  let last: VerifyResult = failed("not attempted", 0);
  for (let attempt = 1; attempt <= attempts; attempt++) {
    try {
      const [aOut, bOut] = await Promise.all([
        sshRun(A, TESTER_CMD, o.sshKey, 40_000),
        sshRun(B, TESTED_CMD, o.sshKey, 40_000),
      ]);
      const [ra, ga, la, ea, osa] = aOut.split("|");
      const [rb, gb, lb, osb] = bOut.split("|");
      last = {
        ok: Number(ga) >= 1 && Number(gb) >= 1 && la === "ok" && lb === "ok" &&
          ea === "ok" && ra === "1" && rb === "1",
        line:
          `tester(${osa}) geneve=${ga} l2-fwd=${la} ipv4=${ea} | tested(${osb}) geneve=${gb} l2-rev=${lb}`,
        testerOs: osa ?? "?",
        testedOs: osb ?? "?",
        geneveTester: Number(ga) || 0,
        geneveTested: Number(gb) || 0,
        l2Fwd: la === "ok",
        l2Rev: lb === "ok",
        ipv4: ea === "ok",
        attempts: attempt,
      };
    } catch (e) {
      last = failed(
        `check error: ${e instanceof Error ? e.message : String(e)}`,
        attempt,
      );
    }
    if (last.ok) return last;
    if (attempt < attempts) {
      o.log?.(
        `attempt ${attempt}/${attempts} failed (${last.line}); retry in ${
          retryDelay / 1000
        }s`,
      );
      await sleep(retryDelay);
    }
  }
  return last;
}

const cmd = command({
  name: "verify-pair",
  description: "Verify one EC2 pair (Geneve L2 + egress) with retries.",
  args: {
    json: option({ long: "json", type: string }),
    sshKey: option({ long: "ssh-key", type: optional(string) }),
    readyTimeout: option({
      long: "ready-timeout-seconds",
      type: number,
      defaultValue: () => 480,
    }),
    attempts: option({
      long: "attempts",
      type: number,
      defaultValue: () => 6,
    }),
    retryDelay: option({
      long: "retry-delay-seconds",
      type: number,
      defaultValue: () => 5,
    }),
  },
  handler: async (a) => {
    const r = await verifyPair({
      json: a.json,
      sshKey: a.sshKey,
      readyTimeoutSec: a.readyTimeout,
      attempts: a.attempts,
      retryDelaySec: a.retryDelay,
      log: (s) => console.log(s),
    });
    console.log(r.line);
    if (!r.ok) Deno.exit(1);
  },
});

if (import.meta.main) {
  await run(cmd, Deno.args);
}
