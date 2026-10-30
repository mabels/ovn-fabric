// fabric/schema/protocol.ts — the protocol of the runtime and fabric's services: the parts a
// message is made of. A message on the wire is a COLLECTION of parts,
// `[{kind: "MsgHeader", ...}, {kind: "VersionReq", ...}]`; every part
// carries its `kind`. The Python side (runtime/message.py) validates the
// collection and transposes it into one internal structure per message.
//
// This schema belongs to the runtime, not to ovn-fabric's IR
// (src/protocol.ts): the runtime owns its message format (ADR 0007).
// Python dataclasses are generated from it by
// `deno run -A fabric/schema/generate.ts`.

import { type } from "arktype";
import { IRNode } from "../../src/protocol.ts";

/** Present on every message. `id` is unique per message; `reqId` ties a
 * request, its response and every log emitted while handling it
 * together; `parentReqId` links a request sent from inside a handler to
 * the request being handled. */
export const MsgHeader = type({
  kind: "'MsgHeader'",
  id: "string",
  "reqId?": "string",
  "parentReqId?": "string",
  time: "number",
  source: "string",
});

export const LogLevel = type("'debug' | 'info' | 'warn' | 'error'");

/** A log line, emitted by whoever is handling `MsgHeader.reqId` (if any).
 * `component` names who wrote it — a service (sysdeps, docker, …) or a
 * container for its output — and survives being relayed. */
export const LogMsg = type({
  kind: "'LogMsg'",
  level: LogLevel,
  message: "string",
  "component?": "string",
});

/** The final answer to a request that failed. */
export const ErrorRes = type({
  kind: "'ErrorRes'",
  code: "string",
  message: "string",
});

export const VersionReq = type({
  kind: "'VersionReq'",
});

export const VersionRes = type({
  kind: "'VersionRes'",
  runtime: "string",
  protocol: "number.integer",
});

// ── system dependency resolver (fabric/services/sysdeps.py) ─────────────────

/** Where one system dependency ended up: installed and usable, not
 * there within the request's timeout (the install may still finish), or
 * the install failed. */
export const SystemDepState = type("'ready' | 'timeout' | 'failed'");

export const DepStatus = type({
  dep: "string",
  state: SystemDepState,
  "message?": "string",
});

/** Make these system dependencies (apt packages) present. `timeout` in
 * seconds; without it the request waits as long as the installs take. */
export const ResolveSystemDepsReq = type({
  kind: "'ResolveSystemDepsReq'",
  deps: "string[]",
  "timeout?": "number",
});

/** Emitted (with the request's reqId) as each dependency settles. */
export const ResolveSystemDep = type({
  kind: "'ResolveSystemDep'",
  dep: "string",
  state: SystemDepState,
  "message?": "string",
});

/** The final answer: one status per requested dependency. */
export const ResolveSystemDepsRes = type({
  kind: "'ResolveSystemDepsRes'",
  deps: DepStatus.array(),
});

// ── docker prepare pipeline (fabric/services/docker.py) ─────────────────────

/** One image a host runs: built from `dockerfile` when given, else pulled. */
export const ImageSpec = type({
  image: "string",
  "dockerfile?": "string",
});

export const ImageState = type("'ready' | 'failed'");

export const ImageStatus = type({
  image: "string",
  state: ImageState,
  "message?": "string",
});

/** Make docker usable (its system dependencies resolved, the daemon
 * reachable), then make every image present (pull or build). */
export const DockerPrepareReq = type({
  kind: "'DockerPrepareReq'",
  images: ImageSpec.array(),
});

/** Emitted (with the request's reqId) once docker and every image are
 * ready — what a service that runs the IR waits for. */
export const DockerReady = type({
  kind: "'DockerReady'",
  images: ImageStatus.array(),
});

/** The final answer: one status per requested image. */
export const DockerPrepareRes = type({
  kind: "'DockerPrepareRes'",
  images: ImageStatus.array(),
});

// ── docker run (fabric/services/docker_run.py) ──────────────────────────────

export const EnvVar = type({ name: "string", value: "string" });

export const Mount = type({
  host: "string",
  container: "string",
  "readOnly?": "boolean",
});

/** A file the container gets, bind-mounted read-only at `path`. */
export const ContainerFile = type({ path: "string", content: "string" });

/** One container to run, with its options from the IR. Its image is made
 * present first (DockerPrepareReq): built from `dockerfile`, else pulled. */
export const ContainerSpec = type({
  name: "string",
  image: "string",
  "dockerfile?": "string",
  "command?": "string[]",
  "privileged?": "boolean",
  "network?": "string",
  "env?": EnvVar.array(),
  "mounts?": Mount.array(),
  "files?": ContainerFile.array(),
  /** Extra `docker run` flags, as the IR gives them. */
  "flags?": "string[]",
});

