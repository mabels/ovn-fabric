#!/usr/bin/env -S deno run -A
// matrix.ts — create, verify, deploy and test the OS/arch matrix.
//
//   1. launch every pair (RunInstances only — fast), so all boot concurrently
//   2. verify the Geneve L2 + egress with a retry queue; keep successes
//   3. for every successful pair, in parallel:
//        - prepare the tester (segments + ISP --use-host)
//        - deploy the fabric to the tested host
//          (generate-ir | fabric-deploy.sh … ir-apply run --wait-background)
//        - run the post-deploy connectivity tests from the tester
//      streaming each command with a [<tester-name>] prefix
//   4. poweroff successful pairs, destroy the rest
//
//   deno run -A scripts/ec2-pair/matrix.ts
//   deno run -A scripts/ec2-pair/matrix.ts --only m-ub2404-x86
//   deno run -A scripts/ec2-pair/matrix.ts --skip-deploy   # transport only
//
// The fabric deploy needs the site secrets: set TOPOLOGY_SECRETS_TS, or pass
// --secrets <file>. The IR's chassis address is the tested host's *private*
// IP; SSH to the tested host uses its *public* IP.

import {
  command,
  flag,
  number,
  option,
  optional,
  run,
  string,
} from "npm:cmd-ts@0.13.0";
import {
  type EC2Client,
  EC2Client as EC2,
  TerminateInstancesCommand,
  waitUntilInstanceTerminated,
} from "npm:@aws-sdk/client-ec2@3";
import {
  DEFAULT_KEYS,
  destroyNetwork,
  destroyPair,
  ensureNetwork,
  findInstancesByPairs,
  type Info,
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
  // Ubuntu 24.04 (tested) is excluded: its OVN 24.03 rejects the addressless
  // inactive-WAN router port (`lrp-add` requires an address); OVN >=25.03
  // (Debian 13, Ubuntu 26.04) accepts it. Re-add with a newer OVN package set.
  { base: "m-ub2604-x86", os: "ubuntu", version: "26.04", arch: "amd64", testerIp: "10.42.0.14", testedIp: "10.42.0.15" },
  { base: "m-ub2604-arm", os: "ubuntu", version: "26.04", arch: "arm64", testerIp: "10.42.0.16", testedIp: "10.42.0.17" },
  { base: "m-deb13-x86", os: "debian", version: "13", arch: "amd64", testerIp: "10.42.0.18", testedIp: "10.42.0.19" },
  { base: "m-deb13-arm", os: "debian", version: "13", arch: "arm64", testerIp: "10.42.0.20", testedIp: "10.42.0.21" },
  { base: "m-fed44-x86", os: "fedora", version: "44", arch: "amd64", testerIp: "10.42.0.22", testedIp: "10.42.0.23" },
  { base: "m-fed44-arm", os: "fedora", version: "44", arch: "arm64", testerIp: "10.42.0.24", testedIp: "10.42.0.25" },
  { base: "m-rocky10-x86", os: "rocky", version: "10", arch: "amd64", testerIp: "10.42.0.26", testedIp: "10.42.0.27" },
  { base: "m-rocky10-arm", os: "rocky", version: "10", arch: "arm64", testerIp: "10.42.0.28", testedIp: "10.42.0.29" },
];

const POST_TEST = `
fail=0
run() {
  echo "=== $*"
  if "$@"; then echo "OK: $*"; else rc=$?; echo "FAIL($rc): $*"; fail=1; fi
}
echo "=== tester root"
run curl -fsS --max-time 15 -o /dev/null -w 'tester-https=%{http_code}\\n' https://www.google.com/
echo "=== ns-1129"
mkdir -p /etc/netns/ns-1129
printf 'nameserver 10.43.0.10\\n' > /etc/netns/ns-1129/resolv.conf
run ip netns exec ns-1129 ping -c 3 -W 2 192.168.129.1
run ip netns exec ns-1129 dig @10.43.0.10 www.geo.de
run ip netns exec ns-1129 curl -fsS --max-time 15 -o /dev/null -w 'ns-1129-https=%{http_code}\\n' https://www.google.com/
exit "$fail"
`;

