// runtime/schema/protocol.ts — the runtime's own parts: what every message
// carries, whatever the application. A message on the wire is a COLLECTION
// of parts, `[{kind: "MsgHeader", ...}, {kind: "VersionReq", ...}]`; every
// part carries its `kind`. The Python side (runtime/message.py) validates
// the collection and transposes it into one internal structure per message.
//
// Applications (fabric/schema/protocol.ts) add their request/response/event
// parts next to these. Python dataclasses are generated from it by
// `deno run -A runtime/schema/generate.ts`.

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
