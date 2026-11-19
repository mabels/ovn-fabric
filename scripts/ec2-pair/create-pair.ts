#!/usr/bin/env -S deno run -A
// create-pair.ts — one EC2 "pair" for the QEMU/OVS smoke test, no IaC stack.
//
// Creates (by default) a throwaway dual-stack VPC with an IPv6 /56, a /24
// subnet, an internet gateway, and a security group exposing SSH on both
// 0.0.0.0/0 and ::/0. Each instance gets a fixed private IPv4, a public IPv4
// and a public IPv6, and the same Geneve bridge:
//
//     [pair]-tester  <-- br-gen / geneve udp/6081 -->  [pair]-tested
//
// The tested side defaults to Ubuntu 24.04 x86_64; tester and tested OS,
// version and arch are selectable (ubuntu|debian|rocky|fedora). Both
// instances self-destruct after --ttl-minutes (default 60) and terminate on
// any OS shutdown (InstanceInitiatedShutdownBehavior=terminate).
//
//   deno run -A scripts/ec2-pair/create-pair.ts --pair demo1
//   deno run -A scripts/ec2-pair/create-pair.ts --pair demo1 \
//     --tested-os rocky --tested-version 10
//   deno run -A scripts/ec2-pair/create-pair.ts --pair demo1 --destroy
//   deno run -A scripts/ec2-pair/create-pair.ts --reap
//
// Output (create): JSON with each host's hostname, private IPv4, public IPv4
// and public IPv6.

import {
  array,
  command,
  flag,
  multioption,
  number,
  option,
  optional,
  run,
  string,
} from "npm:cmd-ts@0.13.0";

import { IPv4, IPv6 } from "../../src/ip.ts";
import {
  AttachInternetGatewayCommand,
  AuthorizeSecurityGroupIngressCommand,
  CreateInternetGatewayCommand,
  CreateRouteCommand,
  CreateSecurityGroupCommand,
  CreateSubnetCommand,
  CreateVpcCommand,
  DeleteInternetGatewayCommand,
  DeleteSecurityGroupCommand,
  DeleteSubnetCommand,
  DeleteVpcCommand,
  DescribeImagesCommand,
  DescribeInstancesCommand,
  DescribeInternetGatewaysCommand,
  DescribeRouteTablesCommand,
  DescribeSecurityGroupsCommand,
  DescribeSubnetsCommand,
  DescribeVpcsCommand,
  DetachInternetGatewayCommand,
  type EC2Client,
  EC2Client as EC2,
  type Instance,
  ModifySubnetAttributeCommand,
  RunInstancesCommand,
  type RunInstancesCommandInput,
  TerminateInstancesCommand,
  waitUntilInstanceRunning,
  waitUntilInstanceTerminated,
} from "npm:@aws-sdk/client-ec2@3";

export type Arch = "amd64" | "arm64";
export type Role = "tester" | "tested";

export const PAIR_TAG = "ovn-fabric-pair";
const ARCHES: readonly Arch[] = ["amd64", "arm64"];
const OS_NAMES = ["ubuntu", "debian", "rocky", "fedora"] as const;

const OS_IMAGES: Record<string, { owner: string; pattern: RegExp }> = {
  ubuntu: { owner: "099720109477", pattern: /ubuntu-.*-server-/i },
  debian: { owner: "136693071363", pattern: /^debian-/i },
  rocky: { owner: "792107900819", pattern: /^Rocky-/i },
  fedora: { owner: "125523088429", pattern: /^Fedora-/i },
};

export const DEFAULT_KEY =
  "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAACAQDIQpC2scaVXEaNuwtq4n6Vtht2WHYxtDFKe44JNFEsZGyQjyL9c2qkmQQGCF+2g3HrIPDTCCCWQ3GUiXGAlQ0/rf6sLqcm4YMXt+hgHU5VeciUIDEySCKdCPC419wFPBw6oKdcN1pLoIdWoF4LRDcjcrKKAlkdNJ/oLnl716piLdchABO9NXGxBpkLsJGK8qw390O1ZqZMe9wEAL9l/A1/49v8LfzELp0/fhSmiXphTVI/zNVIp/QIytXzRg74xcYpBjHk1TQZHuz/HYYsWwccnu7vYaTDX0CCoAyEt599f9u+JQ4oW0qyLO0ie7YcmR6nGEW4DMsPcfdqqo2VyYy4ix3U5RI2JcObfP0snYwPtAdVeeeReXi3c/E7bGLeCcwdFeFBfHSA9PDGxWVlxh/oCJaE7kP7eBhXNjN05FodVdNczKI5T9etfQ9VHILFrvpEREg1+OTiI58RmwjxS5ThloqXvr/nZzhIwTsED0KNW8wE4pjyotDJ8jaW2d7oVIMdWqE2M9Z1sLqDDdhHdVMFxk6Hl2XfqeqO2Jnst7qzbHAN/S3hvSwysixWJEcLDVG+cg1KRwz4qafCU5oHSp8aNNOk4RZozboFjac17nOmfPfnjC/LLayjSkEBZ+eFi+njZRLDN92k3PvHYFEB3USbHYzICsuDcf+L4cslX03g7w== men.abels@adviser.com";