interface PairResult {
  pair: string;
  base: string;
  arch: Arch;
  os: string;
  version: string;
  ok: boolean;
  attempts: number;
  verifyOk: boolean;
  deployOk: boolean;
  testOk: boolean;
  line?: string;
  error?: string;
  deployError?: string;
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

function addIp(base: string, n: number): string {
  const m = base.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  if (!m) throw new Error(`bad IPv4 ${base}`);
  const p = m.slice(1).map(Number) as [number, number, number, number];
  if (p.some((x) => x > 255)) throw new Error(`bad IPv4 ${base}`);
  const v = (((p[0] * 256 + p[1]) * 256 + p[2]) * 256 + p[3]) + n;
  if (v < 0 || v > 0xffffffff) throw new Error(`IPv4 overflow ${base}+${n}`);
  return [
    Math.floor(v / 16777216) % 256,
    Math.floor(v / 65536) % 256,
    Math.floor(v / 256) % 256,
    v % 256,
  ].join(".");
}

function semaphore(limit: number) {
  let active = 0;
  const waiters: Array<() => void> = [];
  return async <T>(fn: () => Promise<T>): Promise<T> => {
    if (active >= limit) await new Promise<void>((r) => waiters.push(r));
    active += 1;
    try {
      return await fn();
    } finally {
      active -= 1;
      waiters.shift()?.();
    }
  };
}

async function pump(
  stream: ReadableStream<Uint8Array>,
  prefix: string,
): Promise<void> {
  const dec = new TextDecoder();
  let buf = "";
  for await (const chunk of stream) {
    buf += dec.decode(chunk, { stream: true });
    let at = buf.indexOf("\n");
    while (at >= 0) {
      console.log(`${prefix} ${buf.slice(0, at)}`);
      buf = buf.slice(at + 1);
      at = buf.indexOf("\n");
    }
  }
  if (buf.length > 0) console.log(`${prefix} ${buf}`);
}

async function runStreamed(
  cmd: string,
  env: Record<string, string>,
  prefix: string,
): Promise<void> {
  const child = new Deno.Command("bash", {
    args: ["-c", cmd],
    cwd: Deno.cwd(),
    env,
    stdout: "piped",
    stderr: "piped",
  }).spawn();
  await Promise.all([pump(child.stdout, prefix), pump(child.stderr, prefix)]);
  const status = await child.status;
  if (!status.success) throw new Error(`command failed (${status.code})`);
}

async function sshStream(
  ip: string,
  key: string,
  remote: string,
  prefix: string,
  stdin?: string,
): Promise<void> {
  const child = new Deno.Command("ssh", {
    args: [
      "-i", key,
      "-o", "IdentitiesOnly=yes",
      "-o", "BatchMode=yes",
      "-o", "StrictHostKeyChecking=no",
      "-o", "UserKnownHostsFile=/dev/null",
      "-o", "ConnectTimeout=8",
      `root@${ip}`,
      remote,
    ],
    stdin: stdin === undefined ? "null" : "piped",
    stdout: "piped",
    stderr: "piped",
  }).spawn();
  if (stdin !== undefined) {
    const w = child.stdin.getWriter();
    await w.write(new TextEncoder().encode(stdin));
    await w.close();
  }
  await Promise.all([pump(child.stdout, prefix), pump(child.stderr, prefix)]);
  const status = await child.status;
  if (!status.success) throw new Error(`ssh ${ip} failed (${status.code})`);
}

async function sshOutput(
  ip: string,
  key: string,
  remote: string,
): Promise<string> {
  const res = await new Deno.Command("ssh", {
    args: [
      "-i", key,
      "-o", "IdentitiesOnly=yes",
      "-o", "BatchMode=yes",
      "-o", "StrictHostKeyChecking=no",
      "-o", "UserKnownHostsFile=/dev/null",
      "-o", "ConnectTimeout=8",
      `root@${ip}`,
      remote,
    ],
    stdout: "piped",
    stderr: "piped",
  }).output();
  const dec = new TextDecoder();
  if (!res.success) {
    throw new Error(`ssh ${ip} failed: ${dec.decode(res.stderr).trim()}`);
  }
  return dec.decode(res.stdout).trim();
}

async function scpTo(ip: string, key: string, files: string[]): Promise<void> {
  const res = await new Deno.Command("scp", {
    args: [
      "-i", key,
      "-o", "IdentitiesOnly=yes",
      "-o", "BatchMode=yes",
      "-o", "StrictHostKeyChecking=no",
      "-o", "UserKnownHostsFile=/dev/null",
      ...files,
      `root@${ip}:/tmp/`,
    ],
    stdout: "piped",
    stderr: "piped",
  }).output();
  if (!res.success) {
    throw new Error(
      `scp ${ip} failed: ${new TextDecoder().decode(res.stderr).trim()}`,
    );
  }
}

async function powerOff(ips: string[], key: string): Promise<void> {
  await Promise.all(ips.map(async (ip) => {
    try {
      await new Deno.Command("ssh", {
        args: [
          "-i", key,
          "-o", "IdentitiesOnly=yes",
          "-o", "BatchMode=yes",
          "-o", "StrictHostKeyChecking=no",
          "-o", "UserKnownHostsFile=/dev/null",
          "-o", "ConnectTimeout=1",
          `root@${ip}`,
          "systemctl poweroff || poweroff",
        ],
        stdout: "null",
        stderr: "null",
        signal: AbortSignal.timeout(5_000),
      }).output();
    } catch {
      // ssh drops as the host powers off
    }
  }));
}

function shq(text: string): string {
  return `'${text.replaceAll("'", `'\\''`)}'`;
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

/** ssh/scp wrappers on PATH: fabric-deploy.sh's ssh/scp have no -i, and
 * macOS ssh ignores $HOME when looking for ~/.ssh/config. */
async function sshWrappers(key: string): Promise<{
  dir: string;
  path: string;
  cleanup: () => Promise<void>;
}> {
  const dir = await Deno.makeTempDir({ prefix: "ec2-ssh-wrap-" });
  const opts = `-F /dev/null -i ${shq(key)} -o IdentitiesOnly=yes ` +
    `-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes`;
  for (
    const [name, real] of [
      ["ssh", "/usr/bin/ssh"],
      ["scp", "/usr/bin/scp"],
    ] as const
  ) {
    await Deno.writeTextFile(
      `${dir}/${name}`,
      `#!/bin/sh\nexec ${real} ${opts} "$@"\n`,
    );
    await Deno.chmod(`${dir}/${name}`, 0o755);
  }
  return {
    dir,
    path: `${dir}:${Deno.env.get("PATH") ?? ""}`,
    cleanup: async () => {
      await Deno.remove(dir, { recursive: true }).catch(() => {});
    },
  };
}

