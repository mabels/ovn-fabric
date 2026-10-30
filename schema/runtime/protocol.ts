// schema/runtime/protocol.ts — the runtime's own protocol: the parts a
// message is made of. A message on the wire is a COLLECTION of parts,
// `[{kind: "MsgHeader", ...}, {kind: "VersionReq", ...}]`; every part
// carries its `kind`. The Python side (rt/message.py) validates the
// collection and transposes it into one internal structure per message.
//
// This schema belongs to the runtime, not to ovn-fabric's IR
// (src/protocol.ts): the runtime owns its message format (ADR 0007).
// Python dataclasses are generated from it by
// `deno run -A schema/runtime/generate.ts`.

import { type } from "arktype";

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

/** A log line, emitted by whoever is handling `MsgHeader.reqId` (if any). */
export const LogMsg = type({
  kind: "'LogMsg'",
  level: LogLevel,
  message: "string",
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

// ── system dependency resolver (rt/services/sysdeps.py) ─────────────────

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

// ── docker prepare pipeline (rt/services/docker.py) ─────────────────────

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

// ── docker run (rt/services/docker_run.py) ──────────────────────────────

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