// ── helpers ──────────────────────────────────────────────────────────

function b64(s: string): string {
  const bytes = new TextEncoder().encode(s);
  let bin = "";
  for (const byte of bytes) bin += String.fromCharCode(byte);
  return btoa(bin);
}

function render(tmpl: string, vars: Record<string, string>): string {
  let out = tmpl;
  for (const [k, v] of Object.entries(vars)) out = out.split(`__${k}__`).join(v);
  const left = out.match(/__[A-Z0-9_]+__/);
  if (left) throw new Error(`unreplaced token ${left[0]}`);
  return out;
}

export function tags(value: string, role: string): { Key: string; Value: string }[] {
  return [
    { Key: PAIR_TAG, Value: value },
    { Key: "Pair", Value: value },
    { Key: "Role", Value: role },
    { Key: "ManagedBy", Value: "ovn-fabric/scripts/ec2-pair" },
  ];
}

// RunInstances is rate-limited per account; parallel matrix jobs hit
// RequestLimitExceeded. Retry only the throttle-shaped errors.
export async function withThrottleRetry<T>(
  fn: () => Promise<T>,
  attempts = 8,
  baseDelayMs = 5000,
): Promise<T> {
  for (let i = 0; i < attempts; i++) {
    try {
      return await fn();
    } catch (e) {
      const name = (e as { name?: string }).name ?? "";
      const throttle = name === "RequestLimitExceeded" ||
        name === "Throttling" || name === "InsufficientInstanceCapacity";
      if (throttle && i < attempts - 1) {
        const backoff = Math.min(60_000, baseDelayMs * 2 ** i);
        const jitter = Math.floor(Math.random() * 1000);
        await new Promise((r) => setTimeout(r, backoff + jitter));
        continue;
      }
      throw e;
    }
  }
  throw new Error("withThrottleRetry: unreachable");
}

async function retry<T>(
  fn: () => Promise<T>,
  attempts = 30,
  delayMs = 2000,
): Promise<T> {
  let last: unknown;
  for (let i = 0; i < attempts; i++) {
    try {
      return await fn();
    } catch (e) {
      last = e;
      await new Promise((r) => setTimeout(r, delayMs));
    }
  }
  throw last;
}

export function deriveVni(name: string): number {
  let h = 2166136261;
  for (const c of name) h = Math.imul(h ^ c.charCodeAt(0), 16777619);
  return (h >>> 0) % 16777215 || 1;
}

function pickArch(value: string, flagName: string): Arch {
  if (!ARCHES.includes(value as Arch)) {
    throw new Error(`${flagName}: expected one of ${ARCHES.join(", ")}`);
  }
  return value as Arch;
}

function pickOs(value: string, flagName: string): string {
  if (!OS_NAMES.includes(value as typeof OS_NAMES[number])) {
    throw new Error(`${flagName}: expected one of ${OS_NAMES.join(", ")}`);
  }
  return value;
}

