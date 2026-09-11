#!/usr/bin/env node
/**
 * Generates `lib/api-types.ts` from FastAPI's OpenAPI schema (SPEC 2, 10).
 *
 * Resolution order:
 *   1. $OPENAPI_URL, if set
 *   2. the running API at $NEXT_PUBLIC_API_BASE_URL (default http://localhost:8000)
 *   3. a schema dumped to ../openapi.json
 *
 * Step 3 exists so CI and a fresh checkout can generate types without standing
 * up the API; dump it with:
 *   cd backend && uv run python -c "import json;from evalharness.api.app import create_app;print(json.dumps(create_app().openapi()))" > ../openapi.json
 */

import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(here, "..");
const repoRoot = resolve(webRoot, "..");
const outFile = join(webRoot, "lib", "api-types.ts");

const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
const schemaUrl = process.env.OPENAPI_URL ?? `${apiBase.replace(/\/$/, "")}/openapi.json`;
const schemaFile = join(repoRoot, "openapi.json");

async function loadSchema() {
  try {
    const response = await fetch(schemaUrl, { signal: AbortSignal.timeout(5000) });
    if (response.ok) {
      console.log(`> schema from ${schemaUrl}`);
      return JSON.stringify(await response.json());
    }
    console.warn(`> ${schemaUrl} returned ${response.status}`);
  } catch (error) {
    console.warn(`> could not reach ${schemaUrl} (${error.message})`);
  }

  if (existsSync(schemaFile)) {
    console.log(`> schema from ${schemaFile}`);
    return readFileSync(schemaFile, "utf8");
  }

  console.error(
    [
      "No OpenAPI schema available.",
      `Tried ${schemaUrl} and ${schemaFile}.`,
      "Start the API (docker compose up api) or dump the schema — see this script's header.",
    ].join("\n"),
  );
  process.exit(1);
}

const schema = await loadSchema();
const scratch = join(mkdtempSync(join(tmpdir(), "evalharness-openapi-")), "openapi.json");
writeFileSync(scratch, schema, "utf8");

// Run the CLI's JS entry point under the current node rather than the .bin
// shim: the shim needs a shell on Windows, and a shell mangles the spaces in
// paths like "…/Desktop/eval harness/web".
//
// Both paths handed to the CLI stay inside the temp dir, because its resolver
// URL-encodes paths and then stats them literally, which fails on any path
// containing a space. The result is copied to its real home afterwards.
const cli = join(webRoot, "node_modules", "openapi-typescript", "bin", "cli.js");
const scratchOut = join(dirname(scratch), "api-types.ts");
execFileSync(process.execPath, [cli, scratch, "--output", scratchOut], { stdio: "inherit" });
writeFileSync(outFile, readFileSync(scratchOut, "utf8"), "utf8");
console.log(`> wrote ${outFile}`);
