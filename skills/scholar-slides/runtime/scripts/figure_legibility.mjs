#!/usr/bin/env node
// CKPT-2 bridge: reuse the shared render geometry arithmetic for the Python review pipeline.
import fs from "node:fs";
import { structuredLegibilityFindings } from "./lib/render_checks.mjs";

function main() {
  const input = process.argv[2];
  if (!input) throw new Error("usage: figure_legibility.mjs <input.json>");
  const payload = JSON.parse(fs.readFileSync(input, "utf8"));
  process.stdout.write(`${JSON.stringify(structuredLegibilityFindings(payload))}\n`);
}

try {
  main();
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
}
