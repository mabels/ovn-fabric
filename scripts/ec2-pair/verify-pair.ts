#!/usr/bin/env -S deno run -A
// verify-pair.ts — check one EC2 pair.
//
//   checkPairOnce(): ONE attempt (short readiness), for the matrix scheduler.
//   verifyPair():    CLI helper that loops checkPairOnce with retries.
//
// A check asserts: cloud-init done + Geneve port present, L2 ping both
// directions, IPv4 egress. Each SSH runs with its own hard timeout.

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
}

export interface CheckOptions {
  json: string | PairJson;
  sshKey?: string;
  /** Per-attempt readiness wait (default 20s). */
  readyTimeoutSec?: number;
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
    "ConnectTimeout=1",
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

// Ready = cloud-init done AND the Geneve port exists.
const READY_CMD =
  "test -f /var/run/pair-ready && ovs-vsctl show 2>/dev/null | grep -q 'type: geneve'";

async function waitReady(
  ip: string,
  key: string | undefined,
  deadline: number,
): Promise<boolean> {
  while (Date.now() < deadline) {
    try {
      await sshRun(ip, READY_CMD, key, 15_000);
      return true;
    } catch {
      await sleep(4_000);
    }
  }
  return false;
}

const TESTER_CMD = [
  'r=$(test -f /var/run/pair-ready && echo 1 || echo 0)',
  'g=$(ovs-vsctl show 2>/dev/null | grep -c "type: geneve")',
  'l=$(ping -c3 -i1 -W2 10.222.0.2 >/dev/null 2>&1 && echo ok || echo fail)',
  'e=$(ping -4 -c2 -W2 1.1.1.1 >/dev/null 2>&1 && echo ok || echo fail)',
  'o=$(grep ^PRETTY_NAME /etc/os-release | cut -d= -f2- | tr -d \'"\')',
  'echo "$r|$g|$l|$e|$o"',
].join("; ");

const TESTED_CMD = [
  'r=$(test -f /var/run/pair-ready && echo 1 || echo 0)',
  'g=$(ovs-vsctl show 2>/dev/null | grep -c "type: geneve")',
  'l=$(ping -c3 -i1 -W2 10.222.0.1 >/dev/null 2>&1 && echo ok || echo fail)',
  'o=$(grep ^PRETTY_NAME /etc/os-release | cut -d= -f2- | tr -d \'"\')',
  'echo "$r|$g|$l|$o"',
].join("; ");

export function failedCheck(line: string): VerifyResult {
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
  };
}

function resolvePair(json: string | PairJson): PairJson {
  if (typeof json !== "string") return json;
  return JSON.parse(Deno.readTextFileSync(json));
}

async function runChecks(
  A: string,
  B: string,
  key: string | undefined,
): Promise<VerifyResult> {
  const [aOut, bOut] = await Promise.all([
    sshRun(A, TESTER_CMD, key, 35_000),
    sshRun(B, TESTED_CMD, key, 35_000),
  ]);
  const [ra, ga, la, ea, osa] = aOut.split("|");
  const [rb, gb, lb, osb] = bOut.split("|");
  return {
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
  };
}

/** One attempt: short readiness wait, then the checks once. */
export async function checkPairOnce(
  o: CheckOptions,
): Promise<VerifyResult> {
  const data = resolvePair(o.json);
  const A = data.tester.publicIpv4;
  const B = data.tested.publicIpv4;
  const deadline = Date.now() + (o.readyTimeoutSec ?? 20) * 1000;

  const readyA = await waitReady(A, o.sshKey, deadline);
  const readyB = await waitReady(B, o.sshKey, deadline);
  if (!readyA || !readyB) {
    return failedCheck(`not ready: tester=${readyA} tested=${readyB}`);
  }
  return runChecks(A, B, o.sshKey);
}

export interface VerifyOptions extends CheckOptions {
  readyTimeoutSec?: number;
  attempts?: number;
  retryDelaySec?: number;
  log?: (line: string) => void;
}

/** CLI helper: readiness with a long wait, then retry the checks. */
export async function verifyPair(o: VerifyOptions): Promise<VerifyResult> {
  const data = resolvePair(o.json);
  const A = data.tester.publicIpv4;
  const B = data.tested.publicIpv4;
  const deadline = Date.now() + (o.readyTimeoutSec ?? 480) * 1000;
  if (
    !await waitReady(A, o.sshKey, deadline) ||
    !await waitReady(B, o.sshKey, deadline)
  ) {
    return failedCheck("cloud-init/geneve timeout");
  }
  const attempts = Math.max(1, o.attempts ?? 6);
  const retryDelay = Math.max(0, o.retryDelaySec ?? 5) * 1000;
  let last = failedCheck("not attempted");
  for (let i = 1; i <= attempts; i++) {
    try {
      last = await runChecks(A, B, o.sshKey);
    } catch (e) {
      last = failedCheck(
        `check error: ${e instanceof Error ? e.message : String(e)}`,
      );
    }
    if (last.ok) return last;
    if (i < attempts) {
      o.log?.(`attempt ${i}/${attempts} failed (${last.line}); retrying`);
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
