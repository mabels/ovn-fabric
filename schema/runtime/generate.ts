// schema/runtime/generate.ts — regenerate the runtime's protocol for
// Python: writes rt/protocol.schema.json (JSON Schema, named $defs) and
// runs datamodel-codegen into rt/generated.py (stdlib dataclasses).
//
//   deno run -A schema/runtime/generate.ts
//
// Uses the build-only .venv-build/ that `src/cli.ts generate-pytypes`
// bootstraps; run that once first on a fresh checkout.

import { refNamedShapes, shape } from "../../src/protocol.ts";
import {
  ContainerFile,
  ContainerSpec,
  ContainerState,
  DepStatus,
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
  LogLevel,
  LogMsg,
  Mount,
  MsgHeader,
  ResolveSystemDep,
  ResolveSystemDepsReq,
  ResolveSystemDepsRes,
  SystemDepState,
  VersionReq,
  VersionRes,
} from "./protocol.ts";

const SCHEMA_PATH = "rt/protocol.schema.json";
const GENERATED_PY_PATH = "rt/generated.py";
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
});

// No root shape: every part is its own top-level class.
const schema = {
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$defs": shapes,
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
