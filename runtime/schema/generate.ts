// runtime/schema/generate.ts — regenerate the runtime's protocol for
// Python: writes runtime/protocol.schema.json (JSON Schema, named $defs) and
// runs datamodel-codegen into runtime/generated.py (stdlib dataclasses).
//
//   deno run -A runtime/schema/generate.ts
//
// `generate()` is what an application's generator (fabric/schema/generate.ts)
// uses for its own parts. Uses the build-only .venv-build/ that
// `src/cli.ts generate-pytypes` bootstraps; run that once first on a fresh
// checkout.

import { refNamedShapes, shape } from "../../src/protocol.ts";
import { ErrorRes, LogLevel, LogMsg, MsgHeader } from "./protocol.ts";

const CODEGEN = ".venv-build/bin/datamodel-codegen";

/** Write `$defs` as JSON Schema to `schemaPath` (no root shape: every part
 * is its own top-level class) and generate `pyPath` from it. */
export async function generate(
  defs: Record<string, unknown>,
  schemaPath: string,
  pyPath: string,
): Promise<void> {
  const schema = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$defs": defs,
  };
  await Deno.writeTextFile(schemaPath, JSON.stringify(schema, null, 2) + "\n");
  console.error(`wrote ${schemaPath}`);

  const { success, stderr } = await new Deno.Command(CODEGEN, {
    args: [
      "--input",
      schemaPath,
      "--input-file-type",
      "jsonschema",
      "--output",
      pyPath,
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
  console.error(`wrote ${pyPath}`);
}

if (import.meta.main) {
  await generate(
    refNamedShapes({
      MsgHeader: shape(MsgHeader),
      LogLevel: shape(LogLevel),
      LogMsg: shape(LogMsg),
      ErrorRes: shape(ErrorRes),
    }),
    "runtime/protocol.schema.json",
    "runtime/generated.py",
  );
}
