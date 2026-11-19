#!/usr/bin/env -S deno run -A
// matrix.ts — create + verify + destroy the OS/arch matrix in ONE process.
//
// One EC2 client with adaptive retries and a shared launch pacer, a bounded
// worker pool, and guaranteed teardown on success, failure, SIGINT or SIGTERM.
//
//   deno run -A scripts/ec2-pair/matrix.ts --concurrency 4
//   deno run -A scripts/ec2-pair/matrix.ts --rig rig-demo --ttl-minutes 30
//   deno run -A scripts/ec2-pair/matrix.ts --vpc-id vpc-… --subnet-id subnet-… --sg-id sg-…
//
// A generated session key is added beside the owner key in cloud-init; the
// verifier signs in with it. Prints a JSON summary; exits non-zero on failure.

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
  createPair,
  DEFAULT_KEY,
  destroyPair,
  ensureNetwork,
  type Net,
  resolveAmi,
  type Arch,
} from "./create-pair.ts";
import { verifyPair } from "./verify-pair.ts";

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
  line?: string;
  error?: string;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function pool<T>(
  items: T[],
  limit: number,
  fn: (item: T) => Promise<void>,
): Promise<void> {
  let next = 0;
  const n = Math.max(1, Math.min(limit, items.length));
  await Promise.all(
    Array.from({ length: n }, async () => {
      while (true) {
        const i = next++;
        if (i >= items.length) return;
        await fn(items[i]!);
      }
    }),
  );
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
    concurrency: option({
      long: "concurrency",
      type: number,
      defaultValue: () => 4,
    }),
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
    const sshKeys = [...new Set([DEFAULT_KEY, key.pub])];
    const templates = {
      tester: await Deno.readTextFile(
        new URL("./tester.cloud-init.yaml", import.meta.url),
      ),
      tested: await Deno.readTextFile(
        new URL("./tested.cloud-init.yaml", import.meta.url),
      ),
    };

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
      // grace: let an in-flight RunInstances land so cleanup can see it
      sleep(3000).then(cleanup).finally(() => Deno.exit(130));
    };
    Deno.addSignalListener("SIGINT", onSignal);
    Deno.addSignalListener("SIGTERM", onSignal);

    const results: PairResult[] = [];
    try {
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

      const selected = a.only
        ? MATRIX.filter((m) => a.only!.split(",").includes(m.base))
        : MATRIX;
      if (selected.length === 0) throw new Error("--only matched no matrix rows");
      await pool(selected, a.concurrency, async (m) => {
        const pair = `${m.base}-${suffix}`;
        created.add(pair);
        const type = m.arch === "arm64" ? "t4g.micro" : "t3.micro";
        let ok = false;
        let line: string | undefined;
        let error: string | undefined;
        try {
          if (aborted) throw new Error("aborted");
          const testerAmi = await amiFor("ubuntu", "24.04", m.arch);
          const testedAmi = await amiFor(m.os, m.version, m.arch);
          const info = await createPair({
            ec2,
            pair,
            net: net!,
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
          if (aborted) throw new Error("aborted");
          const v = await verifyPair({
            json: { pair, tester: info.tester, tested: info.tested },
            sshKey: key.priv,
            readyTimeoutSec: 480,
          });
          ok = v.ok;
          line = v.line;
        } catch (e) {
          error = e instanceof Error ? e.message : String(e);
        } finally {
          if (!a.keepPairs) {
            await destroyPair(ec2, pair).catch((e) =>
              console.error(`destroy ${pair}: ${e.message}`)
            );
          }
          created.delete(pair);
        }
        results.push({
          pair,
          base: m.base,
          arch: m.arch,
          os: m.os,
          version: m.version,
          ok,
          line,
          error,
        });
        console.log(`${ok ? "OK  " : "FAIL"} ${m.base} ${line ?? error ?? ""}`);
      });
    } finally {
      await cleanup();
      Deno.removeSignalListener("SIGINT", onSignal);
      Deno.removeSignalListener("SIGTERM", onSignal);
    }

    results.sort((x, y) => x.base.localeCompare(y.base));
    console.log(JSON.stringify({ rig, region, results }, null, 2));
    if (results.some((r) => !r.ok)) Deno.exit(1);
  },
});

if (import.meta.main) {
  await run(cmd, Deno.args);
}