export async function resolveAmi(
  ec2: EC2Client,
  image: string | undefined,
  osName: string,
  version: string,
  arch: Arch,
): Promise<string> {
  if (image) return image;
  const meta = OS_IMAGES[osName];
  if (!meta) throw new Error(`unknown OS ${osName}`);
  const imageArch = arch === "arm64" ? "arm64" : "x86_64";
  const r = await ec2.send(
    new DescribeImagesCommand({
      Owners: [meta.owner],
      Filters: [
        { Name: "state", Values: ["available"] },
        { Name: "architecture", Values: [imageArch] },
      ],
    }),
  );
  const vre = new RegExp(version.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const matches = (r.Images ?? []).filter((i) =>
    meta.pattern.test(i.Name ?? "") && vre.test(i.Name ?? "")
  );
  const sorted = matches.toSorted((a, b) =>
    (b.CreationDate ?? "").localeCompare(a.CreationDate ?? "")
  );
  const id = sorted[0]?.ImageId;
  if (!id) {
    const sample = (r.Images ?? []).slice(0, 6).map((i) => i.Name).join(", ");
    throw new Error(
      `no ${osName} ${version} ${arch} AMI found; candidates: ${sample}`,
    );
  }
  return id;
}

async function subnetVpc(ec2: EC2Client, subnetId: string): Promise<string> {
  const r = await ec2.send(
    new DescribeSubnetsCommand({ SubnetIds: [subnetId] }),
  );
  const vpc = r.Subnets?.[0]?.VpcId;
  if (!vpc) throw new Error(`subnet ${subnetId} not found`);
  return vpc;
}

async function vpcIpv6(ec2: EC2Client, vpcId: string): Promise<string> {
  for (let i = 0; i < 30; i++) {
    const r = await ec2.send(new DescribeVpcsCommand({ VpcIds: [vpcId] }));
    const assoc = r.Vpcs?.[0]?.Ipv6CidrBlockAssociationSet;
    const block = assoc?.find((a) =>
      a.Ipv6CidrBlockState?.State === "associated"
    )?.Ipv6CidrBlock;
    if (block) {
      IPv6.parse(block); // validate via ovn-fabric's wrapper
      return block;
    }
    await new Promise((res) => setTimeout(res, 2000));
  }
  throw new Error(`VPC ${vpcId} got no IPv6 /56 from AWS`);
}

async function createSecurityGroup(
  ec2: EC2Client,
  vpcId: string,
  pair: string,
  expiresAt: string,
): Promise<string> {
  const name = `ovn-fabric-pair-${pair}-${Date.now().toString(36)}`.slice(0, 255);
  const r = await ec2.send(
    new CreateSecurityGroupCommand({
      GroupName: name,
      Description: "ovn-fabric QEMU/OVS pair (SSH dual-stack)",
      VpcId: vpcId,
      TagSpecifications: [{
        ResourceType: "security-group",
        Tags: [...tags(pair, "sg"), { Key: "ExpiresAt", Value: expiresAt }],
      }],
    }),
  );
  const sgId = r.GroupId!;
  await ec2.send(
    new AuthorizeSecurityGroupIngressCommand({
      GroupId: sgId,
      IpPermissions: [
        {
          IpProtocol: "tcp",
          FromPort: 22,
          ToPort: 22,
          IpRanges: [{ CidrIp: "0.0.0.0/0", Description: "ssh over ipv4" }],
        },
        {
          IpProtocol: "tcp",
          FromPort: 22,
          ToPort: 22,
          Ipv6Ranges: [{ CidrIpv6: "::/0", Description: "ssh over ipv6" }],
        },
        {
          IpProtocol: "icmpv6",
          FromPort: -1,
          ToPort: -1,
          Ipv6Ranges: [{ CidrIpv6: "::/0", Description: "icmpv6 / PMTU" }],
        },
        {
          IpProtocol: "-1",
          UserIdGroupPairs: [{
            GroupId: sgId,
            Description: "pair internal: geneve udp/6081 + tests",
          }],
        },
      ],
    }),
  );
  return sgId;
}

export interface Net {
  vpcId: string;
  subnetId: string;
  sgId: string;
  created: boolean;
}

export interface NetworkOpts {
  pair: string;
  ttlMin: number;
  vpcId?: string;
  subnetId?: string;
  sgId?: string;
  vpcCidr: string;
  subnetCidr: string;
}

export async function ensureNetwork(
  ec2: EC2Client,
  o: NetworkOpts,
): Promise<Net> {
  if (o.subnetId) {
    const vpcId = o.vpcId ?? await subnetVpc(ec2, o.subnetId);
    const expiresAt = new Date(Date.now() + o.ttlMin * 60_000).toISOString();
    const sgId = o.sgId ?? await createSecurityGroup(ec2, vpcId, o.pair, expiresAt);
    return { vpcId, subnetId: o.subnetId, sgId, created: false };
  }

  IPv4.parse(o.vpcCidr);
  IPv4.parse(o.subnetCidr);
  const expiresAt = new Date(Date.now() + o.ttlMin * 60_000).toISOString();

  const vpcRes = await ec2.send(
    new CreateVpcCommand({
      CidrBlock: o.vpcCidr,
      AmazonProvidedIpv6CidrBlock: true,
      TagSpecifications: [{
        ResourceType: "vpc",
        Tags: [...tags(o.pair, "vpc"), { Key: "ExpiresAt", Value: expiresAt }],
      }],
    }),
  );
  const vpcId = vpcRes.Vpc?.VpcId;
  if (!vpcId) throw new Error("CreateVpc returned no VpcId");

  const v6 = await vpcIpv6(ec2, vpcId);
  const subnetV6 = `${v6.split("/")[0]}/64`;

  const subRes = await ec2.send(
    new CreateSubnetCommand({
      VpcId: vpcId,
      CidrBlock: o.subnetCidr,
      Ipv6CidrBlock: subnetV6,
      TagSpecifications: [{
        ResourceType: "subnet",
        Tags: [...tags(o.pair, "sn"), { Key: "ExpiresAt", Value: expiresAt }],
      }],
    }),
  );
  const subnetId = subRes.Subnet?.SubnetId;
  if (!subnetId) throw new Error("CreateSubnet returned no SubnetId");
  await ec2.send(
    new ModifySubnetAttributeCommand({
      SubnetId: subnetId,
      MapPublicIpOnLaunch: { Value: true },
    }),
  );
  await ec2.send(
    new ModifySubnetAttributeCommand({
      SubnetId: subnetId,
      AssignIpv6AddressOnCreation: { Value: true },
    }),
  );

  const igwRes = await ec2.send(
    new CreateInternetGatewayCommand({
      TagSpecifications: [{
        ResourceType: "internet-gateway",
        Tags: [...tags(o.pair, "igw"), { Key: "ExpiresAt", Value: expiresAt }],
      }],
    }),
  );
  const igwId = igwRes.InternetGateway?.InternetGatewayId;
  if (!igwId) throw new Error("CreateInternetGateway returned no id");
  await ec2.send(
    new AttachInternetGatewayCommand({ InternetGatewayId: igwId, VpcId: vpcId }),
  );

  const rt = await ec2.send(
    new DescribeRouteTablesCommand({
      Filters: [
        { Name: "vpc-id", Values: [vpcId] },
        { Name: "association.main", Values: ["true"] },
      ],
    }),
  );
  const rtId = rt.RouteTables?.[0]?.RouteTableId;
  if (!rtId) throw new Error("no main route table");
  await ec2.send(
    new CreateRouteCommand({
      RouteTableId: rtId,
      DestinationIpv6CidrBlock: "::/0",
      GatewayId: igwId,
    }),
  );
  await ec2.send(
    new CreateRouteCommand({
      RouteTableId: rtId,
      DestinationCidrBlock: "0.0.0.0/0",
      GatewayId: igwId,
    }),
  );

  const sgId = await createSecurityGroup(ec2, vpcId, o.pair, expiresAt);
  return { vpcId, subnetId, sgId, created: true };
}

export interface LaunchArgs {
  ec2: EC2Client;
  pair: string;
  name: string;
  role: Role;
  ip: string;
  l2: string;
  peerIp: string;
  ami: string;
  instanceType: string;
  osName: string;
  version: string;
  arch: Arch;
  net: Net;
  vni: number;
  sshKeys: string[];
  ttlMin: number;
  template: string;
}

// App-level pacing shared by every launch in this process. The SDK's adaptive
// retry also rate-limits; this keeps bursts from many pairs from synchronising.
let lastLaunchAt = 0;
const LAUNCH_MIN_INTERVAL_MS = Number(
  Deno.env.get("EC2_LAUNCH_INTERVAL_MS") ?? "500",
);
export async function paceLaunch(): Promise<void> {
  const now = Date.now();
  const at = Math.max(now, lastLaunchAt + LAUNCH_MIN_INTERVAL_MS);
  lastLaunchAt = at;
  if (at > now) await new Promise((r) => setTimeout(r, at - now));
}

export async function launch(a: LaunchArgs): Promise<string> {
  await paceLaunch();
  const userData = render(a.template, {
    HOSTNAME: a.name,
    SSH_KEYS: a.sshKeys.map((k) => `      - ${k}`).join("\n"),
    SSH_KEYS_BLOCK: a.sshKeys.map((k) => `      ${k}`).join("\n"),
    SELF_IP: a.ip,
    PEER_IP: a.peerIp,
    SELF_L2: a.l2,
    VNI: String(a.vni),
    TTL: `${a.ttlMin}min`,
    TTL_MIN: String(a.ttlMin),
  });
  const r = await withThrottleRetry(() =>
    a.ec2.send(
      new RunInstancesCommand({
      ImageId: a.ami,
      InstanceType: a.instanceType as RunInstancesCommandInput["InstanceType"],
      MinCount: 1,
      MaxCount: 1,
      SubnetId: a.net.subnetId,
      PrivateIpAddress: a.ip,
      SecurityGroupIds: [a.net.sgId],
      UserData: b64(userData),
      Ipv6AddressCount: 1,
      InstanceInitiatedShutdownBehavior: "terminate",
      TagSpecifications: [{
        ResourceType: "instance",
        Tags: [
          ...tags(a.pair, a.role),
          { Key: "Name", Value: `${a.pair}--${a.role}` },
          { Key: "Os", Value: a.osName },
          { Key: "Version", Value: a.version },
          { Key: "Arch", Value: a.arch },
          {
            Key: "ExpiresAt",
            Value: new Date(Date.now() + a.ttlMin * 60_000).toISOString(),
          },
        ],
      }],
      }),
    )
  );
  const id = r.Instances?.[0]?.InstanceId;
  if (!id) throw new Error("RunInstances returned no InstanceId");
  return id;
}

export interface Info {
  hostname: string;
  instanceId: string;
  privateIpv4: string;
  publicIpv4: string;
  publicIpv6: string;
}

export async function instanceInfo(
  ec2: EC2Client,
  id: string,
  hostname: string,
): Promise<Info> {
  const d = await ec2.send(new DescribeInstancesCommand({ InstanceIds: [id] }));
  const inst = d.Reservations?.[0]?.Instances?.[0];
  if (!inst) throw new Error(`instance ${id} not found`);
  const v6 = inst.Ipv6Address ??
    inst.NetworkInterfaces?.[0]?.Ipv6Addresses?.[0]?.Ipv6Address;
  if (!v6) {
    throw new Error(
      `instance ${id} has no public IPv6 — is the subnet dual-stack?`,
    );
  }
  IPv6.parse(v6);
  const v4 = inst.PrivateIpAddress;
  if (!v4) throw new Error(`instance ${id} has no private IPv4`);
  const pub4 = inst.PublicIpAddress;
  if (!pub4) {
    throw new Error(
      `instance ${id} has no public IPv4 — is MapPublicIpOnLaunch enabled?`,
    );
  }
  return {
    hostname,
    instanceId: id,
    privateIpv4: v4,
    publicIpv4: pub4,
    publicIpv6: v6,
  };
}

export interface RoleSpec {
  osName: string;
  version: string;
  arch: Arch;
  ip: string;
  l2: string;
  ami: string;
  instanceType: string;
}

export interface CreatePairOptions {
  ec2: EC2Client;
  pair: string;
  tester: RoleSpec;
  tested: RoleSpec;
  net: Net;
  ttlMin: number;
  sshKeys: string[];
  templates: { tester: string; tested: string };
}

/** Launch the tester+tested instances, wait until running, return their info. */
export async function createPair(
  o: CreatePairOptions,
): Promise<{ tester: Info; tested: Info }> {
  const vni = deriveVni(o.pair);
  const testerId = await launch({
    ec2: o.ec2,
    pair: o.pair,
    name: `${o.pair}-tester`,
    role: "tester",
    ip: o.tester.ip,
    l2: o.tester.l2,
    peerIp: o.tested.ip,
    ami: o.tester.ami,
    instanceType: o.tester.instanceType,
    osName: o.tester.osName,
    version: o.tester.version,
    arch: o.tester.arch,
    net: o.net,
    vni,
    sshKeys: o.sshKeys,
    ttlMin: o.ttlMin,
    template: o.templates.tester,
  });
  const testedId = await launch({
    ec2: o.ec2,
    pair: o.pair,
    name: `${o.pair}-tested`,
    role: "tested",
    ip: o.tested.ip,
    l2: o.tested.l2,
    peerIp: o.tester.ip,
    ami: o.tested.ami,
    instanceType: o.tested.instanceType,
    osName: o.tested.osName,
    version: o.tested.version,
    arch: o.tested.arch,
    net: o.net,
    vni,
    sshKeys: o.sshKeys,
    ttlMin: o.ttlMin,
    template: o.templates.tested,
  });

  await Promise.all(
    [testerId, testedId].map((id) =>
      waitUntilInstanceRunning(
        { client: o.ec2, maxWaitTime: 300, minDelay: 5, maxDelay: 20 },
        { InstanceIds: [id] },
      )
    ),
  );

  const [tester, tested] = await Promise.all([
    instanceInfo(o.ec2, testerId, `${o.pair}-tester`),
    instanceInfo(o.ec2, testedId, `${o.pair}-tested`),
  ]);
  return { tester, tested };
}

export async function findInstances(
  ec2: EC2Client,
  pair?: string,
): Promise<Instance[]> {
  const out: Instance[] = [];
  let next: string | undefined;
  do {
    const r = await ec2.send(
      new DescribeInstancesCommand({
        Filters: pair
          ? [{ Name: `tag:${PAIR_TAG}`, Values: [pair] }]
          : [{ Name: "tag-key", Values: [PAIR_TAG] }],
        NextToken: next,
      }),
    );
    for (const res of r.Reservations ?? []) out.push(...(res.Instances ?? []));
    next = r.NextToken;
  } while (next);
  return out;
}

function pairOf(inst: Instance): string | undefined {
  return inst.Tags?.find((t) => t.Key === PAIR_TAG)?.Value;
}

export async function destroyPair(ec2: EC2Client, pair: string): Promise<void> {
  const insts = await findInstances(ec2, pair);
  const live = insts.filter((i) =>
    !["terminated", "shutting-down"].includes(i.State?.Name ?? "")
  );
  if (live.length) {
    const ids = live.map((i) => i.InstanceId!).filter(Boolean);
    await ec2.send(new TerminateInstancesCommand({ InstanceIds: ids }));
    await Promise.all(ids.map((id) =>
      waitUntilInstanceTerminated(
        { client: ec2, maxWaitTime: 300, minDelay: 5, maxDelay: 20 },
        { InstanceIds: [id] },
      )
    ));
  }

  // VPCs this script created (tagged with the pair); reuse-mode networks are
  // left alone.
  const vpcs = await ec2.send(
    new DescribeVpcsCommand({
      Filters: [{ Name: `tag:${PAIR_TAG}`, Values: [pair] }],
    }),
  );
  for (const vpc of vpcs.Vpcs ?? []) {
    const vpcId = vpc.VpcId!;
    const sgs = await ec2.send(
      new DescribeSecurityGroupsCommand({
        Filters: [{ Name: "vpc-id", Values: [vpcId] }],
      }),
    );
    for (const sg of sgs.SecurityGroups ?? []) {
      if (sg.GroupName === "default") continue;
      await retry(() =>
        ec2.send(new DeleteSecurityGroupCommand({ GroupId: sg.GroupId! }))
      );
    }
    const subs = await ec2.send(
      new DescribeSubnetsCommand({
        Filters: [{ Name: "vpc-id", Values: [vpcId] }],
      }),
    );
    for (const s of subs.Subnets ?? []) {
      await retry(() =>
        ec2.send(new DeleteSubnetCommand({ SubnetId: s.SubnetId! }))
      );
    }
    const igws = await ec2.send(
      new DescribeInternetGatewaysCommand({
        Filters: [{ Name: "attachment.vpc-id", Values: [vpcId] }],
      }),
    );
    for (const igw of igws.InternetGateways ?? []) {
      await retry(() =>
        ec2.send(new DetachInternetGatewayCommand({
          InternetGatewayId: igw.InternetGatewayId!,
          VpcId: vpcId,
        }))
      );
      await retry(() =>
        ec2.send(new DeleteInternetGatewayCommand({
          InternetGatewayId: igw.InternetGatewayId!,
        }))
      );
    }
    await retry(() => ec2.send(new DeleteVpcCommand({ VpcId: vpcId })));
  }

  // Any tagged SGs left (reuse-mode network): best effort.
  const sgs = await ec2.send(
    new DescribeSecurityGroupsCommand({
      Filters: [{ Name: `tag:${PAIR_TAG}`, Values: [pair] }],
    }),
  );
  for (const sg of sgs.SecurityGroups ?? []) {
    await retry(() =>
      ec2.send(new DeleteSecurityGroupCommand({ GroupId: sg.GroupId! }))
    );
  }
}

export async function reap(ec2: EC2Client): Promise<string[]> {
  const all = await findInstances(ec2);
  const now = Date.now();
  const expired = all.filter((i) => {
    const state = i.State?.Name ?? "";
    if (["terminated", "shutting-down"].includes(state)) return false;
    const exp = i.Tags?.find((t) => t.Key === "ExpiresAt")?.Value;
    const past = exp ? Date.parse(exp) < now : false;
    return past || state === "stopped" || state === "stopping";
  });
  const ids = expired.map((i) => i.InstanceId!).filter(Boolean);
  if (ids.length) {
    await ec2.send(new TerminateInstancesCommand({ InstanceIds: ids }));
  }
  const pairs = new Set(
    expired.map(pairOf).filter((p): p is string => !!p),
  );

  // A cancelled run can leave the base network (and a pair's instances gone)
  // behind. Destroy a tagged VPC whose ExpiresAt has passed and that has no
  // live instances left.
  const vpcs = await ec2.send(
    new DescribeVpcsCommand({
      Filters: [{ Name: "tag-key", Values: [PAIR_TAG] }],
    }),
  );
  for (const vpc of vpcs.Vpcs ?? []) {
    const pair = vpc.Tags?.find((t) => t.Key === PAIR_TAG)?.Value;
    const exp = vpc.Tags?.find((t) => t.Key === "ExpiresAt")?.Value;
    if (!pair || !exp || Date.parse(exp) >= now) continue;
    const live = (await findInstances(ec2, pair)).some((i) =>
      !["terminated", "shutting-down"].includes(i.State?.Name ?? "")
    );
    if (!live) pairs.add(pair);
  }
  return [...pairs];
}

// ── command ──────────────────────────────────────────────────────────

const cmd = command({
  name: "create-pair",
  description: "Create/destroy one throwaway dual-stack EC2 pair.",
  args: {
    pair: option({
      long: "pair",
      type: optional(string),
      description: "pair name; hosts become <pair>-tester / <pair>-tested",
    }),
    region: option({
      long: "region",
      type: optional(string),
      description: "AWS region (default AWS_REGION or eu-central-1)",
    }),
    testerOs: option({ long: "tester-os", type: optional(string) }),
    testerVersion: option({ long: "tester-version", type: optional(string) }),
    testerArch: option({ long: "tester-arch", type: optional(string) }),
    testedOs: option({ long: "tested-os", type: optional(string) }),
    testedVersion: option({ long: "tested-version", type: optional(string) }),
    testedArch: option({ long: "tested-arch", type: optional(string) }),
    arch: option({
      long: "arch",
      type: optional(string),
      description: "shorthand for --tester-arch",
    }),
    testerInstanceType: option({
      long: "tester-instance-type",
      type: optional(string),
    }),
    testedInstanceType: option({
      long: "tested-instance-type",
      type: optional(string),
    }),
    ttlMinutes: option({
      long: "ttl-minutes",
      type: number,
      defaultValue: () => 60,
    }),
    vpcCidr: option({ long: "vpc-cidr", type: optional(string) }),
    subnetCidr: option({ long: "subnet-cidr", type: optional(string) }),
    testerIp: option({ long: "tester-ip", type: optional(string) }),
    testedIp: option({ long: "tested-ip", type: optional(string) }),
    testerL2: option({ long: "tester-l2", type: optional(string) }),
    testedL2: option({ long: "tested-l2", type: optional(string) }),
    testerAmi: option({ long: "tester-ami", type: optional(string) }),
    testedAmi: option({ long: "tested-ami", type: optional(string) }),
    subnetId: option({ long: "subnet-id", type: optional(string) }),
    sgId: option({ long: "sg-id", type: optional(string) }),
    vpcId: option({ long: "vpc-id", type: optional(string) }),
    sshKey: multioption({
      long: "ssh-key",
      type: array(string),
      description: "authorized key; repeat for several",
    }),
    destroy: flag({ long: "destroy", description: "terminate pair + network" }),
    reap: flag({
      long: "reap",
      description: "terminate expired/stopped pairs of this tool",
    }),
    networkOnly: flag({
      long: "network-only",
      description: "create the VPC/subnet/SG and print ids; launch nothing",
    }),
  },
  handler: async (a) => {
    const region = a.region ??
      Deno.env.get("AWS_REGION") ?? Deno.env.get("AWS_DEFAULT_REGION") ??
      "eu-central-1";
    // adaptive mode enables the SDK's client-side rate limiter (token bucket),
    // so we pace ourselves before AWS has to throttle us — not just retry after.
    const ec2 = new EC2({
      region,
      maxAttempts: 10,
      retryMode: "adaptive",
    });

    if (a.reap) {
      const pairs = await reap(ec2);
      for (const p of pairs) {
        const stillLive = (await findInstances(ec2, p)).some((i) =>
          !["terminated", "shutting-down"].includes(i.State?.Name ?? "")
        );
        if (!stillLive) await destroyPair(ec2, p);
      }
      console.log(JSON.stringify({ reapedPairs: pairs }, null, 2));
      return;
    }

    if (a.pair === undefined) {
      throw new Error("--pair is required (or use --reap)");
    }
    const pair = a.pair;
    if (!/^[a-z0-9][a-z0-9-]{0,30}$/.test(pair)) {
      throw new Error(`bad --pair ${pair}: [a-z0-9-], max 31 chars`);
    }

    if (a.destroy) {
      await destroyPair(ec2, pair);
      console.log(JSON.stringify({ pair, destroyed: true }, null, 2));
      return;
    }

    const testerArch = pickArch(
      a.testerArch ?? a.arch ?? "amd64",
      "--tester-arch",
    );
    const testedArch = pickArch(a.testedArch ?? "amd64", "--tested-arch");
    const testerOs = pickOs(a.testerOs ?? "ubuntu", "--tester-os");
    const testedOs = pickOs(a.testedOs ?? "ubuntu", "--tested-os");
    const testerVersion = a.testerVersion ?? "24.04";
    const testedVersion = a.testedVersion ?? "24.04";
    const defaultType = (arch: Arch) => arch === "arm64" ? "t4g.micro" : "t3.micro";
    const testerType = a.testerInstanceType ?? defaultType(testerArch);
    const testedType = a.testedInstanceType ?? defaultType(testedArch);
    const ttlMin = a.ttlMinutes;
    if (!Number.isFinite(ttlMin) || ttlMin <= 0) {
      throw new Error("--ttl-minutes must be > 0");
    }

    const subnetCidr = a.subnetCidr ?? "10.42.0.0/24";
    const testerIp = a.testerIp ?? "10.42.0.10";
    const testedIp = a.testedIp ?? "10.42.0.11";
    const testerL2 = a.testerL2 ?? "10.222.0.1/24";
    const testedL2 = a.testedL2 ?? "10.222.0.2/24";
    for (
      const [what, text] of [
        ["--tester-ip", testerIp],
        ["--tested-ip", testedIp],
        ["--tester-l2", testerL2],
        ["--tested-l2", testedL2],
      ] as const
    ) {
      IPv4.parse(text); // validate + family check via ovn-fabric's wrapper
      if (what.endsWith("-ip") && !IPv4.parse(subnetCidr).includes(IPv4.parse(text))) {
        throw new Error(`${what} ${text} is not inside --subnet-cidr ${subnetCidr}`);
      }
    }
    if (testerIp === testedIp) {
      throw new Error("tester and tested private IPv4 must differ");
    }

    const [testerTpl, testedTpl] = await Promise.all([
      Deno.readTextFile(new URL("./tester.cloud-init.yaml", import.meta.url)),
      Deno.readTextFile(new URL("./tested.cloud-init.yaml", import.meta.url)),
    ]);

    // The owner key is always installed; session keys (CI) are added beside it.
    const sshKeys = [...new Set([DEFAULT_KEY, ...a.sshKey])];
    const vni = deriveVni(pair);
    const net = await ensureNetwork(ec2, {
      pair,
      ttlMin,
      vpcId: a.vpcId,
      subnetId: a.subnetId,
      sgId: a.sgId,
      vpcCidr: a.vpcCidr ?? "10.42.0.0/16",
      subnetCidr,
    });
    if (a.networkOnly) {
      console.log(JSON.stringify({
        pair,
        region,
        network: {
          vpcId: net.vpcId,
          subnetId: net.subnetId,
          securityGroupId: net.sgId,
          subnetCidr,
        },
      }, null, 2));
      return;
    }

    const testerAmi = await resolveAmi(
      ec2,
      a.testerAmi,
      testerOs,
      testerVersion,
      testerArch,
    );
    const testedAmi = await resolveAmi(
      ec2,
      a.testedAmi,
      testedOs,
      testedVersion,
      testedArch,
    );

    const { tester, tested } = await createPair({
      ec2,
      pair,
      tester: {
        osName: testerOs,
        version: testerVersion,
        arch: testerArch,
        ip: testerIp,
        l2: testerL2,
        ami: testerAmi,
        instanceType: testerType,
      },
      tested: {
        osName: testedOs,
        version: testedVersion,
        arch: testedArch,
        ip: testedIp,
        l2: testedL2,
        ami: testedAmi,
        instanceType: testedType,
      },
      net,
      ttlMin,
      sshKeys,
      templates: { tester: testerTpl, tested: testedTpl },
    });

    console.log(JSON.stringify({
      pair,
      region,
      vni,
      ttlMinutes: ttlMin,
      network: {
        vpcId: net.vpcId,
        subnetId: net.subnetId,
        securityGroupId: net.sgId,
      },
      tester: {
        ...tester,
        os: testerOs,
        version: testerVersion,
        arch: testerArch,
        instanceType: testerType,
      },
      tested: {
        ...tested,
        os: testedOs,
        version: testedVersion,
        arch: testedArch,
        instanceType: testedType,
      },
    }, null, 2));
  },
});

if (import.meta.main) {
  await run(cmd, Deno.args);
}