/** Run a container. By default the request ends once it is started: the
 * runtime owns it from then on (follows it into its own log, removes it
 * when it stops, stops it when the runtime stops). With `follow` the
 * request owns it instead and lasts until it stops: its stdout/stderr come
 * as LogMsg (info/warn); ending the request early (the client goes away)
 * stops it. Its lifecycle comes as DockerContainer events either way. */
export const DockerRunReq = type({
  kind: "'DockerRunReq'",
  container: ContainerSpec,
  "follow?": "boolean",
});

export const ContainerState = type(
  "'starting' | 'started' | 'stopped' | 'failed'",
);

/** A lifecycle step of the container (with the request's reqId). */
export const DockerContainer = type({
  kind: "'DockerContainer'",
  name: "string",
  state: ContainerState,
  "exitCode?": "number.integer",
  "message?": "string",
});

/** The final answer: how the container ended. */
export const DockerRunRes = type({
  kind: "'DockerRunRes'",
  name: "string",
  state: ContainerState,
  "exitCode?": "number.integer",
  "message?": "string",
});

// ── abstract dependencies (fabric/services/abstract_deps.py) ───────────────

/** An abstract dependency ("ovn", "docker"). Without `systemDeps` the
 * service's own table says which system dependencies (apt packages) it
 * means; with them the request says so (e.g. the IR's per-host choice:
 * `ovn` on the central is ovn-central + ovn-host). */
export const AbstractDepSpec = type({
  name: "string",
  "systemDeps?": "string[]",
});

/** Resolve these abstract dependencies: their system dependencies are
 * made present (one ResolveSystemDepsReq for all of them). */
export const AbstractDepsReq = type({
  kind: "'AbstractDepsReq'",
  deps: AbstractDepSpec.array(),
  "timeout?": "number",
});

export const AbstractDepStatus = type({
  name: "string",
  state: SystemDepState,
  deps: DepStatus.array(),
});

/** Emitted (with the request's reqId) as each abstract dependency
 * settles: ready once all its system dependencies are, else failed or
 * timeout as the worst of them. */
export const AbstractDep = type({
  kind: "'AbstractDep'",
  name: "string",
  state: SystemDepState,
  deps: DepStatus.array(),
});

/** Emitted once every requested abstract dependency is ready. */
export const AbstractDepsReady = type({
  kind: "'AbstractDepsReady'",
  names: "string[]",
});

/** The final answer: one status per requested abstract dependency. */
export const AbstractDepsRes = type({
  kind: "'AbstractDepsRes'",
  deps: AbstractDepStatus.array(),
});

// ── IR manager (fabric/services/ir.py) ──────────────────────────────────

/** The IR as a part: its nodes, typed by ovn-fabric's own IR schema
 * (src/protocol.ts). */
export const IrCollection = type({
  kind: "'IrCollection'",
  collection: IRNode.array(),
});

/** What storing did: a new IR, the same content again, or the name now
 * points to different content. */
export const IrPutStatus = type("'created' | 'unchanged' | 'replaced'");

export const IrInfo = type({
  /** Its name — or its hash, when it was stored without one. */
  name: "string",
  /** sha256 of the normalized IR: "sha256:<hex>". */
  hash: "string",
  nodes: "number.integer",
  /** The infra.host names in it. */
  hosts: "string[]",
  bytes: "number.integer",
  /** When this content was stored (seconds since the epoch). */
  storedAt: "number",
});

/** Validate, normalize and store an IR — under `name`, or (without one)
 * under its hash. */
export const IrPutReq = type({
  kind: "'IrPutReq'",
  "name?": "string",
  ir: IrCollection,
});

export const IrPutRes = type({
  kind: "'IrPutRes'",
  /** The name it is stored under: the given one, or its hash. */
  name: "string",
  hash: "string",
  nodes: "number.integer",
  status: IrPutStatus,
  /** The hash the name pointed to before (status replaced). */
  "previous?": "string",
});

export const IrListReq = type({ kind: "'IrListReq'" });

export const IrListRes = type({
  kind: "'IrListRes'",
  irs: IrInfo.array(),
});

/** `ref` is a name or a hash. */
export const IrGetReq = type({ kind: "'IrGetReq'", ref: "string" });

export const IrGetRes = type({
  kind: "'IrGetRes'",
  info: IrInfo,
  ir: IrCollection,
});

/** Remove a name — or, by hash, an IR stored without one (refused while
 * names still point to it). Content goes once nothing refers to it. */
export const IrDeleteReq = type({ kind: "'IrDeleteReq'", ref: "string" });

export const IrDeleteRes = type({
  kind: "'IrDeleteRes'",
  name: "string",
  hash: "string",
});

// ── IR apply (fabric/services/apply.py) ─────────────────────────────────

/** What applying does: create what the IR declares, or delete it (rewound).
 * Later: 'reconcile' (diff against what is there). */
export const ApplyAction = type("'create' | 'delete'");

