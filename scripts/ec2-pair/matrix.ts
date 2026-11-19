#!/usr/bin/env -S deno run -A
// matrix.ts — create + verify + destroy the OS/arch matrix in ONE process.
//
// Two phases, both sequential at the top level:
//   1. launch every pair (RunInstances only — fast), so all boot concurrently
//   2. a work-queue scheduler: test each pair with a hard timeout; on
//      timeout/error requeue it (bounded retry/error counts); on success mark
//      it done. Continue until every pair is success or exhausted.
//
//   deno run -A scripts/ec2-pair/matrix.ts
//   deno run -A scripts/ec2-pair/matrix.ts --only m-ub2404-x86,m-deb13-x86
//
// One EC2 client with adaptive retries and a shared launch pacer, a generated
// session key added beside the owner key, and guaranteed teardown on success,
// failure, SIGINT or SIGTERM. Prints a JSON summary; exits non-zero on failure.

import {
  command,
  flag,
  number,
  option,
  optional,
  run,
  string,
} from "npm:cmd-ts@0.13.0";
import { EC2Client as EC2 } from "npm:@aws-sdk/client-ec2@3";
import {
  DEFAULT_KEYS,
  destroyPair,
  ensureNetwork,
  instanceInfo,
  launchPair,
  type Net,
  type PairIds,
  resolveAmi,
  type Arch,
} from "./create-pair.ts";
import { checkPairOnce } from "./verify-pair.ts";

interface MatrixItem {
  base: string;
  os: string;
  version: string;
  arch: Arch;
  testerIp: string;
  testedIp: string;
}

const MATRIX: MatrixItem[] = [
  { base: "m-ub2404-x86", os: "ubuntu", version: "24.04", arch: "amd64", testerIp: "10.42.0.10", testedIp: "10.42.0.11" },
  { base: "m-ub2404-arm", os: "ubuntu", version: "24.04", arch: "arm64", testerIp: "10.42.0.12", testedIp: "10.42.0.13" },
  { base: "m-ub2604-x86", os: "ubuntu", version: "26.04", arch: "amd64", testerIp: "10.42.0.14", testedIp: "10.42.0.15" },
  { base: "m-ub2604-arm", os: "ubuntu", version: "26.04", arch: "arm64", testerIp: "10.42.0.16", testedIp: "10.42.0.17" },
  { base: "m-deb13-x86", os: "debian", version: "13", arch: "amd64", testerIp: "10.42.0.18", testedIp: "10.42.0.19" },
  { base: "m-deb13-arm", os: "debian", version: "13", arch: "arm64", testerIp: "10.42.0.20", testedIp: "10.42.0.21" },
  { base: "m-fed44-x86", os: "fedora", version: "44", arch: "amd64", testerIp: "10.42.0.22", testedIp: "10.42.0.23" },
  { base: "m-fed44-arm", os: "fedora", version: "44", arch: "arm64", testerIp: "10.42.0.24", testedIp: "10.42.0.25" },
  { base: "m-rocky10-x86", os: "rocky", version: "10", arch: "amd64", testerIp: "10.42.0.26", testedIp: "10.42.0.27" },
  { base: "m-rocky10-arm", os: "rocky", version: "10", arch: "arm64", testerIp: "10.42.0.28", testedIp: "10.42.0.29" },
];

interface PairResult {
  pair: string;
  base: string;
  arch: Arch;
  os: string;
  version: string;
  ok: boolean;
  attempts: number;
  line?: string;
  error?: string;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function withTimeout<T>(p: Promise<T>, ms: number, what: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error(`${what} timed out after ${ms}ms`)),
      ms,
    );
    p.then(
      (v) => {
        clearTimeout(timer);
        resolve(v);
      },
      (e) => {
        clearTimeout(timer);
        reject(e);
      },
    );
  });
}

async function powerOff(ips: string[], key: string): Promise<void> {
  await Promise.all(ips.map(async (ip) => {
    try {
      await new Deno.Command("ssh", {
        args: [
          "-i",
          key,
          "-o",
          "IdentitiesOnly=yes",
          "-o",
          "BatchMode=yes",
          "-o",
          "StrictHostKeyChecking=no",
          "-o",
          "UserKnownHostsFile=/dev/null",
          "-o",
          "ConnectTimeout=1",
          `root@${ip}`,
          "systemctl poweroff || poweroff",
        ],
        stdout: "null",
        stderr: "null",
        signal: AbortSignal.timeout(5_000),
      }).output();
    } catch {
      // ssh drops the connection as the host powers off
    }
  }));
}

