import path from "node:path";
import fs from "node:fs";
import { spawnSync } from "node:child_process";
import { resolvePythonCommand } from "./platform.mjs";

export function parseCheckpointArgument(argv, usage) {
  const index = argv.indexOf("--checkpoint");
  if (index === -1 || !argv[index + 1]) {
    throw new Error(`checkpoint record is required. usage: ${usage} --checkpoint <record.json>`);
  }
  if (argv.indexOf("--checkpoint", index + 1) !== -1) {
    throw new Error("checkpoint record may be supplied only once");
  }
  if (index + 2 !== argv.length) {
    throw new Error("--checkpoint <record.json> must be the final argument; trailing arguments are not allowed");
  }
  return { positional: argv.slice(0, index), checkpointRecord: argv[index + 1] };
}

export function requireApprovedCheckpoint({
  rootDir,
  checkpointRecord,
  artifactPath,
  expectedCheckpoint,
  env = process.env,
  platform = process.platform,
  runner = spawnSync,
}) {
  if (!checkpointRecord) throw new Error("checkpoint record is required");
  if (!artifactPath) {
    try {
      artifactPath = JSON.parse(fs.readFileSync(checkpointRecord, "utf8"))?.artifact?.path;
    } catch (error) {
      throw new Error(`checkpoint record cannot be read: ${error.message}`);
    }
  }
  if (!artifactPath) throw new Error("checkpoint artifact is required");
  const python = resolvePythonCommand({ rootDir, env, platform });
  const result = runner(
    python.command,
    [
      ...python.args,
      path.join(rootDir, "scripts", "checkpoint.py"),
      "require",
      checkpointRecord,
      artifactPath,
      "--expected-checkpoint",
      expectedCheckpoint,
    ],
    { cwd: rootDir, env, encoding: "utf8", shell: false },
  );
  if (result.error) {
    throw new Error(`checkpoint preflight could not start: ${result.error.message}`);
  }
  if (result.status !== 0) {
    const detail = [result.stdout, result.stderr].filter(Boolean).join("\n").trim();
    throw new Error(`checkpoint preflight failed for ${expectedCheckpoint}${detail ? `: ${detail}` : ""}`);
  }
  try {
    return JSON.parse(fs.readFileSync(checkpointRecord, "utf8"));
  } catch (error) {
    throw new Error(`checkpoint record cannot be read after preflight: ${error.message}`);
  }
}