/** Apply a stored IR on this host. */
export const IrApplyReq = type({
  kind: "'IrApplyReq'",
  /** A stored IR: its hash or its name (IrPutReq first). */
  ref: "string",
  action: ApplyAction,
  /** Which of the IR's infra.hosts this runtime is. Default: the runtime's
   * own (from `serve --host`, else its hostname). */
  "host?": "string",
  /** Only describe what would run (per unit of work), run nothing. */
  "dryRun?": "boolean",
});

export const ApplyState = type(
  "'pending' | 'running' | 'done' | 'failed' | 'skipped'",
);

/** One unit of work of an apply: a scope of the IR on this host
 * (host, configure, a router, a workload, the cluster objects, …). */
export const ApplyUnit = type({
  name: "string",
  state: ApplyState,
  /** Operations it ran (or would run, dry-run). */
  "operations?": "number.integer",
  "message?": "string",
});

/** Emitted (with the request's reqId) as each unit of work changes state;
 * its commands and their output come as LogMsg from it, as usual. */
export const IrApplyProgress = type({
  kind: "'IrApplyProgress'",
  unit: ApplyUnit,
});

export const IrApplyOutcome = type("'done' | 'failed'");

/** The final answer: what was applied, and how each unit of work ended. */
export const IrApplyRes = type({
  kind: "'IrApplyRes'",
  /** The IR's hash: what was really applied, whatever `ref` was. */
  hash: "string",
  action: ApplyAction,
  host: "string",
  state: IrApplyOutcome,
  units: ApplyUnit.array(),
});

// ── unit executor (fabric/services/unit.py) ──────────────────────────────
// A unit of work is data: its steps are the lifecycle operations of the
// deployer (deployer/lifecycle.py), serialized. The executor turns them
// back into those operations and runs them on a LifecycleController:
// create forward (strict), delete rewound (best effort).

/** A value produced by a Capture of this run (deployer's RuntimeRef):
 * filled in right before the operation using it runs. `key` picks one
 * field of a captured result object. */
export const RefArg = type({ ref: "string", "key?": "string" });

export const Arg = type("string").or(RefArg);

export const StepCommand = type({
  label: "string",
  argv: Arg.array(),
  "optional?": "boolean",
});

/** What a step creates in the object graph (ADR 0006); `fields` is JSON. */
export const RawNode = type({ id: "string", kind: "string", fields: "string" });

export const PrimitiveStep = type({
  kind: "'Primitive'",
  key: "string",
  nodes: RawNode.array(),
  create: StepCommand.array(),
  delete: StepCommand.array(),
});

export const WriteFileStep = type({
  kind: "'WriteFile'",
  path: "string",
  content: "string",
  "mode?": "number.integer",
});

export const CaptureStep = type({
  kind: "'Capture'",
  name: "string",
  label: "string",
  argv: Arg.array(),
  "optional?": "boolean",
  "retries?": "number.integer",
  "restartUnit?": "string",
  "resultKey?": "string",
});

export const SymlinkNetnsStep = type({
  kind: "'SymlinkNetns'",
  container: "string",
  pid: Arg,
});

/** A directory: created with its parents; deleted with its content when
 * owned, else only while empty (with its empty parents). */
export const DirectoryStep = type({
  kind: "'Directory'",
  path: "string",
  "owned?": "boolean",
});

/** The mode of a file, octal ("600"). */
export const ChmodStep = type({
  kind: "'Chmod'",
  path: "string",
  mode: "string",
});

/** A path that only goes away: removed on delete (a tree if recursive). */
export const RemovePathStep = type({
  kind: "'RemovePath'",
  path: "string",
  "recursive?": "boolean",
});

export const UnitStepSpec = PrimitiveStep.or(WriteFileStep).or(CaptureStep).or(
  SymlinkNetnsStep,
).or(DirectoryStep).or(ChmodStep).or(RemovePathStep);

/** One unit of work: a section of the IR's declarations on this host
 * ("router port: lrp-x (home)"). */
export const Unit = type({
  /** The section: "<kind>: <name>". */
  name: "string",
  kind: "string",
  /** The IR node it comes from, if one. */
  "key?": "string",
  /** What it belongs to: "configure", "cluster", or the router/workload it
   * is part of. A unit needs the units before it in its scope. */
  "scope?": "string",
  steps: UnitStepSpec.array(),
});

/** A value captured during the run, JSON-encoded, handed between units. */
export const UnitValue = type({ name: "string", json: "string" });

export const UnitExecReq = type({
  kind: "'UnitExecReq'",
  unit: Unit,
  action: ApplyAction,
  /** Values captured by earlier units, for this one's refs. */
  "inputs?": UnitValue.array(),
  "host?": "string",
  /** Describe the commands, run nothing. */
  "dryRun?": "boolean",
});

export const UnitExecRes = type({
  kind: "'UnitExecRes'",
  unit: "string",
  state: IrApplyOutcome,
  "message?": "string",
  /** Every value known after the unit (inputs and its own captures). */
  outputs: UnitValue.array(),
});