interface SessionKey {
  priv: string;
  pub: string;
  cleanup: () => Promise<void>;
}

async function sessionKey(existing?: string): Promise<SessionKey> {
  if (existing) {
    const out = await new Deno.Command("ssh-keygen", {
      args: ["-y", "-f", existing],
      stdout: "piped",
      stderr: "piped",
    }).output();
    if (!out.success) throw new Error(`ssh-keygen -y failed for ${existing}`);
    return {
      priv: existing,
      pub: new TextDecoder().decode(out.stdout).trim(),
      cleanup: () => Promise.resolve(),
    };
  }
  const dir = await Deno.makeTempDir({ prefix: "ec2-pair-key-" });
  const priv = `${dir}/key`;
  const gen = await new Deno.Command("ssh-keygen", {
    args: ["-t", "ed25519", "-N", "", "-C", "ci-session", "-f", priv],
    stdout: "null",
    stderr: "null",
  }).output();
  if (!gen.success) throw new Error("ssh-keygen failed");
  return {
    priv,
    pub: (await Deno.readTextFile(`${priv}.pub`)).trim(),
    cleanup: async () => {
      await Deno.remove(dir, { recursive: true }).catch(() => {});
    },
  };
}

const cmd = command({
  name: "matrix",
  description: "Create, verify and destroy the EC2 OS/arch matrix.",
  args: {
    rig: option({ long: "rig", type: optional(string) }),
    suffix: option({ long: "suffix", type: optional(string) }),
    ttlMinutes: option({
      long: "ttl-minutes",
      type: number,
      defaultValue: () => 45,
    }),
    region: option({ long: "region", type: optional(string) }),
    sshKey: option({ long: "ssh-key", type: optional(string) }),
    vpcId: option({ long: "vpc-id", type: optional(string) }),
    subnetId: option({ long: "subnet-id", type: optional(string) }),
    sgId: option({ long: "sg-id", type: optional(string) }),
    vpcCidr: option({ long: "vpc-cidr", type: optional(string) }),
    subnetCidr: option({ long: "subnet-cidr", type: optional(string) }),
    only: option({
      long: "only",
      description: "comma-separated matrix base names to run",
      type: optional(string),
    }),
    checkConcurrency: option({
      long: "check-concurrency",
      type: number,
      defaultValue: () => 4,
    }),
    checkTimeout: option({
      long: "check-timeout-seconds",
      type: number,
      defaultValue: () => 25,
    }),
    maxAttempts: option({
      long: "max-attempts",
      type: number,
      defaultValue: () => 12,
    }),
    retryBackoff: option({
      long: "retry-backoff-seconds",
      type: number,
      defaultValue: () => 3,
    }),
    keep: flag({ long: "keep" }),
    keepPairs: flag({ long: "keep-pairs" }),
  },
  handler: async (a) => {
    const region = a.region ?? Deno.env.get("AWS_REGION") ??
      Deno.env.get("AWS_DEFAULT_REGION") ?? "eu-central-1";
    const ec2 = new EC2({ region, maxAttempts: 10, retryMode: "adaptive" });
    const suffix = a.suffix ?? Date.now().toString(36).slice(-4);
    const rig = a.rig ?? `rig-${suffix}`;
    const ttlMin = a.ttlMinutes;
    const key = await sessionKey(a.sshKey);
    const sshKeys = [...new Set([...DEFAULT_KEYS, key.pub])];
    const templates = {
      tester: await Deno.readTextFile(
        new URL("./tester.cloud-init.yaml", import.meta.url),
      ),
      tested: await Deno.readTextFile(
        new URL("./tested.cloud-init.yaml", import.meta.url),
      ),
    };
    const checkTimeoutMs = Math.max(5000, a.checkTimeout * 1000);
    const maxAttempts = Math.max(1, a.maxAttempts);
    const backoffMs = Math.max(0, a.retryBackoff * 1000);
    const checkConcurrency = Math.max(1, a.checkConcurrency);

    let net: Net | undefined;
    const created = new Set<string>();
    let cleaned = false;
    let aborted = false;

    const cleanup = async () => {
      if (cleaned) return;
      cleaned = true;
      for (const p of created) {
        await destroyPair(ec2, p).catch((e) =>
          console.error(`destroy ${p}: ${e.message}`)
        );
      }
      created.clear();
      if (net && !a.keep && !a.keepPairs) {
        await destroyPair(ec2, rig).catch((e) =>
          console.error(`destroy ${rig}: ${e.message}`)
        );
      }
      await key.cleanup();
    };

    const onSignal = () => {
      if (aborted) return;
      aborted = true;
      console.error("\nsignal received, cleaning up...");
      sleep(3000).then(cleanup).finally(() => Deno.exit(130));
    };
    Deno.addSignalListener("SIGINT", onSignal);
    Deno.addSignalListener("SIGTERM", onSignal);

    const results: PairResult[] = [];
    try {
      const selected = a.only
        ? MATRIX.filter((m) => a.only!.split(",").includes(m.base))
        : MATRIX;
      if (selected.length === 0) throw new Error("--only matched no matrix rows");

      net = await ensureNetwork(ec2, {
        pair: rig,
        ttlMin,
        vpcId: a.vpcId,
        subnetId: a.subnetId,
        sgId: a.sgId,
        vpcCidr: a.vpcCidr ?? "10.42.0.0/16",
        subnetCidr: a.subnetCidr ?? "10.42.0.0/24",
      });

      const amiCache = new Map<string, string>();
      const amiFor = async (
        os: string,
        version: string,
        arch: Arch,
      ): Promise<string> => {
        const k = `${os}:${version}:${arch}`;
        let v = amiCache.get(k);
        if (!v) {
          v = await resolveAmi(ec2, undefined, os, version, arch);
          amiCache.set(k, v);
        }
        return v;
      };

      // ── phase 1: launch all pairs (fast) ──────────────────────────────
      interface Launched {
        m: MatrixItem;
        pair: string;
        ids?: PairIds;
        error?: string;
      }
      const launched: Launched[] = [];
      for (const m of selected) {
        const pair = `${m.base}-${suffix}`;
        created.add(pair);
        if (aborted) {
          launched.push({ m, pair, error: "aborted" });
          continue;
        }
        try {
          const type = m.arch === "arm64" ? "t4g.micro" : "t3.micro";
          const testerAmi = await amiFor("ubuntu", "24.04", m.arch);
          const testedAmi = await amiFor(m.os, m.version, m.arch);
          const ids = await launchPair({
            ec2,
            pair,
            net,
            ttlMin,
            sshKeys,
            templates,
            tester: {
              osName: "ubuntu",
              version: "24.04",
              arch: m.arch,
              ip: m.testerIp,
              l2: "10.222.0.1/24",
              ami: testerAmi,
              instanceType: type,
            },
            tested: {
              osName: m.os,
              version: m.version,
              arch: m.arch,
              ip: m.testedIp,
              l2: "10.222.0.2/24",
              ami: testedAmi,
              instanceType: type,
            },
          });
          launched.push({ m, pair, ids });
          console.log(
            `launched ${m.base} (${pair}) private tester=${m.testerIp} tested=${m.testedIp}`,
          );
        } catch (e) {
          const error = e instanceof Error ? e.message : String(e);
          launched.push({ m, pair, error });
          console.log(`LAUNCH FAIL ${m.base}: ${error}`);
        }
      }

      // Give the just-launched instances a moment before the first check.
      if (!aborted && launched.length > 0) {
        console.log(`waiting 10s for ${launched.length} pair(s) to settle`);
        await sleep(10_000);
      }

      // ── phase 2: retry-queue scheduler until all pairs are settled ─────
      interface CheckState {
        item: Launched;
        attempts: number;
        state: "queued" | "ok" | "failed";
        nextAt: number;
        line?: string;
        error?: string;
        testerPublicIp?: string;
        testedPublicIp?: string;
        recorded: boolean;
      }
      const checks: CheckState[] = launched.map((item) => ({
        item,
        attempts: 0,
        state: item.error ? "failed" : "queued",
        nextAt: 0,
        error: item.error,
        recorded: false,
      }));
      const settle = async (c: CheckState) => {
        if (c.state === "queued") return;
        if (c.state === "ok" && !a.keepPairs) {
          if (c.testerPublicIp && c.testedPublicIp) {
            console.log(`poweroff ${c.item.m.base} (success)`);
            await powerOff([c.testerPublicIp, c.testedPublicIp], key.priv);
            await sleep(3000);
          }
        }
        if (!a.keepPairs) {
          await destroyPair(ec2, c.item.pair).catch((e) =>
            console.error(`destroy ${c.item.pair}: ${e.message}`)
          );
        }
        created.delete(c.item.pair);
        if (!c.recorded) {
          c.recorded = true;
          results.push({
            pair: c.item.pair,
            base: c.item.m.base,
            arch: c.item.m.arch,
            os: c.item.m.os,
            version: c.item.m.version,
            ok: c.state === "ok",
            attempts: c.attempts,
            line: c.line,
            error: c.error,
          });
          console.log(
            `${c.state === "ok" ? "OK  " : "FAIL"} ${c.item.m.base} ` +
              `(attempt ${c.attempts}) ${c.line ?? c.error ?? ""}`,
          );
        }
      };

      while (true) {
        if (aborted) break;
        const pending = checks.filter((c) => c.state === "queued");
        if (pending.length === 0) break;
        const now = Date.now();
        const ready = pending.filter((c) => c.nextAt <= now).slice(
          0,
          checkConcurrency,
        );
        if (ready.length === 0) {
          const next = Math.min(...pending.map((c) => c.nextAt));
          await sleep(Math.max(50, next - now));
          continue;
        }

        await Promise.all(ready.map(async (c) => {
          c.attempts += 1;
          try {
            const [tester, tested] = await withTimeout(
              Promise.all([
                instanceInfo(ec2, c.item.ids!.testerId, `${c.item.pair}-tester`),
                instanceInfo(ec2, c.item.ids!.testedId, `${c.item.pair}-tested`),
              ]),
              checkTimeoutMs,
              "instanceInfo",
            );
            c.testerPublicIp = tester.publicIpv4;
            c.testedPublicIp = tested.publicIpv4;
            console.log(
              `check ${c.item.m.base} attempt ${c.attempts}/${maxAttempts} ` +
                `tester=${tester.publicIpv4} tested=${tested.publicIpv4}`,
            );
            const r = await withTimeout(
              checkPairOnce({
                json: { pair: c.item.pair, tester, tested },
                sshKey: key.priv,
                readyTimeoutSec: Math.max(5, Math.floor(checkTimeoutMs / 2000)),
              }),
              checkTimeoutMs,
              "check",
            );
            c.line = r.line;
            if (r.ok) {
              c.state = "ok";
            } else {
              c.error = r.line;
              c.state = c.attempts >= maxAttempts ? "failed" : "queued";
              c.nextAt = Date.now() + backoffMs * c.attempts;
              if (c.state === "queued") {
                console.log(
                  `retry ${c.item.m.base} ${c.attempts}/${maxAttempts}: ${r.line}`,
                );
              }
            }
          } catch (e) {
            c.error = e instanceof Error ? e.message : String(e);
            c.state = c.attempts >= maxAttempts ? "failed" : "queued";
            c.nextAt = Date.now() + backoffMs * c.attempts;
            if (c.state === "queued") {
              console.log(
                `retry ${c.item.m.base} ${c.attempts}/${maxAttempts}: ${c.error}`,
              );
            }
          }
          await settle(c);
        }));
      }

      // record anything not settled (aborted / launch failures)
      for (const c of checks) {
        if (c.state === "queued") {
          c.state = "failed";
          c.error = c.error ?? "aborted";
        }
        await settle(c);
      }

    } finally {
      await cleanup();
      Deno.removeSignalListener("SIGINT", onSignal);
      Deno.removeSignalListener("SIGTERM", onSignal);
    }

    results.sort((x, y) => x.base.localeCompare(y.base));
    console.log(JSON.stringify({ rig, region, results }, null, 2));
    if (results.length === 0 || results.some((r) => !r.ok)) Deno.exit(1);
  },
});

if (import.meta.main) {
  await run(cmd, Deno.args);
}