/** Batch teardown: terminate every instance of the pairs in one API call,
 * wait for them in parallel, then delete the network(s). */
async function teardownAll(
  ec2: EC2Client,
  pairs: readonly string[],
  rig: string,
  destroyRig: boolean,
): Promise<void> {
  const insts = await findInstancesByPairs(ec2, pairs);
  const live = insts.filter((i) =>
    !["terminated", "shutting-down"].includes(i.State?.Name ?? "")
  );
  const ids = live.map((i) => i.InstanceId!).filter(Boolean);
  if (ids.length > 0) {
    console.log(
      `teardown: terminating ${ids.length} instance(s) for ${pairs.length} pair(s)`,
    );
    await ec2.send(new TerminateInstancesCommand({ InstanceIds: ids }));
    await Promise.all(ids.map((id) =>
      waitUntilInstanceTerminated(
        { client: ec2, maxWaitTime: 300, minDelay: 3, maxDelay: 10 },
        { InstanceIds: [id] },
      )
    ));
  }
  await Promise.all(pairs.map((p) =>
    destroyNetwork(ec2, p).catch((e) =>
      console.error(`destroy network ${p}: ${e.message}`)
    )
  ));
  if (destroyRig) {
    await destroyPair(ec2, rig).catch((e) =>
      console.error(`destroy ${rig}: ${e.message}`)
    );
  }
}

