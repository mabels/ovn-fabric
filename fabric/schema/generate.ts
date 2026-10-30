// fabric/schema/generate.ts — regenerate the runtime's protocol for
// Python: writes fabric/protocol.schema.json (JSON Schema, named $defs) and
// runs datamodel-codegen into fabric/generated.py (stdlib dataclasses).
//
//   deno run -A fabric/schema/generate.ts
//
// Uses the build-only .venv-build/ that `src/cli.ts generate-pytypes`
// bootstraps; run that once first on a fresh checkout.

import { buildJsonSchema, refNamedShapes, shape } from "../../src/protocol.ts";
import {
  AbstractDep,
  AbstractDepSpec,
  AbstractDepsReady,
  AbstractDepsReq,
  AbstractDepsRes,
  AbstractDepStatus,
  ApplyAction,
  ApplyState,
  ApplyUnit,
  Arg,
  CaptureStep,
  ChmodStep,
  ContainerFile,
  ContainerSpec,
  ContainerState,
  DepStatus,
  DirectoryStep,
  DockerContainer,
  DockerPrepareReq,
  DockerPrepareRes,
  DockerReady,
  DockerRunReq,
  DockerRunRes,
  EnvVar,
  ErrorRes,
  ImageSpec,
  ImageState,
  ImageStatus,
  IrApplyOutcome,
  IrApplyProgress,
  IrApplyReq,
  IrApplyRes,
  IrCollection,
  IrDeleteReq,
  IrDeleteRes,
  IrGetReq,
  IrGetRes,
  IrInfo,
  IrListReq,
  IrListRes,
  IrPutReq,
  IrPutRes,
  IrPutStatus,
  LogLevel,
  LogMsg,
  Mount,
  MsgHeader,
  PrimitiveStep,
  RawNode,
  RefArg,
  RemovePathStep,
  ResolveSystemDep,
  ResolveSystemDepsReq,
  ResolveSystemDepsRes,
  StepCommand,
  SymlinkNetnsStep,
  SystemDepState,
  Unit,
  UnitExecReq,
  UnitExecRes,
  UnitStepSpec,
  UnitValue,
  VersionReq,
  VersionRes,
  WriteFileStep,
} from "./protocol.ts";

const SCHEMA_PATH = "fabric/protocol.schema.json";
const GENERATED_PY_PATH = "fabric/generated.py";
const CODEGEN = ".venv-build/bin/datamodel-codegen";

const shapes = refNamedShapes({
  MsgHeader: shape(MsgHeader),
  LogLevel: shape(LogLevel),
  LogMsg: shape(LogMsg),
  ErrorRes: shape(ErrorRes),
  VersionReq: shape(VersionReq),
  VersionRes: shape(VersionRes),
  SystemDepState: shape(SystemDepState),
  DepStatus: shape(DepStatus),
  ResolveSystemDepsReq: shape(ResolveSystemDepsReq),
  ResolveSystemDep: shape(ResolveSystemDep),
  ResolveSystemDepsRes: shape(ResolveSystemDepsRes),
  ImageSpec: shape(ImageSpec),
  ImageState: shape(ImageState),
  ImageStatus: shape(ImageStatus),
  DockerPrepareReq: shape(DockerPrepareReq),
  DockerReady: shape(DockerReady),
  DockerPrepareRes: shape(DockerPrepareRes),
  EnvVar: shape(EnvVar),
  Mount: shape(Mount),
  ContainerFile: shape(ContainerFile),
  ContainerSpec: shape(ContainerSpec),
  DockerRunReq: shape(DockerRunReq),
  ContainerState: shape(ContainerState),
  DockerContainer: shape(DockerContainer),
  DockerRunRes: shape(DockerRunRes),
  AbstractDepSpec: shape(AbstractDepSpec),
  AbstractDepsReq: shape(AbstractDepsReq),
  AbstractDepStatus: shape(AbstractDepStatus),
  AbstractDep: shape(AbstractDep),
  AbstractDepsReady: shape(AbstractDepsReady),
  AbstractDepsRes: shape(AbstractDepsRes),
  IrCollection: shape(IrCollection),
  IrPutStatus: shape(IrPutStatus),
  IrInfo: shape(IrInfo),
  IrPutReq: shape(IrPutReq),
  IrPutRes: shape(IrPutRes),
  IrListReq: shape(IrListReq),
  IrListRes: shape(IrListRes),
  IrGetReq: shape(IrGetReq),
  IrGetRes: shape(IrGetRes),
  IrDeleteReq: shape(IrDeleteReq),
  IrDeleteRes: shape(IrDeleteRes),
  ApplyAction: shape(ApplyAction),
  IrApplyReq: shape(IrApplyReq),
  ApplyState: shape(ApplyState),
  ApplyUnit: shape(ApplyUnit),
  IrApplyProgress: shape(IrApplyProgress),
  IrApplyOutcome: shape(IrApplyOutcome),
  IrApplyRes: shape(IrApplyRes),
  RefArg: shape(RefArg),
  Arg: shape(Arg),
  StepCommand: shape(StepCommand),
  RawNode: shape(RawNode),
  PrimitiveStep: shape(PrimitiveStep),
  WriteFileStep: shape(WriteFileStep),
  CaptureStep: shape(CaptureStep),
  SymlinkNetnsStep: shape(SymlinkNetnsStep),
  DirectoryStep: shape(DirectoryStep),
  ChmodStep: shape(ChmodStep),
  RemovePathStep: shape(RemovePathStep),
  UnitStepSpec: shape(UnitStepSpec),
  Unit: shape(Unit),
  UnitValue: shape(UnitValue),
  UnitExecReq: shape(UnitExecReq),
  UnitExecRes: shape(UnitExecRes),
});

// The IR's own types (src/protocol.ts), under their own names: its nodes
// become the same named classes here, and IrCollection's `collection` is a
// list of the IRNode union of them (not an inlined copy).
const ir = buildJsonSchema() as {
  "$defs": Record<string, unknown>;
  oneOf: unknown[];
};
shapes["IrCollection"] = {
  type: "object",
  properties: {
    kind: { const: "IrCollection" },
    collection: { type: "array", items: { "$ref": "#/$defs/IRNode" } },
  },
  required: ["kind", "collection"],
};

// No root shape: every part is its own top-level class.
const schema = {
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$defs": { ...ir.$defs, IRNode: { oneOf: ir.oneOf }, ...shapes },
};

await Deno.writeTextFile(SCHEMA_PATH, JSON.stringify(schema, null, 2) + "\n");
console.error(`wrote ${SCHEMA_PATH}`);

const { success, stderr } = await new Deno.Command(CODEGEN, {
  args: [
    "--input",
    SCHEMA_PATH,
    "--input-file-type",
    "jsonschema",
    "--output",
    GENERATED_PY_PATH,
    "--output-model-type",
    "dataclasses.dataclass",
    "--target-python-version",
    "3.11",
    "--disable-timestamp",
  ],
  stderr: "piped",
}).output();
if (!success) {
  throw new Error(
    `datamodel-codegen failed:\n${new TextDecoder().decode(stderr)}`,
  );
}
console.error(`wrote ${GENERATED_PY_PATH}`);