const cmd = command({
  name: "matrix",
  description: "Create, verify, deploy and test the EC2 OS/arch matrix.",
  args: {
    rig: option({ long: "rig", type: optional(string) }),
    suffix: option({ long: "suffix", type: optional(string) }),
    ttlMinutes: option({
      long: "ttl-minutes",
      type: number,
      defaultValue: () => 60,
    }),
    region: option({ long: "region", type: optional(string) }),
    sshKey: option({ long: "ssh-key", type: optional(string) }),
    secrets: option({
      long: "secrets",
      description: "secrets file for generate-ir (else TOPOLOGY_SECRETS_TS)",
      type: optional(string),
    }),
    vpcId: option({ long: "vpc-id", type: optional(string) }),
    ipBase: option({
      long: "ip-base",
      description: "first private IPv4 for the pairs (reuse mode)",
      type: optional(string),
    }),
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
      description: "0 = unlimited",
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
    deployConcurrency: option({
      long: "deploy-concurrency",
      type: number,
      description: "0 = unlimited",
      defaultValue: () => 4,
    }),
    skipDeploy: flag({
      long: "skip-deploy",
      description: "stop after the transport checks (no fabric deploy)",
    }),
    keep: flag({ long: "keep" }),
    keepPairs: flag({ long: "keep-pairs" }),
    keepFailed: flag({
      long: "keep-failed",
      description: "leave failed pairs running for debugging",
    }),
    teardown: flag({
      long: "teardown",
      description: "terminate all pairs for --suffix and the --rig, then exit",
    }),
  },
  handler: async (a) => {
    const region = a.region ?? Deno.env.get("AWS_REGION") ??
      Deno.env.get("AWS_DEFAULT_REGION") ?? "eu-central-1";
    const ec2 = new EC2({ region, maxAttempts: 10, retryMode: "adaptive" });
    if (a.teardown) {
      if (!a.suffix) throw new Error("--teardown needs --suffix");
      const rig = a.rig ?? `rig-${a.suffix}`;
      const pairs = MATRIX.map((m) => `${m.base}-${a.suffix}`);
      await teardownAll(ec2, pairs, rig, true);
      console.log(JSON.stringify({ teardown: { rig, suffix: a.suffix, pairs } }, null, 2));
      return;
    }
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
    const wrap = await sshWrappers(key.priv);
    const irDir = await Deno.makeTempDir({ prefix: "ec2-ir-" });
    const checkConcurrency = a.checkConcurrency <= 0
      ? Number.POSITIVE_INFINITY
      : a.checkConcurrency;
    const deployConcurrency = a.deployConcurrency <= 0
      ? Number.POSITIVE_INFINITY
      : a.deployConcurrency;
    const checkTimeoutMs = Math.max(5000, a.checkTimeout * 1000);
    const maxAttempts = Math.max(1, a.maxAttempts);
    const backoffMs = Math.max(0, a.retryBackoff * 1000);

    let net: Net | undefined;
    const created = new Set<string>();
    let cleaned = false;
    let aborted = false;

    const cleanup = async () => {
      if (cleaned) return;
      cleaned = true;
      if (!a.keepPairs && !a.keepFailed) {
        const remaining = [...created];
        created.clear();
        await teardownAll(
          ec2,
          remaining,
          rig,
          net !== undefined && !a.keep,
        );
      } else {
        created.clear();
      }
      await wrap.cleanup();
      // keep the generated IRs when pairs are kept for debugging
      if (!a.keepPairs && !a.keepFailed) {
        await Deno.remove(irDir, { recursive: true }).catch(() => {});
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
      const only = a.only
        ?.split(",")
        .map((s) => s.trim())
        .filter((s) => s.length > 0);
      const selected = only && only.length > 0
        ? MATRIX.filter((m) =>
          only.some((o) => o === m.base || o.startsWith(`${m.base}-`))
        )
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
      const ipBase = a.ipBase;
      let rowIndex = 0;
      for (const m of selected) {
        const testerIp = ipBase ? addIp(ipBase, rowIndex * 2) : m.testerIp;
        const testedIp = ipBase ? addIp(ipBase, rowIndex * 2 + 1) : m.testedIp;
        rowIndex += 1;
        const pair = `${m.base}-${suffix}`;
        created.add(pair);
        if (aborted) {
          launched.push({ m, pair, error: "aborted" });
          continue;
        }
        try {
          const type = m.arch === "arm64" ? "t4g.micro" : "t3.micro";
          const testerAmi = await amiFor("ubuntu", "26.04", m.arch);
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
              version: "26.04",
              arch: m.arch,
              ip: testerIp,
              l2: "10.222.0.1/24",
              ami: testerAmi,
              instanceType: type,
            },
            tested: {
              osName: m.os,
              version: m.version,
              arch: m.arch,
              ip: testedIp,
              l2: "10.222.0.2/24",
              ami: testedAmi,
              instanceType: type,
            },
          });
          launched.push({ m, pair, ids });
          console.log(`launched ${m.base} (${pair})`);
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
        for (const l of launched) {
          if (!l.ids) continue;
          try {
            const [t, d] = await withTimeout(
              Promise.all([
                instanceInfo(ec2, l.ids.testerId, `${l.pair}-tester`),
                instanceInfo(ec2, l.ids.testedId, `${l.pair}-tested`),
              ]),
              5000,
              "public ip lookup",
            );
            console.log(
              `public ${l.m.base} tester=${t.publicIpv4} tested=${d.publicIpv4}`,
            );
          } catch {
            console.log(`public ${l.m.base} pending`);
          }
        }
      }

      // ── phase 2: verify; each success goes straight to a deploy slot ──
      interface CheckState {
        item: Launched;
        attempts: number;
        verifyOk: boolean;
        settled: boolean;
        nextAt: number;
        line?: string;
        error?: string;
        info?: { tester: Info; tested: Info };
        deployOk: boolean;
        testOk: boolean;
        deployError?: string;
        recorded: boolean;
      }
      const checks: CheckState[] = launched.map((item) => ({
        item,
        attempts: 0,
        verifyOk: false,
        settled: item.error !== undefined,
        nextAt: 0,
        error: item.error,
        deployOk: false,
        testOk: false,
        recorded: false,
      }));

      const record = (c: CheckState) => {
        if (c.recorded) return;
        c.recorded = true;
        results.push({
          pair: c.item.pair,
          base: c.item.m.base,
          arch: c.item.m.arch,
          os: c.item.m.os,
          version: c.item.m.version,
          ok: c.verifyOk && c.deployOk && c.testOk,
          attempts: c.attempts,
          verifyOk: c.verifyOk,
          deployOk: c.deployOk,
          testOk: c.testOk,
          line: c.line,
          error: c.error,
          deployError: c.deployError,
        });
      };

      // Pipeline: a verified pair starts prepare/deploy/test at once,
      // bounded by --deploy-concurrency, without waiting for the others.
      const deployTasks: Promise<void>[] = [];
      const deploySlot = semaphore(deployConcurrency);
      const pexPath = `${irDir}/fabric-${suffix}.pex`;
      let pexPromise: Promise<string> | undefined;
      const ensurePex = (): Promise<string> => {
        pexPromise ??= (async () => {
          console.log(`building runtime PEX once -> ${pexPath}`);
          await runStreamed(
            // use the build venv: fabric imports requirements (ip_address)
            `set -e; ` +
              `if [ ! -x .venv-build/bin/python ]; then ` +
              `python3 -m venv .venv-build; ` +
              `.venv-build/bin/pip install --quiet -r requirements.txt; fi; ` +
              `.venv-build/bin/python -m fabric bundle -o ${shq(pexPath)}`,
            Deno.env.toObject(),
            "[bundle]",
          );
          return pexPath;
        })();
        return pexPromise;
      };

      const finalizeVerifyOnly = async (c: CheckState): Promise<void> => {
        c.deployOk = true;
        c.testOk = true;
        if (!a.keepPairs) {
          await destroyPair(ec2, c.item.pair).catch((e) =>
            console.error(`destroy ${c.item.pair}: ${e.message}`)
          );
        }
        created.delete(c.item.pair);
        record(c);
        console.log(`OK (verify only) ${c.item.m.base}`);
      };

      const deployAndTest = async (c: CheckState): Promise<void> => {
        const info = c.info!;
        const prefix = `[${info.tester.hostname}]`;
        const log = (s: string) => console.log(`${prefix} ${s}`);
        const irFile = `${irDir}/${c.item.pair}.ir`;
        try {
          if (aborted) throw new Error("aborted");
          const pex = await ensurePex();
          // The apply service keys the live RawIR by socket.gethostname()
          // (the kernel hostname). Use that exact name as the IR host too.
          let chassisName = info.tested.hostname;
          try {
            const h = await sshOutput(
              info.tested.publicIpv4,
              key.priv,
              "hostname",
            );
            if (h) chassisName = h;
          } catch (e) {
            log(
              `hostname lookup failed, assuming ${chassisName}: ${
                e instanceof Error ? e.message : String(e)
              }`,
            );
          }
          log(`runtime hostname: ${chassisName}`);
          log("preparing tester (segments + ISP --use-host)");
          await scpTo(info.tester.publicIpv4, key.priv, [
            ".github/scripts/tester/test-isp.sh",
            ".github/scripts/tester/test-segments.sh",
          ]);
          await sshStream(
            info.tester.publicIpv4,
            key.priv,
            "chmod +x /tmp/test-isp.sh /tmp/test-segments.sh; " +
              "/tmp/test-segments.sh --trunk veth-l2 up; " +
              "/tmp/test-isp.sh --use-host --trunk veth-l2 up",
            prefix,
          );

          log("deploying fabric");
          const secretsArg = a.secrets
            ? ` --runtime ${shq(`secrets=${a.secrets}`)}`
            : "";
          const deployCmd =
            `deno run -A src/cli.ts generate-ir --runtime ctx=test` +
            ` --runtime ${shq(`chassis.name=${chassisName}`)}` +
            ` --runtime ${shq(`chassis.ipv4=${info.tested.privateIpv4}`)}` +
            secretsArg +
            ` examples/topology.cluster-tenants.ts` +
            ` | tee ${shq(irFile)}` +
            ` | PATH=${shq(wrap.path)} scripts/fabric-deploy.sh root@${info.tested.publicIpv4}` +
            ` --pex ${shq(pex)} ir-apply run --host ${shq(chassisName)} --no-cleanup --wait-background`;
          log(`deploy: ${deployCmd}`);
          await runStreamed(deployCmd, Deno.env.toObject(), prefix);

          log("post-deploy tests");
          let lastErr: unknown;
          for (let attempt = 1; attempt <= 3; attempt++) {
            try {
              await sshStream(
                info.tester.publicIpv4,
                key.priv,
                "sh -s",
                prefix,
                POST_TEST,
              );
              c.testOk = true;
              break;
            } catch (e) {
              lastErr = e;
              log(
                `test attempt ${attempt}/3 failed: ${
                  e instanceof Error ? e.message : String(e)
                }`,
              );
              await sleep(10_000);
            }
          }
          if (!c.testOk) throw lastErr ?? new Error("post-deploy tests failed");
          c.deployOk = true;
          c.deployError = undefined;
          log("OK (deploy + tests)");
        } catch (e) {
          c.deployOk = false;
          c.deployError = e instanceof Error ? e.message : String(e);
          log(`FAIL deploy/test: ${c.deployError}`);
        } finally {
          const succeeded = c.deployOk && c.testOk;
          if (succeeded && !a.keepPairs) {
            log("poweroff (success)");
            await powerOff(
              [info.tester.publicIpv4, info.tested.publicIpv4],
              key.priv,
            );
            await sleep(3000);
          }
          if (!a.keepPairs) {
            if (!succeeded && a.keepFailed) {
              log(
                `KEEP failed tester=${info.tester.publicIpv4} ` +
                  `tested=${info.tested.publicIpv4} ir=${irFile} ` +
                  `(ttl ${ttlMin}m)`,
              );
            } else {
              await destroyPair(ec2, c.item.pair).catch((e) =>
                log(`destroy: ${e.message}`)
              );
            }
          }
          created.delete(c.item.pair);
          record(c);
        }
      };

      const startPair = (c: CheckState) => {
        if (a.skipDeploy) {
          deployTasks.push(finalizeVerifyOnly(c));
        } else {
          deployTasks.push(deploySlot(() => deployAndTest(c)));
        }
      };

      while (true) {
        if (aborted) break;
        const pending = checks.filter((c) => !c.settled && !c.verifyOk);
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
            c.info = { tester, tested };
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
              c.verifyOk = true;
              c.error = undefined;
              c.info = { tester, tested };
              console.log(`VERIFIED ${c.item.m.base} ${r.line}`);
              startPair(c);
            } else {
              c.error = r.line;
              if (c.attempts >= maxAttempts) {
                c.settled = true;
                console.log(
                  `FAIL verify ${c.item.m.base} (attempt ${c.attempts}) ${r.line}`,
                );
              } else {
                c.nextAt = Date.now() + backoffMs * c.attempts;
                console.log(
                  `retry ${c.item.m.base} ${c.attempts}/${maxAttempts}: ${r.line}`,
                );
              }
            }
          } catch (e) {
            c.error = e instanceof Error ? e.message : String(e);
            if (c.attempts >= maxAttempts) {
              c.settled = true;
              console.log(`FAIL verify ${c.item.m.base}: ${c.error}`);
            } else {
              c.nextAt = Date.now() + backoffMs * c.attempts;
              console.log(
                `retry ${c.item.m.base} ${c.attempts}/${maxAttempts}: ${c.error}`,
              );
            }
          }
        }));
      }

      for (const c of checks) {
        if (!c.verifyOk) c.settled = true;
      }
      // Destroy pairs that failed verification / never launched.
      for (const c of checks) {
        if (!c.verifyOk) {
          if (a.keepPairs || a.keepFailed) {
            console.log(
              `KEEP failed-verify ${c.item.m.base} ` +
                `tester=${c.info?.tester.publicIpv4 ?? "?"} ` +
                `tested=${c.info?.tested.publicIpv4 ?? "?"} (ttl ${ttlMin}m)`,
            );
          } else {
            await destroyPair(ec2, c.item.pair).catch((e) =>
              console.error(`destroy ${c.item.pair}: ${e.message}`)
            );
          }
          created.delete(c.item.pair);
          record(c);
        }
      }

      await Promise.all(deployTasks);

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
